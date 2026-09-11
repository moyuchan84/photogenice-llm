"""api/routes_chat.py — NDJSON 이벤트 순서와 배선 검증(계획/도구는 monkeypatch)."""

import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_chat
from api.deps import (
    get_asml_api_client_dep,
    get_db_pool,
    get_embedding_client_dep,
    get_llm_client_dep,
    get_settings_dep,
)
from config import Settings
from mcp_server.planner import Plan

_TEST_SETTINGS = Settings(_env_file=None, database_url="postgresql://x/x")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes_chat.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    app.dependency_overrides[get_asml_api_client_dep] = lambda: None
    app.dependency_overrides[get_settings_dep] = lambda: _TEST_SETTINGS
    return TestClient(app)


def _events(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_chat_streams_plan_tool_events_and_final_with_successful_calls(monkeypatch):
    plan = Plan(
        calls=[
            {
                "tool": "check_spec",
                "args": {"equipment_id": "MOCK-EUV-01", "parameter": "SCALE_CH1"},
            },
            {"tool": "list_equipment", "args": {}},
        ],
        planner="llm",
    )
    plan_turn = AsyncMock(return_value=plan)
    monkeypatch.setattr(routes_chat, "plan_turn", plan_turn)
    monkeypatch.setattr(
        routes_chat,
        "execute_tool",
        AsyncMock(
            side_effect=[
                {
                    "tool": "check_spec",
                    "args": plan.calls[0]["args"],
                    "ok": True,
                    "kind": "spec_check",
                    "summary": "S1",
                    "data": {},
                },
                {"tool": "list_equipment", "args": {}, "ok": False, "error": "down"},
            ]
        ),
    )

    resp = _client().post(
        "/chat",
        json={
            "message": "그럼 SCALE_CH1은?",
            "history": [
                {
                    "role": "assistant",
                    "content": "x",
                    "calls": [{"tool": "check_spec", "args": {"equipment_id": "MOCK-EUV-01"}}],
                }
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = _events(resp)
    assert [e["type"] for e in events] == [
        "status",
        "plan",
        "tool_start",
        "tool_result",
        "tool_start",
        "tool_result",
        "final",
    ]
    assert any("설비/항목 목록 없음" in n for n in events[1]["notes"])  # ASML 미설정도 대화는 진행
    final = events[-1]
    assert "S1" in final["reply"] and "⚠ list_equipment 실패: down" in final["reply"]
    assert final["calls"] == [{"tool": "check_spec", "args": plan.calls[0]["args"]}]
    history_arg = plan_turn.call_args.args[2]
    assert history_arg[0].calls[0].tool == "check_spec"


def test_chat_stream_reports_unexpected_errors_as_event(monkeypatch):
    monkeypatch.setattr(routes_chat, "plan_turn", AsyncMock(side_effect=RuntimeError("boom")))
    events = _events(_client().post("/chat", json={"message": "hi"}))
    assert events[-1] == {"type": "error", "message": "RuntimeError: boom"}


def test_chat_rejects_empty_message():
    assert _client().post("/chat", json={"message": ""}).status_code == 422


def test_chat_tools_lists_registry():
    names = [t["name"] for t in _client().get("/chat/tools").json()["tools"]]
    assert "check_spec" in names and "analyze_id_dump" in names
