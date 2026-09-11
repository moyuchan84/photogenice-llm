"""mcp_server/tools.py — 도구가 기존 파이프라인을 그대로 감싸고, 요약 문장이 도구 결과에서만
만들어지는지 검증한다(DB/네트워크 없음 — 파이프라인 함수는 monkeypatch)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from config import Settings
from mcp_server import tools
from rag.core import RagJudgementResult
from rag.features import SpecCheckOutcome
from rag.retriever import RetrievedChunk

SETTINGS = Settings(_env_file=None, database_url="postgresql://x/x")


@dataclass
class _Embedding:
    calls: int = 0

    async def embed(self, texts):
        self.calls += 1
        return [[0.0] * 1024 for _ in texts]


def _ctx(asml=None, embedding=None) -> tools.ToolContext:
    return tools.ToolContext(
        pool=None,
        embedding_client=embedding or _Embedding(),
        llm_client=None,
        asml_client=asml,
        settings=SETTINGS,
    )


def _chunk(chunk_id: int, feature_type: str, distance: float) -> RetrievedChunk:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    return RetrievedChunk(
        chunk_id, "MOCK-EUV-01", "text", [1], now, now, [], feature_type, distance
    )


@pytest.mark.parametrize(
    ("criterion", "text"),
    [
        ({"is_absolute": True, "operator": "<", "threshold": 0.5, "operator2": ""}, "|v| < 0.5"),
        (
            {
                "is_absolute": False,
                "operator": "<",
                "threshold": 3.0,
                "operator2": ">=",
                "threshold2": 0.0,
            },
            "v < 3.0 AND v >= 0.0",
        ),
        (None, "-"),
    ],
)
def test_format_criterion(criterion, text):
    assert tools.format_criterion(criterion) == text


def test_registry_exposes_mcp_shaped_definitions():
    definition = tools.TOOL_REGISTRY["check_spec"].as_mcp_tool()
    assert definition["name"] == "check_spec"
    assert set(definition["inputSchema"]["properties"]) == {"feature", "equipment_id", "parameter"}


async def test_execute_tool_reports_unknown_tool_and_bad_args_without_raising():
    unknown = await tools.execute_tool("drop_table", {}, _ctx())
    assert unknown["ok"] is False and "알 수 없는 도구" in unknown["error"]
    bad = await tools.execute_tool("check_spec", {"feature": "nope"}, _ctx())
    assert bad["ok"] is False and "인자가 올바르지 않습니다" in bad["error"]


async def test_check_spec_wraps_existing_pipeline_and_summarizes_from_results(monkeypatch):
    data = {
        "value": 0.62,
        "unit": "ppm",
        "measured_at": "2026-09-10T14:30:05+09:00",
        "source": {"anchor": "a"},
    }
    spec = {
        "domain": "FOCAL",
        "spec_level": "verify",
        "criterion": {"is_absolute": True, "operator": "<", "threshold": 0.5, "operator2": ""},
        "other_levels": [
            {"spec_level": "update", "is_absolute": True, "operator": "<", "threshold": 0.3}
        ],
    }
    resolve = AsyncMock(return_value=(data, spec))
    run = AsyncMock(
        return_value=SpecCheckOutcome(
            eval_id=6,
            determination="OUT_OF_SPEC",
            margin_pct=-24.0,
            judgement_id=7,
            conclusion="레벨링 센서 드리프트",
            confidence=0.7,
            recommended_action="재캘리브레이션",
            evidence_chunk_ids=[7],
        )
    )
    monkeypatch.setattr(tools, "resolve_data_and_spec", resolve)
    monkeypatch.setattr(tools, "run_spec_check", run)
    monkeypatch.setattr(
        tools, "fetch_judgement", AsyncMock(return_value={"retrieved_chunk_ids": [7, 9]})
    )
    monkeypatch.setattr(tools, "fetch_chunks_by_ids", AsyncMock(return_value=[]))

    result = await tools.execute_tool(
        "check_spec",
        {"feature": "focal_curve", "equipment_id": "MOCK-EUV-01", "parameter": "SCALE_CH1"},
        _ctx(),
    )

    assert result["ok"] is True
    assert resolve.call_args.kwargs["feature_type"] == "focal_curve"
    assert run.call_args.kwargs["data"] is data
    assert "측정 0.62 ppm / 기준 |v| < 0.5 → OUT_OF_SPEC (margin -24%)" in result["summary"]
    assert "원인 설명(RAG): 레벨링 센서 드리프트" in result["summary"]
    assert result["data"]["retrieved_chunk_ids"] == [7, 9]
    assert result["data"]["other_levels"] == [{"spec_level": "update", "expression": "|v| < 0.3"}]


async def test_check_spec_without_rag_says_llm_was_skipped(monkeypatch):
    monkeypatch.setattr(
        tools,
        "resolve_data_and_spec",
        AsyncMock(
            return_value=(
                {"value": 0.12, "unit": "ppm"},
                {"criterion": {"is_absolute": True, "operator": "<=", "threshold": 1.0}},
            )
        ),
    )
    monkeypatch.setattr(
        tools,
        "run_spec_check",
        AsyncMock(return_value=SpecCheckOutcome(8, "IN_SPEC", 88.0, None, None, None, None, None)),
    )
    result = await tools.execute_tool(
        "check_spec", {"equipment_id": "MOCK-EUV-01", "parameter": "TRANSLATION_CH1"}, _ctx()
    )
    assert "IN_SPEC (margin 88%)" in result["summary"]
    assert "LLM 설명은 호출하지 않았습니다" in result["summary"]
    assert "원인 설명" not in result["summary"]


async def test_check_spec_turns_http_error_into_tool_error(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        tools,
        "resolve_data_and_spec",
        AsyncMock(side_effect=HTTPException(404, "기준정보가 없습니다")),
    )
    result = await tools.execute_tool("check_spec", {"equipment_id": "E", "parameter": "P"}, _ctx())
    assert result["ok"] is False
    assert "HTTP 404" in result["error"] and "기준정보가 없습니다" in result["error"]


async def test_latest_values_never_reports_a_verdict():
    class _Asml:
        async def fetch_latest_spec_values(self, *, domain, equipment_id):
            return {
                "domain": domain,
                "equipment_id": equipment_id,
                "measured_at": "2026-09-10T14:30:05+09:00",
                "anchor": "focal:a",
                "spec": "focalspec:b",
                "values": [{"record_name": "SCALE_CH1", "value": 0.62, "unit": "ppm"}],
            }

        async def fetch_spec_criteria(self, *, domain):
            return [
                {
                    "record_name": "SCALE_CH1",
                    "criterion": {"is_absolute": True, "operator": "<", "threshold": 0.5},
                }
            ]

    result = await tools.execute_tool(
        "get_latest_spec_values",
        {"equipment_id": "MOCK-EUV-01", "domain": "FOCAL"},
        _ctx(asml=_Asml()),
    )
    assert result["data"]["values"] == [
        {"record_name": "SCALE_CH1", "value": 0.62, "unit": "ppm", "criterion": "|v| < 0.5"}
    ]
    assert "OUT_OF_SPEC" not in str(result["data"]) and "IN_SPEC" not in str(result["data"])


async def test_tools_needing_ftpmodule_fail_cleanly_when_unconfigured():
    result = await tools.execute_tool("list_equipment", {}, _ctx(asml=None))
    assert result["ok"] is False and "ASML API" in result["error"]


async def test_ask_history_classifies_query_like_ingest_before_searching(monkeypatch):
    rag = AsyncMock(return_value=RagJudgementResult(11, "chuck 누설", 0.8, "클리닝", [8], []))
    monkeypatch.setattr(tools, "run_rag_judgement", rag)
    monkeypatch.setattr(
        tools, "fetch_judgement", AsyncMock(return_value={"retrieved_chunk_ids": [8]})
    )
    monkeypatch.setattr(tools, "fetch_chunks_by_ids", AsyncMock(return_value=[]))
    embedding = _Embedding()

    result = await tools.execute_tool(
        "ask_history",
        {"query": "chuck 누설로 overlay 문제 있었어?", "equipment_id": "MOCK-EUV-01"},
        _ctx(embedding=embedding),
    )

    assert rag.call_args.kwargs["feature_type"] == "final_xy"
    assert rag.call_args.kwargs["use_case"] == "history"
    assert embedding.calls == 0  # 키워드로 정해졌으니 기능 탐색 검색을 하지 않는다
    assert "feature_type=final_xy" in result["summary"]


async def test_ask_history_without_keywords_uses_uc1_default_scope(monkeypatch):
    rag = AsyncMock(return_value=RagJudgementResult(12, None, 0.0, None, [], []))
    monkeypatch.setattr(tools, "run_rag_judgement", rag)
    monkeypatch.setattr(
        tools, "fetch_judgement", AsyncMock(return_value={"retrieved_chunk_ids": []})
    )
    monkeypatch.setattr(tools, "fetch_chunks_by_ids", AsyncMock(return_value=[]))

    result = await tools.execute_tool("ask_history", {"query": "커넥터 산화 사례"}, _ctx())

    assert rag.call_args.kwargs["feature_type"] == "log_general"  # /query/history와 같은 기본값
    assert "LLM 설명을 생략" in result["summary"]


async def test_search_history_always_filters_one_feature_type(monkeypatch):
    searched: list[str] = []

    async def fake_search(pool, embedding, *, feature_type, equipment_id=None, top_k=5, **_):
        searched.append(feature_type)
        return [_chunk(1, feature_type, 0.2)]

    monkeypatch.setattr(tools, "hybrid_search", fake_search)
    result = await tools.execute_tool("search_history", {"query": "detector 관련 로그"}, _ctx())
    assert searched == ["id_dump"]
    assert result["data"]["feature_type"] == "id_dump"


def test_search_scopes_follow_feature_registry():
    from rag.features import FEATURE_REGISTRY

    assert tools.SEARCH_FEATURE_TYPES == ("log_general", *FEATURE_REGISTRY)


async def test_conclusion_missing_with_evidence_is_not_reported_as_no_evidence(monkeypatch):
    monkeypatch.setattr(
        tools,
        "run_rag_judgement",
        AsyncMock(return_value=RagJudgementResult(13, None, None, None, [8], [])),
    )
    monkeypatch.setattr(
        tools, "fetch_judgement", AsyncMock(return_value={"retrieved_chunk_ids": [8]})
    )
    monkeypatch.setattr(tools, "fetch_chunks_by_ids", AsyncMock(return_value=[]))
    result = await tools.execute_tool("ask_history", {"query": "chuck 이력"}, _ctx())
    assert "결론(conclusion)이 없었습니다" in result["summary"]
