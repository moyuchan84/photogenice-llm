"""spec in/out 결정론적 판정 (FR-2.2, NFR-1).

외부 의존성(DB, LLM/embedding 관련 클라이언트, config) 없는 순수 함수 모듈이다.
enforce-evaluator-purity.sh hook이 금지된 클라이언트 임포트를 자동 차단한다.
DB 저장 및 조건부 RAG 호출(run_spec_check)은 Phase 4의 api/routes_spec_check.py
책임이며 여기서 구현하지 않는다.
"""
from dataclasses import dataclass
from typing import Literal, Protocol

Determination = Literal["IN_SPEC", "OUT_OF_SPEC"]


@dataclass(frozen=True)
class SpecResult:
    determination: Determination
    margin_pct: float


class SpecEvaluator(Protocol):
    """Phase 6의 FocalCurveEvaluator/FinalXYEvaluator가 구현할 공통 인터페이스."""

    def evaluate(self, data: dict, spec: dict) -> SpecResult: ...


def evaluate_spec(data: dict, spec: dict) -> SpecResult:
    value = data["value"]
    lsl, usl = spec["lsl"], spec["usl"]
    determination: Determination = (
        "OUT_OF_SPEC" if (value < lsl or value > usl) else "IN_SPEC"
    )
    half_range = (usl - lsl) / 2
    center = (usl + lsl) / 2
    if half_range == 0:
        margin_pct = 100.0 if value == center else -100.0
    else:
        margin_pct = (1 - abs(value - center) / half_range) * 100
    return SpecResult(determination=determination, margin_pct=round(margin_pct, 2))


class GenericSpecEvaluator:
    """기본 단일값 evaluator (feature_type='generic')."""

    def evaluate(self, data: dict, spec: dict) -> SpecResult:
        return evaluate_spec(data, spec)
