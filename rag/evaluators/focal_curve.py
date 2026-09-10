"""Focal Curve evaluator — curve 배열의 range/sigma/DOF를 결정론적으로 계산한다.

TBD(00-requirements.md §11): range/sigma/DOF는 CLAUDE.md/01-architecture.md에 명시된
초안 정의를 그대로 구현한 것이며, 정확한 계산식은 부서 엔지니어 확인이 필요하다.
ftpmodule의 focal 아이템(kind="field_curve")이 실제 입력 후보이나, 어느 마크/단면의
H·V 곡선을 curve로 선택할지는 아직 정해지지 않았다 — 그 선택은 이 evaluator의
책임이 아니라 호출자(resolve_data_and_spec 등)의 책임이다.

외부 의존성 없는 순수 함수만 포함한다(enforce-evaluator-purity.sh가 강제).
"""

import statistics

from rag.spec_evaluator import SpecResult, evaluate_spec


class FocalCurveEvaluator:
    """data={"curve": [float, ...]}, spec={"lsl": float, "usl": float}."""

    def evaluate(self, data: dict, spec: dict) -> SpecResult:
        curve = data["curve"]
        curve_range = max(curve) - min(curve)
        sigma = statistics.stdev(curve) if len(curve) >= 2 else 0.0
        dof = len(curve) - 1

        result = evaluate_spec({"value": curve_range}, spec)
        metrics = {"range": curve_range, "sigma": sigma, "dof": dof, "n": len(curve)}
        return SpecResult(
            determination=result.determination,
            margin_pct=result.margin_pct,
            metrics=metrics,
            measured_value=curve_range,
            lsl=spec.get("lsl"),
            usl=spec.get("usl"),
        )
