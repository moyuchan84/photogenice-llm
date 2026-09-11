"""api/routes_final_xy.py — 얇은 배선 검증(판정 로직 자체는 tests/test_features.py,
FinalXYEvaluator 계산은 tests/test_evaluators_final_xy.py에서 검증)."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import routes_final_xy
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
    app.include_router(routes_final_xy.router)
    app.dependency_overrides[get_db_pool] = lambda: None
    app.dependency_overrides[get_embedding_client_dep] = lambda: None
    app.dependency_overrides[get_llm_client_dep] = lambda: None
    app.dependency_overrides[get_asml_api_client_dep] = lambda: None
    app.dependency_overrides[get_settings_dep] = lambda: _TEST_SETTINGS
    return app


def test_route_calls_run_spec_check_with_final_xy_feature_type(monkeypatch):
    mock_resolve = AsyncMock(
        return_value=(
            {"x": [0.0, 0.0], "y": [0.0, 0.0]},
            {"x": {"lsl": -5.0, "usl": 5.0}, "y": {"lsl": -5.0, "usl": 5.0}},
        )
    )
    monkeypatch.setattr(routes_final_xy, "resolve_data_and_spec", mock_resolve)
    mock_run = AsyncMock(
        return_value=SpecCheckOutcome(
            eval_id=6,
            determination="IN_SPEC",
            margin_pct=100.0,
            judgement_id=None,
            conclusion=None,
            confidence=None,
            recommended_action=None,
            evidence_chunk_ids=None,
        )
    )
    monkeypatch.setattr(routes_final_xy, "run_spec_check", mock_run)

    client = TestClient(_make_app())
    resp = client.post("/query/final-xy", json={"equipment_id": "EQ-01", "parameter": "overlay_x"})

    assert resp.status_code == 200
    assert resp.json()["eval_id"] == 6

    _, resolve_kwargs = mock_resolve.call_args
    assert resolve_kwargs["feature_type"] == "final_xy"
    _, run_kwargs = mock_run.call_args
    assert run_kwargs["feature_type"] == "final_xy"
