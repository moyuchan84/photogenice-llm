"""api/routes_spec_check.py 단위 테스트 — 판정/저장/조건부 RAG 로직 자체는
tests/test_features.py로 이동했다. 여기서는 라우터가 resolve_data_and_spec()/
run_spec_check()를 올바른 인자(feature_type="generic")로 호출하고 응답을 올바르게
마샬링하는지만 검증한다.
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
from rag.features import SpecCheckOutcome

_TEST_SETTINGS = Settings(_env_file=None, database_url="postgresql://x/x")


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes_spec_check.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    app.dependency_overrides[get_asml_api_client_dep] = lambda: None
    app.dependency_overrides[get_settings_dep] = lambda: _TEST_SETTINGS
    return app


def test_route_wires_resolve_and_run_spec_check_with_generic_feature_type(monkeypatch):
    mock_resolve = AsyncMock(return_value=({"value": 15.0}, {"lsl": 10.0, "usl": 20.0}))
    monkeypatch.setattr(routes_spec_check, "resolve_data_and_spec", mock_resolve)
    mock_run = AsyncMock(
        return_value=SpecCheckOutcome(
            eval_id=1,
            determination="IN_SPEC",
            margin_pct=100.0,
            judgement_id=None,
            conclusion=None,
            confidence=None,
            recommended_action=None,
            evidence_chunk_ids=None,
        )
    )
    monkeypatch.setattr(routes_spec_check, "run_spec_check", mock_run)

    client = TestClient(_make_app())
    resp = client.post("/query/spec-check", json={"equipment_id": "EQ-01", "parameter": "focus"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["determination"] == "IN_SPEC"
    assert body["margin_pct"] == 100.0
    assert body["judgement_id"] is None

    mock_resolve.assert_called_once()
    _, resolve_kwargs = mock_resolve.call_args
    assert resolve_kwargs["feature_type"] == "generic"

    mock_run.assert_called_once()
    _, run_kwargs = mock_run.call_args
    assert run_kwargs["feature_type"] == "generic"
    assert run_kwargs["equipment_id"] == "EQ-01"
    assert run_kwargs["parameter"] == "focus"
    assert run_kwargs["data"] == {"value": 15.0}
    assert run_kwargs["spec"] == {"lsl": 10.0, "usl": 20.0}


def test_rejects_empty_equipment_id():
    client = TestClient(_make_app())
    resp = client.post("/query/spec-check", json={"equipment_id": "", "parameter": "focus"})
    assert resp.status_code == 422


def test_inline_data_succeeds_when_asml_api_not_configured(monkeypatch):
    """FR-2.5 회귀 테스트(code review에서 발견) — get_asml_api_client_dep을
    오버라이드하지 않고 app.state.asml_api_client=None(ASML API 미설정 환경)을 그대로
    둔 채, inline_data/inline_spec 경로가 503 없이 성공하는지 실제 의존성 체인으로
    확인한다."""
    mock_run = AsyncMock(
        return_value=SpecCheckOutcome(
            eval_id=1,
            determination="IN_SPEC",
            margin_pct=100.0,
            judgement_id=None,
            conclusion=None,
            confidence=None,
            recommended_action=None,
            evidence_chunk_ids=None,
        )
    )
    monkeypatch.setattr(routes_spec_check, "run_spec_check", mock_run)

    app = FastAPI()
    app.include_router(routes_spec_check.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    app.dependency_overrides[get_settings_dep] = lambda: _TEST_SETTINGS
    app.state.asml_api_client = None  # get_asml_api_client_dep은 오버라이드하지 않음

    client = TestClient(app)
    resp = client.post(
        "/query/spec-check",
        json={
            "equipment_id": "EQ-01",
            "parameter": "focus",
            "inline_data": {"value": 15.0},
            "inline_spec": {"lsl": 10.0, "usl": 20.0},
        },
    )

    assert resp.status_code == 200
    mock_run.assert_called_once()
