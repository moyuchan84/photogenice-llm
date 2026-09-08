"""api/routes_history.py 단위 테스트 — run_rag_judgement()를 스텁으로 대체해 라우팅/스키마
검증만 확인한다. 실제 검색/LLM 흐름은 tests/test_rag_core_integration.py에서 검증한다.
"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_history
from api.deps import get_db_pool, get_embedding_client_dep, get_llm_client_dep
from rag.core import RagJudgementResult


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes_history.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    return app


def test_query_history_returns_judgement(monkeypatch):
    fake_result = RagJudgementResult(
        judgement_id=42,
        conclusion="결론",
        confidence=0.9,
        recommended_action="조치",
        evidence_chunk_ids=[1, 2],
        retrieved_chunks=[],
    )
    monkeypatch.setattr(routes_history, "run_rag_judgement", AsyncMock(return_value=fake_result))

    client = TestClient(_make_app())
    resp = client.post("/query/history", json={"query": "EQ-01 최근 이상 원인은?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "judgement_id": 42,
        "conclusion": "결론",
        "confidence": 0.9,
        "recommended_action": "조치",
        "evidence_chunk_ids": [1, 2],
    }


def test_query_history_passes_filters_through(monkeypatch):
    fake_result = RagJudgementResult(
        judgement_id=1,
        conclusion=None,
        confidence=None,
        recommended_action=None,
        evidence_chunk_ids=[],
        retrieved_chunks=[],
    )
    mock_run = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(routes_history, "run_rag_judgement", mock_run)

    client = TestClient(_make_app())
    resp = client.post(
        "/query/history",
        json={"query": "질의", "equipment_id": "EQ-01", "top_k": 3},
    )

    assert resp.status_code == 200
    _, kwargs = mock_run.call_args
    assert kwargs["equipment_id"] == "EQ-01"
    assert kwargs["top_k"] == 3
    assert kwargs["use_case"] == "history"


def test_query_history_rejects_empty_query():
    client = TestClient(_make_app())
    resp = client.post("/query/history", json={"query": ""})
    assert resp.status_code == 422
