"""BGE-M3 임베딩 클라이언트. Ollama(로컬)/사내 API(internal) 전환 가능.

EMBEDDING_PROVIDER 환경변수로 구현체를 전환한다:
- "ollama": 로컬 Ollama(bge-m3 등)로 개발/테스트
- "internal": 사내 BGE-M3 API로 실서비스 (엔드포인트/인증/스키마 TBD, 부서 확인 필요)
"""

from typing import Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import Settings, get_settings

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def aclose(self) -> None: ...


def _make_retrying(max_retries: int) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )


def _validate_dim(
    embeddings: list[list[float]], expected_dim: int, model: str
) -> list[list[float]]:
    for e in embeddings:
        if len(e) != expected_dim:
            raise ValueError(
                f"임베딩 차원 불일치: 기대 {expected_dim}, 실제 {len(e)} (model={model}). "
                "EMBEDDING_PROVIDER/모델 설정을 확인하세요."
            )
    return embeddings


class OllamaEmbeddingClient:
    """Ollama 로컬 BGE-M3. 계약 확인됨:
    POST /api/embed {"model": ..., "input": [...]} -> {"embeddings": [[...]]}
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        embedding_dim: int,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self._model = model
        self._embedding_dim = embedding_dim
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._retrying = _make_retrying(max_retries)

    async def _post_embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post("/api/embed", json={"model": self._model, "input": texts})
        resp.raise_for_status()
        return resp.json()["embeddings"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings = await self._retrying(self._post_embed, texts)
        return _validate_dim(embeddings, self._embedding_dim, self._model)

    async def aclose(self) -> None:
        await self._client.aclose()


class InternalEmbeddingClient:
    """사내 BGE-M3 Embedding API 어댑터.

    TBD: 실제 엔드포인트/요청·응답 필드명 미확정(00-requirements.md §11, 부서 확인 대기).
    아래는 합리적으로 추정한 범용 REST 계약이며, 스펙 확정 후 _parse_response()만 조정하면 된다.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        embedding_dim: int,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self._model = model
        self._embedding_dim = embedding_dim
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )
        self._retrying = _make_retrying(max_retries)

    @staticmethod
    def _parse_response(payload: dict) -> list[list[float]]:
        if "data" in payload:  # OpenAI 스타일: {"data": [{"embedding": [...], "index": 0}, ...]}
            items = sorted(payload["data"], key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in items]
        if "embeddings" in payload:  # Ollama 스타일 호환
            return payload["embeddings"]
        raise ValueError(
            f"알 수 없는 임베딩 응답 형식 (TBD 스펙 확정 필요): keys={list(payload.keys())}"
        )

    async def _post_embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post("/embeddings", json={"model": self._model, "input": texts})
        resp.raise_for_status()
        return self._parse_response(resp.json())

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings = await self._retrying(self._post_embed, texts)
        return _validate_dim(embeddings, self._embedding_dim, self._model)

    async def aclose(self) -> None:
        await self._client.aclose()


def get_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    """워커/스크립트 시작 시 1회 생성해서 재사용할 것 (요청마다 새로 만들지 말 것)."""
    settings = settings or get_settings()
    if settings.embedding_provider == "ollama":
        return OllamaEmbeddingClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embedding_model,
            embedding_dim=settings.embedding_dim,
            timeout=settings.embedding_http_timeout_sec,
            max_retries=settings.http_retry_max_attempts,
        )
    if settings.embedding_provider == "internal":
        return InternalEmbeddingClient(
            base_url=settings.internal_embedding_api_base,
            api_key=settings.internal_embedding_api_key,
            model=settings.internal_embedding_model,
            embedding_dim=settings.embedding_dim,
            timeout=settings.embedding_http_timeout_sec,
            max_retries=settings.http_retry_max_attempts,
        )
    raise ValueError(f"알 수 없는 EMBEDDING_PROVIDER: {settings.embedding_provider}")
