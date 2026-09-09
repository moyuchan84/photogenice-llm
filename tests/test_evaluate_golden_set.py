"""scripts/evaluate_golden_set.py의 순수 함수(DB/LLM 불필요) 유닛테스트.

margin_pct 임계치 게이트 판단/집계와 top_k 결과 집계 로직만 검증한다 — 실제
run_rag_judgement() 흐름은 이 스크립트 자체가 tests/test_rag_core_integration.py가
이미 검증한 run_rag_judgement()를 그대로 재사용하므로 여기서 다시 검증하지 않는다.
"""

import pytest

from rag.spec_evaluator import SpecResult
from scripts.evaluate_golden_set import (
    GoldenCase,
    aggregate_topk_results,
    gate_decision,
    keyword_score,
    recommend_threshold,
    recommend_top_k,
    sweep_gate_thresholds,
)


def _case(case_id: str, requires_explanation: bool) -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        equipment_id="EQ-01",
        parameter="overlay_x",
        unit="nm",
        measured_value=0.0,
        spec={"lsl": -5.0, "usl": 5.0, "target": 0.0},
        requires_explanation=requires_explanation,
        expected_error_codes=[],
        expected_keywords=[],
    )


def test_gate_decision_true_when_out_of_spec_regardless_of_margin():
    result = SpecResult(determination="OUT_OF_SPEC", margin_pct=-50.0)
    assert gate_decision(result, threshold=0) is True


def test_gate_decision_true_when_margin_within_threshold():
    result = SpecResult(determination="IN_SPEC", margin_pct=8.0)
    assert gate_decision(result, threshold=10) is True
    assert gate_decision(result, threshold=5) is False


def test_keyword_score_counts_case_insensitive_substring_hits():
    assert keyword_score("램프 노화로 인한 출력 저하", ["램프", "노화", "없음"]) == 2 / 3


def test_keyword_score_is_one_when_no_keywords_expected():
    assert keyword_score("아무 텍스트", []) == 1.0


def test_sweep_gate_thresholds_reports_accuracy_and_wrong_cases():
    cases = [_case("A", requires_explanation=True), _case("B", requires_explanation=False)]
    spec_results = {
        "A": SpecResult(determination="IN_SPEC", margin_pct=12.0),
        "B": SpecResult(determination="IN_SPEC", margin_pct=18.0),
    }

    sweep = sweep_gate_thresholds(cases, spec_results, thresholds=[10.0, 15.0])

    at_10 = next(r for r in sweep if r.threshold == 10.0)
    assert at_10.wrong_case_ids == ["A"]  # A는 margin12>10 -> 게이트 False, gold=True와 불일치
    assert at_10.accuracy == 0.5
    assert at_10.missed_explanation_rate == 1.0
    assert at_10.unnecessary_rag_rate == 0.0

    at_15 = next(r for r in sweep if r.threshold == 15.0)
    assert at_15.wrong_case_ids == []
    assert at_15.accuracy == 1.0
    assert at_15.missed_explanation_rate == 0.0
    assert at_15.unnecessary_rag_rate == 0.0


def test_recommend_threshold_prefers_fewer_missed_explanations_over_unnecessary_calls():
    from scripts.evaluate_golden_set import GateSweepResult

    sweep = [
        GateSweepResult(
            threshold=5.0,
            accuracy=0.8,
            unnecessary_rag_rate=0.0,
            missed_explanation_rate=0.2,
            wrong_case_ids=["A"],
        ),
        GateSweepResult(
            threshold=15.0,
            accuracy=0.8,
            unnecessary_rag_rate=0.2,
            missed_explanation_rate=0.0,
            wrong_case_ids=["B"],
        ),
    ]

    best = recommend_threshold(sweep)

    assert best.threshold == 15.0


def test_aggregate_topk_results_averages_metrics():
    per_case = {
        "A": {"evidence_hit": True, "keyword_score": 1.0, "confidence": 0.8},
        "B": {"evidence_hit": False, "keyword_score": 0.0, "confidence": 0.4},
    }

    result = aggregate_topk_results(top_k=5, per_case=per_case)

    assert result.mean_evidence_hit_rate == 0.5
    assert result.mean_keyword_score == 0.5
    assert result.mean_confidence == pytest.approx(0.6)


def test_recommend_top_k_prefers_smaller_top_k_on_tie():
    from scripts.evaluate_golden_set import TopKSweepResult

    sweep = [
        TopKSweepResult(
            top_k=8,
            mean_evidence_hit_rate=1.0,
            mean_keyword_score=1.0,
            mean_confidence=0.9,
            per_case={},
        ),
        TopKSweepResult(
            top_k=4,
            mean_evidence_hit_rate=1.0,
            mean_keyword_score=1.0,
            mean_confidence=0.9,
            per_case={},
        ),
    ]

    assert recommend_top_k(sweep).top_k == 4
