"""api/routes_id_dump.py — 얇은 배선 검증(root_cause 흐름 자체는 tests/test_id_dump.py)."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_id_dump
from api.deps import get_db_pool, get_embedding_client_dep, get_llm_client_dep
from rag.core import RagJudgementResult


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes_id_dump.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    return app


def test_route_calls_run_id_dump_analysis(monkeypatch):
    fake_result = RagJudgementResult(
        judgement_id=9,
        conclusion="원인 추정",
        confidence=0.6,
        recommended_action="조치 권고",
        evidence_chunk_ids=[3],
        retrieved_chunks=[],
    )
    mock_run = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(routes_id_dump, "run_id_dump_analysis", mock_run)

    client = TestClient(_make_app())
    resp = client.post(
        "/query/id-dump", json={"equipment_id": "EQ-01", "error_dump_text": "ERR_1234 timeout"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["judgement_id"] == 9
    assert body["evidence_chunk_ids"] == [3]

    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["equipment_id"] == "EQ-01"
    assert kwargs["error_dump_text"] == "ERR_1234 timeout"


def test_rejects_empty_error_dump_text():
    client = TestClient(_make_app())
    resp = client.post("/query/id-dump", json={"equipment_id": "EQ-01", "error_dump_text": ""})
    assert resp.status_code == 422
