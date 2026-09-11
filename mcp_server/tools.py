"""/chat 탐색 인터페이스용 도구 레지스트리 (FR-7, CLAUDE.md Session 20).

원칙 (00-requirements.md FR-7.2, CLAUDE.md "하지 말아야 할 것"):
- 도구는 **새 계산을 하지 않는다.** 이미 있는 결정론적 파이프라인(run_spec_check /
  run_id_dump_analysis / run_rag_judgement)과 검색·조회 함수를 그대로 감싸기만 한다.
- 판정(IN/OUT), margin, 수치는 도구 결과에서만 나온다. 답변 문장(summary)도 LLM이 아니라
  이 파일이 도구 결과로 조립한다 — LLM이 숫자를 재계산·재서술할 틈을 두지 않는다.
- 판정·설명을 실행하는 도구는 /query/* 와 같은 함수를 타므로 감사 레코드도 똑같이 남는다.
- 이 레지스트리는 MCP tool 정의(name/description/inputSchema)와 같은 모양으로 노출할 수 있게
  만들었다. 실제 MCP 프로토콜 서버로 띄우는 것은 사내 LLM의 tool-calling 확인 이후 과제다.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import asyncpg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clients.asml_api_client import (
    FEATURE_DOMAINS,
    AsmlApiClient,
    AsmlApiError,
    summarize_dump_item,
)
from clients.embedding_client import EmbeddingClient
from clients.llm_client import LLMClient
from config import Settings
from db.repo import fetch_chunks_by_ids, fetch_judgement, fetch_recent_evaluations
from models.schemas import SpecCheckRequest
from rag.core import run_rag_judgement
from rag.features import (
    FEATURE_REGISTRY,
    build_log_keyword_map,
    resolve_data_and_spec,
    run_spec_check,
)
from rag.id_dump import run_id_dump_analysis
from rag.retriever import RetrievedChunk, hybrid_search
from workers.chunker import classify_text

# 검색 범위 후보와 기능 목록은 레지스트리에서 만든다 — 새 기능은 FEATURE_REGISTRY 등록만으로
# /chat에도 반영된다(FR-5.1). feature_type -> domain 매핑은 ASML 어댑터의 정의를 그대로 쓴다.
SEARCH_FEATURE_TYPES = ("log_general", *FEATURE_REGISTRY)
SPEC_FEATURES = ("generic", *FEATURE_DOMAINS)
DOMAINS = tuple(dict.fromkeys(FEATURE_DOMAINS.values()))


class ToolError(Exception):
    """사용자에게 그대로 보여줄 수 있는 도구 실패 사유."""


@dataclass(frozen=True)
class ToolContext:
    pool: asyncpg.Pool
    embedding_client: EmbeddingClient
    llm_client: LLMClient
    asml_client: AsmlApiClient | None
    settings: Settings


# ---------------------------------------------------------------------------
# 인자 스키마 — LLM이 만든 인자는 여기서 검증되지 않으면 실행되지 않는다.
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class NoArgs(_Args):
    pass


class ListSpecItemsArgs(_Args):
    domain: Literal[DOMAINS] | None = None


class LatestSpecValuesArgs(_Args):
    equipment_id: str = Field(min_length=1)
    domain: Literal[DOMAINS] = DOMAINS[0]


class CheckSpecArgs(_Args):
    feature: Literal[SPEC_FEATURES] = "generic"
    equipment_id: str = Field(min_length=1)
    parameter: str = Field(min_length=1)


class AnalyzeIdDumpArgs(_Args):
    equipment_id: str = Field(min_length=1)
    error_dump_text: str | None = None


class AskHistoryArgs(_Args):
    query: str = Field(min_length=1)
    equipment_id: str | None = None
    feature_type: Literal[SEARCH_FEATURE_TYPES] | None = None


class SearchHistoryArgs(AskHistoryArgs):
    top_k: int = Field(default=5, ge=1, le=10)


class RecentEvaluationsArgs(_Args):
    equipment_id: str | None = None
    parameter: str | None = None
    only_out_of_spec: bool = False
    limit: int = Field(default=10, ge=1, le=30)


# ---------------------------------------------------------------------------
# 표시 헬퍼 (순수)
# ---------------------------------------------------------------------------


def format_criterion(criterion: dict | None) -> str:
    """ftpmodule 기준 행 -> 사람이 읽는 식. 비교는 하지 않는다."""
    if not criterion:
        return "-"
    var = "|v|" if criterion.get("is_absolute") else "v"
    text = f"{var} {criterion.get('operator')} {criterion.get('threshold')}"
    if criterion.get("operator2"):
        text += f" AND {var} {criterion.get('operator2')} {criterion.get('threshold2')}"
    return text


def _spec_expression(spec: dict) -> str:
    if spec.get("criterion"):
        return format_criterion(spec["criterion"])
    if "lsl" in spec or "usl" in spec:
        return f"{spec.get('lsl')} <= v <= {spec.get('usl')}"
    return "-"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else value


def _chunk_dict(chunk: RetrievedChunk | asyncpg.Record) -> dict:
    get = (lambda k: getattr(chunk, k)) if isinstance(chunk, RetrievedChunk) else chunk.get
    out = {
        "chunk_id": get("chunk_id"),
        "equipment_id": get("equipment_id"),
        "feature_type": get("feature_type"),
        "period_start": _iso(get("period_start")),
        "period_end": _iso(get("period_end")),
        "error_codes": list(get("error_codes") or []),
        "chunk_text": get("chunk_text"),
    }
    if isinstance(chunk, RetrievedChunk):
        out["distance"] = round(chunk.distance, 4)
    return out


async def _chunks_for(ctx: ToolContext, judgement_id: int | None) -> tuple[list[int], list[dict]]:
    if judgement_id is None:
        return [], []
    judgement = await fetch_judgement(ctx.pool, judgement_id)
    retrieved = list(judgement["retrieved_chunk_ids"] or []) if judgement else []
    return retrieved, [_chunk_dict(r) for r in await fetch_chunks_by_ids(ctx.pool, retrieved)]


def _require_asml(ctx: ToolContext) -> AsmlApiClient:
    if ctx.asml_client is None:
        raise ToolError("ASML API(ftpmodule)가 설정되지 않았습니다 (ASML_API_BASE 확인 필요).")
    return ctx.asml_client


def _rag_lines(
    conclusion: str | None, action: str | None, judgement_id: int | None, retrieved_ids: list[int]
) -> list[str]:
    if judgement_id is None:
        return []
    if not retrieved_ids:
        return ["근거가 되는 과거 사례 청크가 없어 LLM 설명을 생략했습니다(판정·질의 기록만 저장)."]
    if conclusion is None:
        return [
            "근거 청크는 찾았지만 LLM 응답에 결론(conclusion)이 없었습니다 — judgements 원문 확인 필요."
        ]
    lines = [f"원인 설명(RAG): {conclusion}"]
    if action:
        lines.append(f"권고 조치: {action}")
    return lines


def _bullets(head: str, lines: list[str]) -> str:
    return "\n".join([head, *(f"  · {line}" for line in lines)])


# ---------------------------------------------------------------------------
# 도구 구현
# ---------------------------------------------------------------------------


async def _list_equipment(ctx: ToolContext, _: NoArgs) -> dict:
    catalog = await _require_asml(ctx).fetch_catalog()
    names = catalog["equipment"]
    return {
        "kind": "equipment",
        "summary": f"ftpmodule에 등록된 설비 {len(names)}대: {', '.join(names) or '없음'}",
        "data": catalog,
    }


async def _list_spec_items(ctx: ToolContext, args: ListSpecItemsArgs) -> dict:
    client = _require_asml(ctx)
    domains = [args.domain] if args.domain else list(DOMAINS)
    rows = []
    for domain in domains:
        for row in await client.fetch_spec_criteria(domain=domain):
            rows.append({**row, "expression": format_criterion(row["criterion"])})
    counts = ", ".join(f"{d} {sum(1 for r in rows if r['domain'] == d)}개" for d in domains)
    return {
        "kind": "criteria",
        "summary": f"판정 기준표({counts}, spec_level={ctx.settings.asml_spec_level}, "
        f"{ctx.settings.asml_spec_line}/{ctx.settings.asml_spec_model}). "
        "기준식만 보여주며 판정은 하지 않습니다.",
        "data": {"rows": rows},
    }


async def _latest_spec_values(ctx: ToolContext, args: LatestSpecValuesArgs) -> dict:
    client = _require_asml(ctx)
    latest = await client.fetch_latest_spec_values(
        domain=args.domain, equipment_id=args.equipment_id
    )
    criteria = {
        row["record_name"]: format_criterion(row["criterion"])
        for row in await client.fetch_spec_criteria(domain=args.domain)
    }
    rows = [{**v, "criterion": criteria.get(v["record_name"], "-")} for v in latest["values"]]
    return {
        "kind": "values",
        "summary": (
            f"{args.equipment_id} {args.domain} 최신 측정 세대 값 {len(rows)}개 "
            f"(측정 {latest['measured_at']}, {latest['spec']}). "
            "원시값과 기준식만 나열한 표입니다 — IN/OUT 판정은 항목별 check_spec을 실행해야 "
            "결정론적으로 계산·기록됩니다."
        ),
        "data": {**latest, "values": rows},
    }


async def _check_spec(ctx: ToolContext, args: CheckSpecArgs) -> dict:
    req = SpecCheckRequest(equipment_id=args.equipment_id, parameter=args.parameter)
    try:
        data, spec = await resolve_data_and_spec(
            req, feature_type=args.feature, asml_client=ctx.asml_client
        )
    except HTTPException as exc:
        raise ToolError(f"측정값/기준 조회 실패(HTTP {exc.status_code}): {exc.detail}") from exc

    outcome = await run_spec_check(
        ctx.pool,
        ctx.embedding_client,
        ctx.llm_client,
        ctx.settings,
        feature_type=args.feature,
        equipment_id=args.equipment_id,
        parameter=args.parameter,
        data=data,
        spec=spec,
    )
    retrieved_ids, chunks = await _chunks_for(ctx, outcome.judgement_id)
    threshold = ctx.settings.spec_check_margin_threshold_pct
    expression = _spec_expression(spec)
    unit = data.get("unit") or ""
    label = spec.get("domain") or args.feature

    head = (
        f"{args.equipment_id} {args.parameter} ({label}, {spec.get('spec_level') or 'spec'}): "
        f"측정 {_num(data.get('value'))}{(' ' + unit) if unit else ''} / 기준 {expression} "
        f"→ {outcome.determination} (margin {_num(outcome.margin_pct)}%)"
    )
    lines = _rag_lines(
        outcome.conclusion, outcome.recommended_action, outcome.judgement_id, retrieved_ids
    )
    if outcome.judgement_id is None:
        lines.append(f"margin이 임계 {threshold:g}%보다 커서 LLM 설명은 호출하지 않았습니다.")
    lines.append(
        f"기록: spec_evaluations#{outcome.eval_id}"
        + (f", judgements#{outcome.judgement_id}" if outcome.judgement_id else "")
    )
    return {
        "kind": "spec_check",
        "summary": _bullets(head, lines),
        "data": {
            "feature": args.feature,
            "equipment_id": args.equipment_id,
            "parameter": args.parameter,
            "determination": outcome.determination,
            "margin_pct": outcome.margin_pct,
            "threshold_pct": threshold,
            "eval_id": outcome.eval_id,
            "judgement_id": outcome.judgement_id,
            "conclusion": outcome.conclusion,
            "confidence": outcome.confidence,
            "recommended_action": outcome.recommended_action,
            "evidence_chunk_ids": outcome.evidence_chunk_ids or [],
            "retrieved_chunk_ids": retrieved_ids,
            "chunks": chunks,
            "value": data.get("value"),
            "unit": data.get("unit"),
            "measured_at": data.get("measured_at"),
            "source": data.get("source"),
            "domain": spec.get("domain"),
            "spec_level": spec.get("spec_level"),
            "criterion_expression": expression,
            "other_levels": [
                {"spec_level": o.get("spec_level"), "expression": format_criterion(o)}
                for o in spec.get("other_levels", [])
            ],
        },
    }


async def _analyze_id_dump(ctx: ToolContext, args: AnalyzeIdDumpArgs) -> dict:
    text = args.error_dump_text
    dump_source = "user"
    if not text:
        try:
            item = await _require_asml(ctx).fetch_item(equipment_id=args.equipment_id, item="dump")
        except AsmlApiError as exc:
            raise ToolError(f"{args.equipment_id}의 ID dump를 가져오지 못했습니다: {exc}") from exc
        text = summarize_dump_item(item)
        dump_source = "ftpmodule"

    result = await run_id_dump_analysis(
        ctx.pool,
        ctx.embedding_client,
        ctx.llm_client,
        equipment_id=args.equipment_id,
        error_dump_text=text,
    )
    retrieved_ids, chunks = await _chunks_for(ctx, result.judgement_id)
    head = f"{args.equipment_id} ID dump 원인 분석 (입력: {'ftpmodule dump 요약' if dump_source == 'ftpmodule' else '사용자 제공 텍스트'})"
    lines = [f"입력 요약: {text}"]
    lines += _rag_lines(
        result.conclusion, result.recommended_action, result.judgement_id, retrieved_ids
    )
    lines.append(f"기록: judgements#{result.judgement_id}")
    return {
        "kind": "root_cause",
        "summary": _bullets(head, lines),
        "data": {
            "equipment_id": args.equipment_id,
            "error_dump_text": text,
            "dump_source": dump_source,
            "judgement_id": result.judgement_id,
            "conclusion": result.conclusion,
            "confidence": result.confidence,
            "recommended_action": result.recommended_action,
            "evidence_chunk_ids": result.evidence_chunk_ids,
            "retrieved_chunk_ids": retrieved_ids,
            "chunks": chunks,
        },
    }


def _choose_feature_type(query: str, requested: str | None) -> tuple[str, str]:
    """검색 범위(feature_type) 결정: 지정값 > 질의 어휘를 적재 청크와 같은 규칙으로 분류 >
    log_general(/query/history와 같은 기본값). 여러 기능을 합쳐 찾는 사실상 전체 검색은 하지
    않는다(CLAUDE.md: 메타데이터 필터 없는 벡터 검색 금지)."""
    if requested:
        return requested, "요청에 지정된 기능"
    by_keyword = classify_text([query], build_log_keyword_map())
    if by_keyword != "log_general":
        return by_keyword, "질의 어휘를 적재 청크와 같은 규칙으로 분류"
    return "log_general", "기능 키워드가 없어 UC1 기본 범위"


async def _ask_history(ctx: ToolContext, args: AskHistoryArgs) -> dict:
    feature_type, reason = _choose_feature_type(args.query, args.feature_type)
    config = FEATURE_REGISTRY.get(feature_type)
    result = await run_rag_judgement(
        ctx.pool,
        ctx.embedding_client,
        ctx.llm_client,
        use_case="history",
        query=args.query,
        feature_type=feature_type,
        equipment_id=args.equipment_id,
        prompt_context=config.prompt_context if config else None,
    )
    retrieved_ids, chunks = await _chunks_for(ctx, result.judgement_id)
    head = f"이력 질의 ({args.equipment_id or '전체 설비'}, 검색 범위 feature_type={feature_type} — {reason})"
    lines = _rag_lines(
        result.conclusion, result.recommended_action, result.judgement_id, retrieved_ids
    )
    lines.append(f"기록: judgements#{result.judgement_id}")
    return {
        "kind": "history",
        "summary": _bullets(head, lines),
        "data": {
            "query": args.query,
            "equipment_id": args.equipment_id,
            "feature_type": feature_type,
            "feature_reason": reason,
            "judgement_id": result.judgement_id,
            "conclusion": result.conclusion,
            "confidence": result.confidence,
            "recommended_action": result.recommended_action,
            "evidence_chunk_ids": result.evidence_chunk_ids,
            "retrieved_chunk_ids": retrieved_ids,
            "chunks": chunks,
        },
    }


async def _search_history(ctx: ToolContext, args: SearchHistoryArgs) -> dict:
    feature_type, reason = _choose_feature_type(args.query, args.feature_type)
    [embedding] = await ctx.embedding_client.embed([args.query])
    chunks = await hybrid_search(
        ctx.pool,
        embedding,
        feature_type=feature_type,
        equipment_id=args.equipment_id,
        top_k=args.top_k,
    )
    rows = [_chunk_dict(c) for c in chunks]
    if rows:
        top = rows[0]
        head = (
            f"관련 이력 청크 {len(rows)}건 ({args.equipment_id or '전체 설비'}, "
            f"feature_type={feature_type} — {reason}). "
            f"가장 가까운 것: #{top['chunk_id']} {top['feature_type']} (거리 {top['distance']})"
        )
    else:
        head = f"조건에 맞는 이력 청크가 없습니다 (feature_type={feature_type} — {reason})."
    return {
        "kind": "search",
        "summary": head + " — 검색만 수행했고 LLM 설명은 만들지 않았습니다.",
        "data": {"chunks": rows, "feature_type": feature_type, "feature_reason": reason},
    }


async def _recent_evaluations(ctx: ToolContext, args: RecentEvaluationsArgs) -> dict:
    records = await fetch_recent_evaluations(
        ctx.pool,
        equipment_id=args.equipment_id,
        parameter=args.parameter,
        only_out_of_spec=args.only_out_of_spec,
        limit=args.limit,
    )
    rows = [{k: _iso(v) for k, v in dict(r).items()} for r in records]
    oos = sum(1 for r in rows if r["determination"] == "OUT_OF_SPEC")
    scope = " ".join(filter(None, [args.equipment_id, args.parameter])) or "전체"
    summary = f"저장된 판정 이력 {len(rows)}건 ({scope}{', OUT_OF_SPEC만' if args.only_out_of_spec else ''}; OUT_OF_SPEC {oos}건)"
    if rows:
        latest = rows[0]
        summary += (
            f". 최근: {latest['equipment_id']} {latest['parameter']} → {latest['determination']} "
            f"(margin {_num(latest['margin_pct'])}%, eval#{latest['eval_id']})"
        )
    return {"kind": "evaluations", "summary": summary, "data": {"rows": rows}}


# ---------------------------------------------------------------------------
# 레지스트리
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    signature: str
    description: str
    args_model: type[_Args]
    handler: Callable[[ToolContext, _Args], Awaitable[dict]]
    writes_audit: bool = False
    may_call_llm: bool = False

    def as_mcp_tool(self) -> dict:
        """MCP tool 정의와 같은 모양 {name, description, inputSchema}."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.args_model.model_json_schema(),
        }


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "list_equipment",
            "list_equipment()",
            "ftpmodule에 등록된 설비(servername) 목록",
            NoArgs,
            _list_equipment,
        ),
        ToolSpec(
            "list_spec_items",
            "list_spec_items(domain?: FOCAL|OVERLAY)",
            "판정 기준표(항목별 기준식). 판정하지 않음",
            ListSpecItemsArgs,
            _list_spec_items,
        ),
        ToolSpec(
            "get_latest_spec_values",
            "get_latest_spec_values(equipment_id, domain: FOCAL|OVERLAY)",
            "설비의 최신 측정 세대 원시값 전체와 기준식. 판정하지 않음",
            LatestSpecValuesArgs,
            _latest_spec_values,
        ),
        ToolSpec(
            "check_spec",
            "check_spec(feature: focal_curve|final_xy|generic, equipment_id, parameter)",
            "항목 1개를 결정론적으로 판정하고 기록. OUT_OF_SPEC/근접이면 과거 사례 RAG 설명",
            CheckSpecArgs,
            _check_spec,
            writes_audit=True,
            may_call_llm=True,
        ),
        ToolSpec(
            "analyze_id_dump",
            "analyze_id_dump(equipment_id, error_dump_text?)",
            "ID dump 원인 분석(항상 RAG). 텍스트가 없으면 ftpmodule dump를 요약해 사용",
            AnalyzeIdDumpArgs,
            _analyze_id_dump,
            writes_audit=True,
            may_call_llm=True,
        ),
        ToolSpec(
            "ask_history",
            "ask_history(query, equipment_id?, feature_type?)",
            "과거 이력 자연어 질의 — 검색 + LLM 결론(기록됨)",
            AskHistoryArgs,
            _ask_history,
            writes_audit=True,
            may_call_llm=True,
        ),
        ToolSpec(
            "search_history",
            "search_history(query, equipment_id?, feature_type?, top_k?)",
            "과거 이력 청크 검색만(LLM 없음)",
            SearchHistoryArgs,
            _search_history,
        ),
        ToolSpec(
            "get_recent_evaluations",
            "get_recent_evaluations(equipment_id?, parameter?, only_out_of_spec?, limit?)",
            "이미 저장된 판정/설명 이력 조회",
            RecentEvaluationsArgs,
            _recent_evaluations,
        ),
    )
}


def validate_args(name: str, raw_args: dict | None) -> _Args:
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"알 수 없는 도구입니다: {name!r}")
    try:
        return spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(args)'}: {e['msg']}" for e in exc.errors()
        )
        raise ToolError(f"{name} 인자가 올바르지 않습니다 — {problems}") from exc


async def execute_tool(name: str, raw_args: dict | None, ctx: ToolContext) -> dict:
    """검증 → 실행 → {tool, args, ok, kind, summary, data, error, elapsed_sec}. 예외를 밖으로
    던지지 않는다(한 도구의 실패가 대화 턴 전체를 끊지 않도록)."""
    started = time.perf_counter()
    result: dict = {"tool": name, "args": raw_args or {}, "ok": False}
    try:
        args = validate_args(name, raw_args)
        result["args"] = args.model_dump(exclude_none=True)
        output = await TOOL_REGISTRY[name].handler(ctx, args)
        result.update(ok=True, **output)
    except ToolError as exc:
        result["error"] = str(exc)
    except AsmlApiError as exc:
        result["error"] = f"ASML API(ftpmodule) 오류: {exc}"
    except Exception as exc:  # noqa: BLE001 — 대화는 계속되어야 하고, 원인은 화면에 남긴다
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["elapsed_sec"] = round(time.perf_counter() - started, 2)
    return result
