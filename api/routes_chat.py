"""POST /chat — 대화형 탐색 인터페이스 (FR-7, CLAUDE.md Session 20).

- 핵심 판정 엔드포인트(/query/*)와 **분리된 라우터**다(FR-7.1). /query/* 에는 도구 호출
  방식을 적용하지 않는다(CLAUDE.md "하지 말아야 할 것").
- 한 턴 = 계획(mcp_server.planner) → 검증된 도구 실행(mcp_server.tools) → 도구 결과로 조립한
  답변. 로컬 LLM이 느려서(호출당 수십 초~) 진행 상황을 NDJSON 이벤트로 흘려보낸다:
    {"type":"status"} → {"type":"plan"} → ({"type":"tool_start"} → {"type":"tool_result"})*
    → {"type":"final"}   (실패 시 {"type":"error"})
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from api.deps import AsmlApiClientDep, DbPool, EmbeddingClientDep, LLMClientDep, SettingsDep
from clients.asml_api_client import AsmlApiClient
from mcp_server.planner import compose_reply, plan_turn
from mcp_server.tools import TOOL_REGISTRY, ToolContext, execute_tool
from models.schemas import ChatRequest

router = APIRouter(prefix="/chat", tags=["chat"])


def _tools_meta() -> list[dict]:
    return [
        {
            "name": spec.name,
            "signature": spec.signature,
            "description": spec.description,
            "writes_audit": spec.writes_audit,
            "may_call_llm": spec.may_call_llm,
            "input_schema": spec.as_mcp_tool()["inputSchema"],
        }
        for spec in TOOL_REGISTRY.values()
    ]


async def _load_catalog(asml_client: AsmlApiClient | None) -> tuple[dict | None, str | None]:
    if asml_client is None:
        return None, "ASML API(ftpmodule)가 설정되지 않았습니다 (ASML_API_BASE)."
    try:
        return await asml_client.fetch_catalog(), None
    except Exception as exc:  # noqa: BLE001 — 목록 없이도 대화(이력 질의 등)는 가능해야 한다
        return None, f"{type(exc).__name__}: {exc}"


@router.get("/tools")
async def chat_tools() -> dict:
    return {"tools": _tools_meta()}


@router.get("/context")
async def chat_context(asml_client: AsmlApiClientDep, settings: SettingsDep) -> dict:
    catalog, catalog_error = await _load_catalog(asml_client)
    return {
        "tools": _tools_meta(),
        "catalog": catalog,
        "catalog_error": catalog_error,
        "max_tool_calls": settings.chat_max_tool_calls,
        "margin_threshold_pct": settings.spec_check_margin_threshold_pct,
        "llm_model": settings.ollama_llm_model
        if settings.llm_provider == "ollama"
        else settings.internal_llm_model,
    }


def _line(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")


@router.post("")
async def chat(
    req: ChatRequest,
    pool: DbPool,
    embedding_client: EmbeddingClientDep,
    llm_client: LLMClientDep,
    asml_client: AsmlApiClientDep,
    settings: SettingsDep,
) -> StreamingResponse:
    ctx = ToolContext(
        pool=pool,
        embedding_client=embedding_client,
        llm_client=llm_client,
        asml_client=asml_client,
        settings=settings,
    )

    async def events() -> AsyncIterator[bytes]:
        started = time.perf_counter()
        try:
            yield _line({"type": "status", "stage": "planning"})
            catalog, catalog_error = await _load_catalog(asml_client)
            plan = await plan_turn(
                llm_client,
                req.message,
                req.history,
                catalog,
                max_calls=settings.chat_max_tool_calls,
                history_messages=settings.chat_history_messages,
            )
            yield _line(
                {
                    "type": "plan",
                    "planner": plan.planner,
                    "calls": plan.calls,
                    "reply": plan.reply,
                    "notes": plan.notes
                    + ([f"설비/항목 목록 없음: {catalog_error}"] if catalog_error else []),
                    "elapsed_sec": round(time.perf_counter() - started, 2),
                }
            )
            results = []
            for index, call in enumerate(plan.calls):
                yield _line({"type": "tool_start", "index": index, **call})
                # 클라이언트가 스트림을 끊으면 이 제너레이터는 취소된다. 도구는 판정 저장 → RAG
                # 설명 저장을 순서대로 하므로, 중간에 취소되면 spec_evaluations만 남고 judgements가
                # 빠진 반쪽 감사 기록이 생긴다(FR-6.1). shield로 도구 실행은 끝까지 완료시키고
                # 스트림만 끊는다 — /query/* 가 연결이 끊겨도 핸들러를 끝까지 도는 것과 같은 보장.
                task = asyncio.ensure_future(execute_tool(call["tool"], call["args"], ctx))
                result = await asyncio.shield(task)
                results.append(result)
                yield _line({"type": "tool_result", "index": index, **result})
            yield _line(
                {
                    "type": "final",
                    "reply": compose_reply(plan, results),
                    # 다음 턴의 문맥으로 되돌려 보낼 "실제로 성공한 호출"
                    "calls": [{"tool": r["tool"], "args": r["args"]} for r in results if r["ok"]],
                    "elapsed_sec": round(time.perf_counter() - started, 2),
                }
            )
        except Exception as exc:  # noqa: BLE001 — 스트림 도중 실패도 화면에 남긴다
            yield _line({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(events(), media_type="application/x-ndjson")
