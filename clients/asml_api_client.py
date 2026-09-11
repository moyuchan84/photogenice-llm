"""ASML API(부서 fleet 백엔드 = ftpmodule) 호출 어댑터.

실제 계약(ftpmodule/README.api.md, SPEC §3.12~3.13)은 data+spec 결합 응답이 아니라
여러 호출의 조합이다. 하나의 (equipment_id, parameter) 판정 입력을 만들기 위해:

  1. GET  /spec/map?domain=&line=&model=&model_type=   -> parameter(=record_name)의 기준
  2. GET  /servers                                     -> servername == equipment_id 인 서버 id
  3. POST /servers/{id}/list        {"set_name": anchor} -> 최신 측정 파일(mtime 최대)
  4. POST /servers/{id}/items/{anchor} {"files": [..]}  -> <spec item>_pairs 짝 이름표
  5. POST /servers/{id}/items/{spec item} {"files": [..]} -> tag_value 레코드(name == parameter)

domain/anchor/spec item 매핑: FOCAL = focal/focalspec, OVERLAY = overlay/overlayspec.
ftpmodule은 판정을 계산하지 않으므로(§3.13 "No verdict is computed") 이 어댑터는
값과 기준을 정규화해서 넘기기만 하고, 판정은 rag/spec_evaluator.py::evaluate_criterion
(순수 함수)이 한다.

[TBD — 부서 확인 필요, 확정 시 이 파일만 조정]
- equipment_id ↔ servers.servername 동일 취급(ftpmodule 결정: servername이 장비 정체성)
- line/model/model_type/spec_level: servers 표에 컬럼이 없어 설정값(ASML_SPEC_*)으로 고정
- "최신 측정" = anchor set에서 mtime이 가장 최근인 파일 한 세대
- 같은 name의 tag_value가 여러 개(occurrence/index)면 (occurrence, index) 최소값을 쓴다
- focal_curve 기능이 focalspec 값(SCALE/ROTATION/…)을 판정한다는 해석 자체
"""

from typing import Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from config import Settings, get_settings


class AsmlApiError(Exception):
    """ASML API 어댑터 오류의 공통 부모. 라우터는 이 계층만 보고 HTTP 코드로 옮긴다."""


class AsmlNotFoundError(AsmlApiError):
    """요청한 설비/항목/기준/측정 파일이 부서 API에 없다 (-> 404)."""


class AsmlDataError(AsmlApiError):
    """부서 API 응답은 왔지만 판정 입력으로 쓸 수 없다(짝 없음, 비숫자 값 등) (-> 502)."""


class AsmlUpstreamError(AsmlApiError):
    """재시도 후에도 부서 API 호출 자체가 실패했다 (-> 502)."""


class AsmlApiClient(Protocol):
    async def fetch_data_and_spec(
        self, *, equipment_id: str, parameter: str
    ) -> tuple[dict, dict]: ...
    async def fetch_feature_data_and_spec(
        self, *, feature_type: str, equipment_id: str, parameter: str
    ) -> tuple[dict, dict]: ...
    async def fetch_item(self, *, equipment_id: str, item: str) -> dict: ...
    async def fetch_catalog(self) -> dict: ...
    async def fetch_spec_criteria(self, *, domain: str) -> list[dict]: ...
    async def fetch_latest_spec_values(self, *, domain: str, equipment_id: str) -> dict: ...
    async def aclose(self) -> None: ...


# domain -> (측정 anchor item, 짝 spec item)
_DOMAIN_ITEMS = {"FOCAL": ("focal", "focalspec"), "OVERLAY": ("overlay", "overlayspec")}
# feature_type -> ftpmodule 기준 domain. /chat 도구도 이 매핑을 그대로 쓴다(중복 정의 금지).
FEATURE_DOMAINS = {"focal_curve": "FOCAL", "final_xy": "OVERLAY"}
_FEATURE_DOMAINS = FEATURE_DOMAINS
# generic(/query/spec-check)은 parameter가 어느 domain 기준에 있는지 이 순서로 찾는다.
_GENERIC_DOMAIN_ORDER = ("FOCAL", "OVERLAY")
_CRITERION_KEYS = ("operator", "threshold", "operator2", "threshold2", "is_absolute")


def _is_retryable(exc: BaseException) -> bool:
    # 4xx(없는 서버·아이템, 422 해석 불가 등)는 다시 불러도 같은 답이라 재시도하지 않는다.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _make_retrying(max_retries: int) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# 응답 파싱 — 순수 함수(유닛테스트 대상). 부서 계약이 바뀌면 여기만 고친다.
# ---------------------------------------------------------------------------


def pick_server_id(servers: list[dict], equipment_id: str) -> int:
    for server in servers:
        if server.get("servername") == equipment_id:
            return server["id"]
    raise AsmlNotFoundError(f"ASML API에 등록되지 않은 설비입니다: servername={equipment_id!r}")


def pick_criterion(spec_map: dict, parameter: str, spec_level: str) -> tuple[dict, list[dict]]:
    """/spec/map 응답에서 parameter의 spec_level 기준 1행과, 같은 항목의 전체 기준 목록."""
    entry = (spec_map.get("map") or {}).get(parameter)
    if not entry or not entry.get("criteria"):
        raise AsmlNotFoundError(
            f"기준정보가 없습니다: domain={spec_map.get('domain')} record_name={parameter!r} "
            f"filters={spec_map.get('filters')}"
        )
    criteria = entry["criteria"]
    for row in criteria:
        if row.get("spec_level", "") == spec_level:
            return row, criteria
    levels = sorted({row.get("spec_level", "") for row in criteria})
    raise AsmlNotFoundError(
        f"{parameter!r}에 spec_level={spec_level!r} 기준이 없습니다 (있는 값: {levels})"
    )


def pick_latest_file(listing: dict, set_name: str) -> dict:
    """/servers/{id}/list 응답에서 mtime이 가장 최근인 수신된 파일 세대."""
    entries = [
        e
        for e in (listing.get("listing") or {}).get(set_name, [])
        if e.get("type") == "file" and e.get("mtime") and not e.get("missing_at")
    ]
    if not entries:
        raise AsmlNotFoundError(f"{set_name} 측정 파일이 없습니다")
    return max(entries, key=lambda e: e["mtime"])


def pick_pair(anchor_item: dict, spec_item: str, anchor_name: str) -> tuple[str, dict]:
    """anchor item 응답의 <spec_item>_pairs에서 짝 spec 파일 이름과, 서빙된 anchor 세대."""
    served = next((s for s in anchor_item.get("served", []) if s.get("file") == anchor_name), None)
    if served is None:
        raise AsmlDataError(
            f"{anchor_name} 세대가 서빙되지 않았습니다 (issues={anchor_item.get('issues')})"
        )
    pair = (anchor_item.get(f"{spec_item}_pairs") or {}).get(anchor_name)
    if not pair:
        raise AsmlDataError(
            f"{anchor_name}의 {spec_item} 짝 파일이 없습니다 (issues={anchor_item.get('issues')})"
        )
    return pair, served


def first_tag_values(spec_item: dict) -> dict[str, dict]:
    """name별 대표 tag_value 레코드 — 같은 name이 여러 위치에 있으면 (occurrence, index)
    최소값. 판정 입력(pick_tag_value)과 값 목록(fetch_latest_spec_values)이 **같은 레코드**를
    보도록 선택 규칙을 이 함수 한 곳에 둔다."""
    chosen: dict[str, dict] = {}
    for r in spec_item.get("records", []):
        if r.get("kind") != "tag_value" or not r.get("name"):
            continue
        key = (r.get("occurrence") or 0, r.get("index") or 0)
        current = chosen.get(r["name"])
        if current is None or key < (current.get("occurrence") or 0, current.get("index") or 0):
            chosen[r["name"]] = r
    return chosen


def pick_tag_value(spec_item: dict, parameter: str) -> tuple[dict, dict | None]:
    """spec item 응답에서 name == parameter인 tag_value 레코드와 그 파일의 서빙 세대."""
    record = first_tag_values(spec_item).get(parameter)
    if record is None:
        raise AsmlNotFoundError(
            f"{spec_item.get('item')} 레코드에 {parameter!r} 값이 없습니다 "
            f"(issues={spec_item.get('issues')})"
        )
    if isinstance(record.get("value"), bool) or not isinstance(record.get("value"), int | float):
        raise AsmlDataError(f"{parameter!r} 값이 숫자가 아닙니다: {record.get('value')!r}")
    served = next(
        (s for s in spec_item.get("served", []) if s.get("file") == record.get("source")), None
    )
    return record, served


def summarize_dump_item(item: dict) -> str:
    """dump item 응답 -> ID Dump 분석 질의 텍스트. 스캔 메타의 미사용 detector와 신호가 전부
    0인 raw 채널만 뽑는다(판정이 아니라 사람이 읽는 요약 — 원인 추정은 RAG가 한다).
    [TBD] 부서가 실제로 보는 덤프 필드가 확정되면 여기만 바꾼다."""
    lines = []
    for rec in item.get("records", []):
        if "results" in rec:
            unused = [r.get("detector_id") for r in rec["results"] if r.get("used") is False]
            lines.append(
                f"{rec.get('scan_enum')} chuck={rec.get('chuck_id')} sensor={rec.get('sensor_id')} "
                f"timestamp={rec.get('timestamp')} 미사용 detector={unused or '없음'}"
            )
        elif isinstance(rec.get("value"), dict):
            zero = [
                channel
                for channel, values in rec["value"].items()
                if values and all(v == 0 for v in values)
            ]
            if zero:
                lines.append(f"scan_raw 신호 0 채널={zero} ({rec.get('source')})")
    return "ID dump 요약: " + " / ".join(lines) if lines else "ID dump 레코드 없음"


def _ref(served: dict | None, fallback_name: str) -> str:
    return f"{served['file']}@{served['sig']}" if served else fallback_name


def build_data_and_spec(
    *,
    domain: str,
    equipment_id: str,
    server_id: int,
    parameter: str,
    criterion: dict,
    all_criteria: list[dict],
    anchor_item: str,
    anchor_served: dict,
    spec_item: str,
    tag_value: dict,
    spec_served: dict | None,
    spec_filters: dict,
) -> tuple[dict, dict]:
    """evaluator 입력 형태로 정규화한다. 배열(curve/overlay 벡터)은 넣지 않는다 — 감사
    레코드(raw_data_json)와 LLM 설명 질의에 그대로 실리므로 출처 식별자만 남긴다."""
    data = {
        "value": float(tag_value["value"]),
        "unit": tag_value.get("unit"),
        # 측정 이벤트 시각 = anchor 파일 세대의 서버 mtime(KST aware, ftpmodule SPEC §3.4)
        "measured_at": anchor_served.get("mtime"),
        "source": {
            "servername": equipment_id,
            "server_id": server_id,
            "anchor": f"{anchor_item}:{_ref(anchor_served, anchor_served.get('file', ''))}",
            "spec": f"{spec_item}:{_ref(spec_served, tag_value.get('source', ''))}",
            "tag": tag_value.get("tag"),
            "occurrence": tag_value.get("occurrence"),
            "index": tag_value.get("index"),
        },
    }
    criterion_keys = _CRITERION_KEYS
    spec = {
        "record_name": parameter,
        "domain": domain,
        **spec_filters,
        "spec_level": criterion.get("spec_level", ""),
        "criterion": {k: criterion.get(k) for k in criterion_keys},
        "other_levels": [
            {"spec_level": row.get("spec_level", ""), **{k: row.get(k) for k in criterion_keys}}
            for row in all_criteria
            if row is not criterion
        ],
    }
    return data, spec


class HttpAsmlApiClient:
    """부서 fleet(ftpmodule) API 어댑터. 호출마다 tenacity 재시도(5xx/전송 오류만)."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        *,
        item_source: str = "cache",
        spec_line: str = "COMMON",
        spec_model: str = "EUV",
        spec_model_type: str = "ALL",
        spec_level: str = "verify",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        # ftpmodule API 자체는 인증이 없다 — 앞단 게이트웨이가 요구할 때만 헤더를 싣는다.
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, headers=headers, transport=transport
        )
        self._retrying = _make_retrying(max_retries)
        # source=cache: 부서 API가 우리 요청 때문에 설비 FTP에 접속하지 않도록(FTP 0회 보장).
        # 스윕이 선수집한 세대만 쓴다.
        self._item_source = item_source
        self._spec_filters = {"line": spec_line, "model": spec_model, "model_type": spec_model_type}
        self._spec_level = spec_level

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        async def call() -> dict | list:
            resp = await self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp.json()

        try:
            return await self._retrying(call)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:300]
            if status == 404:
                raise AsmlNotFoundError(f"{method} {path} -> 404: {detail}") from exc
            raise AsmlUpstreamError(f"{method} {path} -> {status}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise AsmlUpstreamError(f"{method} {path} 호출 실패: {exc!r}") from exc

    async def _spec_map(self, domain: str) -> dict:
        return await self._request(
            "GET", "/spec/map", params={"domain": domain, **self._spec_filters}
        )

    async def _server_id(self, equipment_id: str) -> int:
        return pick_server_id(await self._request("GET", "/servers"), equipment_id)

    async def _run_item(self, server_id: int, item: str, files: list[str] | None) -> dict:
        body: dict = {"source": self._item_source}
        if files is not None:
            body["files"] = files
        return await self._request("POST", f"/servers/{server_id}/items/{item}", json=body)

    async def _latest_spec_item(self, domain: str, equipment_id: str) -> dict:
        """최신 anchor 세대 -> 짝 spec 파일 -> spec item 응답까지(판정 입력의 공통 앞단)."""
        anchor_item, spec_item = _DOMAIN_ITEMS[domain]
        server_id = await self._server_id(equipment_id)
        listing = await self._request(
            "POST", f"/servers/{server_id}/list", json={"set_name": anchor_item}
        )
        latest = pick_latest_file(listing, anchor_item)
        anchor = await self._run_item(server_id, anchor_item, [latest["name"]])
        pair_name, anchor_served = pick_pair(anchor, spec_item, latest["name"])
        spec_resp = await self._run_item(server_id, spec_item, [pair_name])
        return {
            "server_id": server_id,
            "anchor_item": anchor_item,
            "anchor_served": anchor_served,
            "spec_item": spec_item,
            "spec_resp": spec_resp,
        }

    async def _fetch_domain(
        self, domain: str, equipment_id: str, parameter: str, spec_map: dict | None = None
    ) -> tuple[dict, dict]:
        spec_map = spec_map if spec_map is not None else await self._spec_map(domain)
        criterion, all_criteria = pick_criterion(spec_map, parameter, self._spec_level)

        latest = await self._latest_spec_item(domain, equipment_id)
        server_id = latest["server_id"]
        anchor_item, anchor_served = latest["anchor_item"], latest["anchor_served"]
        spec_item = latest["spec_item"]
        tag_value, spec_served = pick_tag_value(latest["spec_resp"], parameter)

        return build_data_and_spec(
            domain=domain,
            equipment_id=equipment_id,
            server_id=server_id,
            parameter=parameter,
            criterion=criterion,
            all_criteria=all_criteria,
            anchor_item=anchor_item,
            anchor_served=anchor_served,
            spec_item=spec_item,
            tag_value=tag_value,
            spec_served=spec_served,
            spec_filters=self._spec_filters,
        )

    async def fetch_data_and_spec(self, *, equipment_id: str, parameter: str) -> tuple[dict, dict]:
        for domain in _GENERIC_DOMAIN_ORDER:
            spec_map = await self._spec_map(domain)
            if parameter in (spec_map.get("map") or {}):
                return await self._fetch_domain(domain, equipment_id, parameter, spec_map)
        raise AsmlNotFoundError(
            f"어느 domain({', '.join(_GENERIC_DOMAIN_ORDER)})에도 {parameter!r} 기준이 없습니다"
        )

    async def fetch_feature_data_and_spec(
        self, *, feature_type: str, equipment_id: str, parameter: str
    ) -> tuple[dict, dict]:
        domain = _FEATURE_DOMAINS.get(feature_type)
        if domain is None:
            raise AsmlNotFoundError(f"ASML API 매핑이 없는 feature_type입니다: {feature_type!r}")
        return await self._fetch_domain(domain, equipment_id, parameter)

    async def fetch_item(self, *, equipment_id: str, item: str) -> dict:
        """item 원본 응답(예: dump). ID Dump 분석 입력 텍스트를 만드는 호출자가 쓴다."""
        return await self._run_item(await self._server_id(equipment_id), item, None)

    async def fetch_catalog(self) -> dict:
        """등록 설비(servername)와 domain별 기준이 있는 항목(record_name) 목록 — 입력 보조용."""
        servers = await self._request("GET", "/servers")
        parameters = {}
        for domain in _DOMAIN_ITEMS:
            spec_map = await self._spec_map(domain)
            parameters[domain] = sorted((spec_map.get("map") or {}).keys())
        return {
            "equipment": sorted(s["servername"] for s in servers),
            "parameters": parameters,
            "spec_filters": {**self._spec_filters, "spec_level": self._spec_level},
        }

    async def fetch_spec_criteria(self, *, domain: str) -> list[dict]:
        """domain의 항목별 판정 기준(설정된 spec_level 행). 기준표 조회용 — 판정하지 않는다."""
        if domain not in _DOMAIN_ITEMS:
            raise AsmlNotFoundError(f"알 수 없는 domain입니다: {domain!r}")
        spec_map = await self._spec_map(domain)
        rows = []
        for name in sorted((spec_map.get("map") or {}).keys()):
            try:
                criterion, all_criteria = pick_criterion(spec_map, name, self._spec_level)
            except AsmlNotFoundError:
                continue
            rows.append(
                {
                    "domain": domain,
                    "record_name": name,
                    "spec_level": criterion.get("spec_level", ""),
                    "criterion": {k: criterion.get(k) for k in _CRITERION_KEYS},
                    "levels": sorted({row.get("spec_level", "") for row in all_criteria}),
                }
            )
        return rows

    async def fetch_latest_spec_values(self, *, domain: str, equipment_id: str) -> dict:
        """최신 측정 세대의 spec 원시값 전부(tag_value). **판정 결과를 포함하지 않는다** —
        판정은 항목별로 run_spec_check를 거쳐야 spec_evaluations에 남기 때문이다."""
        if domain not in _DOMAIN_ITEMS:
            raise AsmlNotFoundError(f"알 수 없는 domain입니다: {domain!r}")
        latest = await self._latest_spec_item(domain, equipment_id)
        spec_resp = latest["spec_resp"]
        values = {
            name: {"value": rec.get("value"), "unit": rec.get("unit")}
            for name, rec in first_tag_values(spec_resp).items()
        }
        spec_served = (spec_resp.get("served") or [None])[0]
        return {
            "domain": domain,
            "equipment_id": equipment_id,
            "measured_at": latest["anchor_served"].get("mtime"),
            "anchor": f"{latest['anchor_item']}:{_ref(latest['anchor_served'], '')}",
            "spec": f"{latest['spec_item']}:{_ref(spec_served, '')}",
            "values": [{"record_name": name, **v} for name, v in sorted(values.items())],
        }

    async def aclose(self) -> None:
        await self._client.aclose()


def get_asml_api_client(settings: Settings | None = None) -> AsmlApiClient | None:
    """워커/API 시작 시 1회 생성해서 재사용할 것 (요청마다 새로 만들지 말 것).

    ASML_API_BASE가 설정되지 않은 환경에서는 None을 반환한다 — identifier 조회가 필요한
    시점에 503으로 안내한다(rag/features.py::resolve_data_and_spec). inline 경로는 영향 없음.
    """
    settings = settings or get_settings()
    if not settings.asml_api_base:
        return None
    return HttpAsmlApiClient(
        base_url=settings.asml_api_base,
        api_key=settings.asml_api_key,
        timeout=settings.asml_http_timeout_sec,
        max_retries=settings.http_retry_max_attempts,
        item_source=settings.asml_item_source,
        spec_line=settings.asml_spec_line,
        spec_model=settings.asml_spec_model,
        spec_model_type=settings.asml_spec_model_type,
        spec_level=settings.asml_spec_level,
    )
