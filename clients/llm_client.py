"""Gemma4-260430 LLM 클라이언트. Ollama(로컬)/사내 API(internal) 전환 가능.

이 파일은 클라이언트 어댑터만 스캐폴딩한다 — rag/core.py 연동은 Phase 2(Session 7)에서 진행한다.
LLM_PROVIDER 환경변수로 구현체를 전환한다:
- "ollama": 로컬 Ollama(llama3 등)로 개발/테스트, "format": "json"으로 JSON 강제 출력
- "internal": 사내 Gemma4-260430 API로 실서비스 (엔드포인트/인증/스키마 TBD, 부서 확인 필요)
"""

import json
import re
from typing import Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import Settings, get_settings

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


class LLMClient(Protocol):
    async def generate_json(
        self, system_prompt: str, user_prompt: str, *, temperature: float = 0.0
    ) -> dict: ...
    async def aclose(self) -> None: ...


class LLMResponseParseError(Exception):
    def __init__(self, raw_text: str):
        super().__init__(f"LLM 응답이 유효한 JSON이 아닙니다: {raw_text[:500]!r}")
        self.raw_text = raw_text


def parse_json_response(raw_text: str) -> dict:
    """```json ... ``` 코드펜스로 감싸져 오는 경우까지 방어적으로 처리 후 JSON 파싱."""
    text = _FENCE_RE.sub("", raw_text.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMResponseParseError(raw_text) from e


def _make_retrying(max_retries: int) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )


class OllamaLLMClient:
    """Ollama 로컬 LLM(예: llama3). POST /api/chat, format="json"으로 JSON 강제 출력."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0, max_retries: int = 3):
        self._model = model
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._retrying = _make_retrying(max_retries)

    async def _chat_once(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        resp = await self._client.post(
            "/api/chat",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": temperature},
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    async def generate_json(
        self, system_prompt: str, user_prompt: str, *, temperature: float = 0.0
    ) -> dict:
        raw = await self._retrying(self._chat_once, system_prompt, user_prompt, temperature)
        return parse_json_response(raw)

    async def aclose(self) -> None:
        await self._client.aclose()


class InternalLLMClient:
    """사내 Gemma4-260430 API 어댑터.

    TBD: 엔드포인트/인증/JSON 강제 출력 방식 미확정. OpenAI 호환 chat completions 형식으로 추정 구현.
    실제 스펙 확정 시 _chat_once()의 요청/응답 파싱만 조정하면 된다.
    """

    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: float = 60.0, max_retries: int = 3
    ):
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )
        self._retrying = _make_retrying(max_retries)

    async def _chat_once(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": temperature,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def generate_json(
        self, system_prompt: str, user_prompt: str, *, temperature: float = 0.0
    ) -> dict:
        raw = await self._retrying(self._chat_once, system_prompt, user_prompt, temperature)
        return parse_json_response(raw)

    async def aclose(self) -> None:
        await self._client.aclose()


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """워커/스크립트 시작 시 1회 생성해서 재사용할 것 (요청마다 새로 만들지 말 것)."""
    settings = settings or get_settings()
    if settings.llm_provider == "ollama":
        return OllamaLLMClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_llm_model,
            timeout=settings.llm_http_timeout_sec,
            max_retries=settings.http_retry_max_attempts,
        )
    if settings.llm_provider == "internal":
        return InternalLLMClient(
            base_url=settings.internal_llm_api_base,
            api_key=settings.internal_llm_api_key,
            model=settings.internal_llm_model,
            timeout=settings.llm_http_timeout_sec,
            max_retries=settings.http_retry_max_attempts,
        )
    raise ValueError(f"알 수 없는 LLM_PROVIDER: {settings.llm_provider}")
