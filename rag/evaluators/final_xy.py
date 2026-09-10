"""Final XY(overlay) evaluator — X/Y 축별 mean±3σ를 결정론적으로 계산한다.

TBD(00-requirements.md §11): mean±3σ는 CLAUDE.md/01-architecture.md에 명시된 초안
정의를 그대로 구현한 것이다. ftpmodule의 overlay 아이템은 10-파라미터 모델 적합 후
residual의 absmax/3σ(ddof=1)를 쓰는 더 정교한 공식(interface.md "모델 적합 공식")을
제공하지만, 그 공식 채택 여부는 부서 엔지니어 확인 후 결정한다 — 지금은 dx/dy(또는
x/y) 원시 배열에 대한 단순 mean±3σ로 구현한다.

외부 의존성 없는 순수 함수만 포함한다(enforce-evaluator-purity.sh가 강제).
"""

import statistics

from rag.spec_evaluator import SpecResult, evaluate_spec


def _axis_bounds(values: list[float]) -> tuple[float, float, dict]:
    mean = statistics.fmean(values)
    sigma = statistics.stdev(values) if len(values) >= 2 else 0.0
    lower, upper = mean - 3 * sigma, mean + 3 * sigma
    metrics = {
        "mean": mean,
        "sigma": sigma,
        "lower_bound": lower,
        "upper_bound": upper,
        "n": len(values),
    }
    return lower, upper, metrics


def _evaluate_axis(values: list[float], axis_spec: dict) -> tuple[SpecResult, dict, float]:
    lower, upper, metrics = _axis_bounds(values)
    lower_result = evaluate_spec({"value": lower}, axis_spec)
    upper_result = evaluate_spec({"value": upper}, axis_spec)
    if lower_result.margin_pct <= upper_result.margin_pct:
        return lower_result, metrics, lower
    return upper_result, metrics, upper


class FinalXYEvaluator:
    """data={"x": [float, ...], "y": [float, ...]}, spec={"x": {"lsl","usl"}, "y": {...}}."""

    def evaluate(self, data: dict, spec: dict) -> SpecResult:
        x_result, x_metrics, x_bound = _evaluate_axis(data["x"], spec["x"])
        y_result, y_metrics, y_bound = _evaluate_axis(data["y"], spec["y"])
        metrics = {"x": x_metrics, "y": y_metrics}
        if x_result.margin_pct <= y_result.margin_pct:
            worse, worse_spec, worse_bound = x_result, spec["x"], x_bound
        else:
            worse, worse_spec, worse_bound = y_result, spec["y"], y_bound
        return SpecResult(
            determination=worse.determination,
            margin_pct=worse.margin_pct,
            metrics=metrics,
            measured_value=worse_bound,
            lsl=worse_spec.get("lsl"),
            usl=worse_spec.get("usl"),
        )
