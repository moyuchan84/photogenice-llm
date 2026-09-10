"""api/routes_focal_curve.py — 얇은 배선 검증(판정 로직 자체는 tests/test_features.py,
FocalCurveEvaluator 계산은 tests/test_evaluators_focal_curve.py에서 검증)."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_focal_curve
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
    app.include_router(routes_focal_curve.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    app.dependency_overrides[get_asml_api_client_dep] = lambda: None
    app.dependency_overrides[get_settings_dep] = lambda: _TEST_SETTINGS
    return app


def test_route_calls_run_spec_check_with_focal_curve_feature_type(monkeypatch):
    mock_resolve = AsyncMock(return_value=({"curve": [0.0, 10.0]}, {"lsl": 0.0, "usl": 20.0}))
    monkeypatch.setattr(routes_focal_curve, "resolve_data_and_spec", mock_resolve)
    mock_run = AsyncMock(
        return_value=SpecCheckOutcome(
            eval_id=5,
            determination="IN_SPEC",
            margin_pct=90.0,
            judgement_id=None,
            conclusion=None,
            confidence=None,
            recommended_action=None,
            evidence_chunk_ids=None,
        )
    )
    monkeypatch.setattr(routes_focal_curve, "run_spec_check", mock_run)

    client = TestClient(_make_app())
    resp = client.post("/query/focal-curve", json={"equipment_id": "EQ-01", "parameter": "focal"})

    assert resp.status_code == 200
    assert resp.json()["eval_id"] == 5

    _, resolve_kwargs = mock_resolve.call_args
    assert resolve_kwargs["feature_type"] == "focal_curve"
    _, run_kwargs = mock_run.call_args
    assert run_kwargs["feature_type"] == "focal_curve"
    assert run_kwargs["data"] == {"curve": [0.0, 10.0]}
