"""api/routes_spec_check.py 단위 테스트 — evaluate_spec()는 실제로 실행하되(순수 함수),
save_spec_evaluation()/run_rag_judgement()는 스텁으로 대체해 라우팅/조건부 RAG 호출
로직(FR-2.3)만 검증한다.
"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_spec_check
from api.deps import (
    get_asml_api_client_dep,
    get_db_pool,
    get_embedding_client_dep,
    get_llm_client_dep,
    get_settings_dep,
)
from config import Settings
from rag.core import RagJudgementResult

_TEST_SETTINGS = Settings(_env_file=None, database_url="postgresql://x/x")


class _FakeAsmlClient:
    def __init__(self, data: dict, spec: dict):
        self._data = data
        self._spec = spec

    async def fetch_data_and_spec(self, *, equipment_id: str, parameter: str) -> tuple[dict, dict]:
        return self._data, self._spec


def _make_app(asml_client) -> FastAPI:
    app = FastAPI()
    app.include_router(routes_spec_check.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    app.dependency_overrides[get_asml_api_client_dep] = lambda: asml_client
    app.dependency_overrides[get_settings_dep] = lambda: _TEST_SETTINGS
    return app


def _post(asml_client) -> "TestClient.Response":
    client = TestClient(_make_app(asml_client))
    return client.post("/query/spec-check", json={"equipment_id": "EQ-01", "parameter": "focus"})


def test_in_spec_well_within_range_skips_rag(monkeypatch):
    monkeypatch.setattr(routes_spec_check, "save_spec_evaluation", AsyncMock(return_value=1))
    mock_rag = AsyncMock()
    monkeypatch.setattr(routes_spec_check, "run_rag_judgement", mock_rag)

    resp = _post(_FakeAsmlClient({"value": 15.0}, {"lsl": 10.0, "usl": 20.0}))

    assert resp.status_code == 200
    body = resp.json()
    assert body["determination"] == "IN_SPEC"
    assert body["margin_pct"] == 100.0
    assert body["judgement_id"] is None
    mock_rag.assert_not_called()


def test_out_of_spec_triggers_rag(monkeypatch):
    mock_save = AsyncMock(return_value=42)
    monkeypatch.setattr(routes_spec_check, "save_spec_evaluation", mock_save)
    fake_result = RagJudgementResult(
        judgement_id=7,
        conclusion="원인 추정",
        confidence=0.8,
        recommended_action="조치",
        evidence_chunk_ids=[1, 2],
        retrieved_chunks=[],
    )
    mock_rag = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(routes_spec_check, "run_rag_judgement", mock_rag)

    resp = _post(_FakeAsmlClient({"value": 25.0}, {"lsl": 10.0, "usl": 20.0}))

    assert resp.status_code == 200
    body = resp.json()
    assert body["determination"] == "OUT_OF_SPEC"
    assert body["judgement_id"] == 7
    assert body["conclusion"] == "원인 추정"
    assert body["evidence_chunk_ids"] == [1, 2]

    mock_rag.assert_called_once()
    _, kwargs = mock_rag.call_args
    assert kwargs["use_case"] == "spec_check"
    assert kwargs["eval_id"] == 42
    assert kwargs["equipment_id"] == "EQ-01"

    mock_save.assert_called_once()
    _, save_kwargs = mock_save.call_args
    assert save_kwargs["determination"] == "OUT_OF_SPEC"


def test_in_spec_near_margin_threshold_triggers_rag(monkeypatch):
    monkeypatch.setattr(routes_spec_check, "save_spec_evaluation", AsyncMock(return_value=1))
    mock_rag = AsyncMock(
        return_value=RagJudgementResult(
            judgement_id=1,
            conclusion=None,
            confidence=None,
            recommended_action=None,
            evidence_chunk_ids=[],
            retrieved_chunks=[],
        )
    )
    monkeypatch.setattr(routes_spec_check, "run_rag_judgement", mock_rag)

    # value=10.5, lsl=10, usl=20 -> margin_pct == 10.0 (default threshold), IN_SPEC.
    resp = _post(_FakeAsmlClient({"value": 10.5}, {"lsl": 10.0, "usl": 20.0}))

    assert resp.status_code == 200
    body = resp.json()
    assert body["determination"] == "IN_SPEC"
    assert body["margin_pct"] == 10.0
    mock_rag.assert_called_once()


def test_rejects_empty_equipment_id():
    client = TestClient(_make_app(_FakeAsmlClient({"value": 1}, {"lsl": 0, "usl": 2})))
    resp = client.post("/query/spec-check", json={"equipment_id": "", "parameter": "focus"})
    assert resp.status_code == 422
