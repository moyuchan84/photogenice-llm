"""rag/features.py::run_spec_check() — 판정→저장→조건부 RAG 흐름(FR-2.3) 단위 테스트.
기존 tests/test_routes_spec_check.py에 있던 조건부 RAG 3케이스를 여기로 옮겼다 —
로직이 라우터에서 이 공용 함수로 이동했기 때문(핵심 설계 결정 #2).
"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from config import Settings
from rag import features
from rag.core import RagJudgementResult

_TEST_SETTINGS = Settings(_env_file=None, database_url="postgresql://x/x")


async def _run(monkeypatch, *, feature_type, data, spec, save_return=1, rag_result=None):
    mock_save = AsyncMock(return_value=save_return)
    monkeypatch.setattr(features, "save_spec_evaluation", mock_save)
    mock_rag = AsyncMock(return_value=rag_result)
    monkeypatch.setattr(features, "run_rag_judgement", mock_rag)

    outcome = await features.run_spec_check(
        None,
        None,
        None,
        _TEST_SETTINGS,
        feature_type=feature_type,
        equipment_id="EQ-01",
        parameter="focus",
        data=data,
        spec=spec,
        top_k=5,
    )
    return outcome, mock_save, mock_rag


async def test_in_spec_well_within_range_skips_rag(monkeypatch):
    outcome, mock_save, mock_rag = await _run(
        monkeypatch, feature_type="generic", data={"value": 15.0}, spec={"lsl": 10.0, "usl": 20.0}
    )

    assert outcome.determination == "IN_SPEC"
    assert outcome.margin_pct == 100.0
    assert outcome.judgement_id is None
    mock_rag.assert_not_called()
    mock_save.assert_called_once()
    _, save_kwargs = mock_save.call_args
    assert save_kwargs["feature_type"] == "generic"
    assert save_kwargs["metrics"] is None


async def test_out_of_spec_triggers_rag(monkeypatch):
    fake_result = RagJudgementResult(
        judgement_id=7,
        conclusion="원인 추정",
        confidence=0.8,
        recommended_action="조치",
        evidence_chunk_ids=[1, 2],
        retrieved_chunks=[],
    )
    outcome, mock_save, mock_rag = await _run(
        monkeypatch,
        feature_type="generic",
        data={"value": 25.0},
        spec={"lsl": 10.0, "usl": 20.0},
        save_return=42,
        rag_result=fake_result,
    )

    assert outcome.determination == "OUT_OF_SPEC"
    assert outcome.judgement_id == 7
    assert outcome.conclusion == "원인 추정"
    assert outcome.evidence_chunk_ids == [1, 2]

    mock_rag.assert_called_once()
    _, kwargs = mock_rag.call_args
    assert kwargs["use_case"] == "spec_check"
    assert kwargs["eval_id"] == 42
    assert kwargs["equipment_id"] == "EQ-01"
    assert kwargs["feature_type"] == "log_general"  # generic은 log_chunks 기본 스코프 사용

    mock_save.assert_called_once()
    _, save_kwargs = mock_save.call_args
    assert save_kwargs["determination"] == "OUT_OF_SPEC"


async def test_in_spec_near_margin_threshold_triggers_rag(monkeypatch):
    fake_result = RagJudgementResult(
        judgement_id=1,
        conclusion=None,
        confidence=None,
        recommended_action=None,
        evidence_chunk_ids=[],
        retrieved_chunks=[],
    )
    # value=10.75, lsl=10, usl=20 -> margin_pct == 15.0 (Phase 5 확정 기본 임계치).
    outcome, _mock_save, mock_rag = await _run(
        monkeypatch,
        feature_type="generic",
        data={"value": 10.75},
        spec={"lsl": 10.0, "usl": 20.0},
        rag_result=fake_result,
    )

    assert outcome.determination == "IN_SPEC"
    assert outcome.margin_pct == 15.0
    mock_rag.assert_called_once()


async def test_focal_curve_feature_type_uses_registered_evaluator_and_saves_metrics(monkeypatch):
    mock_save = AsyncMock(return_value=99)
    monkeypatch.setattr(features, "save_spec_evaluation", mock_save)
    mock_rag = AsyncMock()
    monkeypatch.setattr(features, "run_rag_judgement", mock_rag)

    outcome = await features.run_spec_check(
        None,
        None,
        None,
        _TEST_SETTINGS,
        feature_type="focal_curve",
        equipment_id="EQ-01",
        parameter="focal",
        data={"curve": [0.0, 50.0]},
        spec={"lsl": 0.0, "usl": 100.0},
        top_k=5,
    )

    assert outcome.determination == "IN_SPEC"
    assert outcome.margin_pct == 100.0
    mock_save.assert_called_once()
    _, save_kwargs = mock_save.call_args
    assert save_kwargs["feature_type"] == "focal_curve"
    assert save_kwargs["metrics"] == pytest.approx(
        {"range": 50.0, "sigma": 50.0 / 2**0.5, "dof": 1, "n": 2}
    )
    # curve 데이터는 {"value"} 키가 없어 measured_value가 NULL로 저장되던 감사 추적
    # 공백(code review 발견) — evaluator가 채운 대표값이 저장되는지 확인.
    assert save_kwargs["measured_value"] == 50.0
    assert save_kwargs["lsl"] == 0.0
    assert save_kwargs["usl"] == 100.0
    mock_rag.assert_not_called()


async def test_final_xy_feature_type_saves_worse_axis_as_measured_value(monkeypatch):
    mock_save = AsyncMock(return_value=100)
    monkeypatch.setattr(features, "save_spec_evaluation", mock_save)
    monkeypatch.setattr(features, "run_rag_judgement", AsyncMock())

    # spec은 {"x": {...}, "y": {...}} 중첩 구조라 spec.get("lsl")이 항상 None이 되던
    # 감사 추적 공백(code review 발견) — worse axis의 spec/bound가 저장되는지 확인.
    await features.run_spec_check(
        None,
        None,
        None,
        _TEST_SETTINGS,
        feature_type="final_xy",
        equipment_id="EQ-01",
        parameter="overlay_x",
        data={"x": [10.0, 10.0, 10.0], "y": [0.0, 0.0, 0.0]},
        spec={"x": {"lsl": -5.0, "usl": 5.0}, "y": {"lsl": -5.0, "usl": 5.0}},
        top_k=5,
    )

    mock_save.assert_called_once()
    _, save_kwargs = mock_save.call_args
    assert save_kwargs["measured_value"] == 10.0
    assert save_kwargs["lsl"] == -5.0
    assert save_kwargs["usl"] == 5.0


async def test_resolve_data_and_spec_uses_inline_without_asml_client(monkeypatch):
    from models.schemas import SpecCheckRequest

    req = SpecCheckRequest(
        equipment_id="EQ-01",
        parameter="focus",
        inline_data={"value": 1.0},
        inline_spec={"lsl": 0.0, "usl": 2.0},
    )

    # asml_client=None이어도(ASML API 미설정 환경) inline 경로는 503 없이 동작해야 한다
    # (FR-2.5 — code review에서 발견된 버그: 이전에는 api/deps.py의 의존성 단계에서
    # asml_client가 None이면 핸들러 진입 전에 무조건 503을 던져 이 경로를 무력화했다).
    data, spec = await features.resolve_data_and_spec(req, feature_type="generic", asml_client=None)
    assert data == {"value": 1.0}
    assert spec == {"lsl": 0.0, "usl": 2.0}


async def test_resolve_data_and_spec_raises_503_without_inline_and_without_asml_client():
    from fastapi import HTTPException

    from models.schemas import SpecCheckRequest

    req = SpecCheckRequest(equipment_id="EQ-01", parameter="focus")

    with pytest.raises(HTTPException) as exc_info:
        await features.resolve_data_and_spec(req, feature_type="generic", asml_client=None)
    assert exc_info.value.status_code == 503


async def test_resolve_data_and_spec_dispatches_to_feature_fetch_method():
    from models.schemas import SpecCheckRequest

    class _FakeAsmlClient:
        async def fetch_feature_data_and_spec(self, *, feature_type, equipment_id, parameter):
            return {"curve": [0.0, 1.0]}, {"lsl": 0.0, "usl": 1.0}

    req = SpecCheckRequest(equipment_id="EQ-01", parameter="focal")
    data, spec = await features.resolve_data_and_spec(
        req, feature_type="focal_curve", asml_client=_FakeAsmlClient()
    )
    assert data == {"curve": [0.0, 1.0]}
    assert spec == {"lsl": 0.0, "usl": 1.0}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "2026-09-10T13:45:00+09:00",
            datetime(2026, 9, 10, 13, 45, tzinfo=timezone(timedelta(hours=9))),
        ),
        ("2026-09-10T04:45:00Z", datetime(2026, 9, 10, 4, 45, tzinfo=UTC)),
        ("2026-09-10T13:45:00", datetime(2026, 9, 10, 13, 45)),  # noqa: DTZ001 — tz 없는 입력도 그대로 보존
        (None, None),
        ("not-a-timestamp", None),
    ],
)
async def test_measured_at_is_normalized_to_datetime(monkeypatch, raw, expected):
    """ASML API/inline_data의 measured_at은 ISO 문자열로 들어오는데, asyncpg는
    timestamptz에 str을 받으면 DataError를 낸다 — 저장 직전에 datetime으로 정규화돼야
    판정 결과가 감사 로그에 남는다(CLAUDE.md '감사 추적, 생략 금지')."""
    _, mock_save, _ = await _run(
        monkeypatch,
        feature_type="generic",
        data={"value": 15.0, "measured_at": raw},
        spec={"lsl": 10.0, "usl": 20.0},
    )

    _, save_kwargs = mock_save.call_args
    assert save_kwargs["measured_at"] == expected


async def test_measured_at_datetime_passes_through(monkeypatch):
    already = datetime(2026, 9, 10, 13, 45, tzinfo=timezone(timedelta(hours=9)))
    _, mock_save, _ = await _run(
        monkeypatch,
        feature_type="generic",
        data={"value": 15.0, "measured_at": already},
        spec={"lsl": 10.0, "usl": 20.0},
    )

    _, save_kwargs = mock_save.call_args
    assert save_kwargs["measured_at"] is already


# --- 적재 분류 키워드 배선 (Phase 6 후속) --------------------------------------


def test_every_registered_feature_has_log_keywords():
    """log_keywords가 비면 그 기능의 청크가 적재되지 않아 검색이 영구히 0건이 된다 —
    새 기능을 FEATURE_REGISTRY에 추가할 때 가장 놓치기 쉬운 지점이라 테스트로 고정한다."""
    missing = [ft for ft, cfg in features.FEATURE_REGISTRY.items() if not cfg.log_keywords]
    assert missing == []


def test_build_log_keyword_map_covers_registry():
    keyword_map = features.build_log_keyword_map()
    assert set(keyword_map) == set(features.FEATURE_REGISTRY)
    for feature_type, keywords in keyword_map.items():
        assert keywords == features.FEATURE_REGISTRY[feature_type].log_keywords


def test_registry_keywords_actually_classify_their_own_feature():
    """레지스트리 키워드가 chunker 분류를 실제로 통과하는지 — 키워드 간 충돌로 다른
    기능이 선택되면 여기서 잡힌다."""
    from datetime import UTC, datetime

    from workers.chunker import RawLogRow, classify_feature_type

    keyword_map = features.build_log_keyword_map()
    for feature_type, keywords in keyword_map.items():
        row = RawLogRow(
            log_id=1,
            equipment_id="EQ-01",
            event_time=datetime(2026, 9, 8, tzinfo=UTC),
            message=keywords[0],
        )
        assert classify_feature_type([row], keyword_map) == feature_type
