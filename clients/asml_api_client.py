"""ASML API(부서 FastAPI 백엔드) 호출 어댑터. data(실측값) + spec(스펙 한계) 결합 JSON을 pull한다.

TBD: 실제 엔드포인트/인증 방식/응답 필드명 미확정(00-requirements.md §11, 부서 확인 대기).
아래는 합리적으로 추정한 REST 계약이며, 스펙 확정 후 `_parse_response()`만 조정하면 된다.
평가 로직(rag/spec_evaluator.py)은 이 클라이언트가 반환하는 정규화된 data/spec dict 형태
(`data["value"]`, `spec["lsl"]`/`spec["usl"]`)에만 의존하므로 실제 파싱만 바뀌면 나머지는
그대로 동작한다.
"""

from typing import Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import Settings, get_settings

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class AsmlApiClient(Protocol):
    async def fetch_data_and_spec(
        self, *, equipment_id: str, parameter: str
    ) -> tuple[dict, dict]: ...
    async def aclose(self) -> None: ...


def _make_retrying(max_retries: int) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )


class HttpAsmlApiClient:
    """부서 ASML API 어댑터.

    TBD: 실제 엔드포인트/인증/응답 필드명 미확정. `data`+`spec`이 결합된 JSON을
    반환하는 단일 GET 엔드포인트로 추정 구현했다.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, max_retries: int = 3):
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}
        )
        self._retrying = _make_retrying(max_retries)

    @staticmethod
    def _parse_response(payload: dict) -> tuple[dict, dict]:
        if "data" not in payload or "spec" not in payload:
            raise ValueError(
                f"알 수 없는 ASML API 응답 형식 (TBD 스펙 확정 필요): keys={list(payload.keys())}"
            )
        return payload["data"], payload["spec"]

    async def _get_measurement(self, equipment_id: str, parameter: str) -> tuple[dict, dict]:
        resp = await self._client.get(
            "/measurements", params={"equipment_id": equipment_id, "parameter": parameter}
        )
        resp.raise_for_status()
        return self._parse_response(resp.json())

    async def fetch_data_and_spec(self, *, equipment_id: str, parameter: str) -> tuple[dict, dict]:
        return await self._retrying(self._get_measurement, equipment_id, parameter)

    async def aclose(self) -> None:
        await self._client.aclose()


def get_asml_api_client(settings: Settings | None = None) -> AsmlApiClient | None:
    """워커/API 시작 시 1회 생성해서 재사용할 것 (요청마다 새로 만들지 말 것).

    ASML_API_BASE/ASML_API_KEY가 설정되지 않은 환경(예: 로컬 개발)에서는 None을
    반환한다 — /query/spec-check 호출 시점에 명확한 에러로 안내한다(api/deps.py).
    """
    settings = settings or get_settings()
    if not settings.asml_api_base or not settings.asml_api_key:
        return None
    return HttpAsmlApiClient(
        base_url=settings.asml_api_base,
        api_key=settings.asml_api_key,
        timeout=settings.asml_http_timeout_sec,
        max_retries=settings.http_retry_max_attempts,
    )
