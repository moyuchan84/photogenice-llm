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
    metrics: dict | None = None
    # data/spec이 evaluate_spec()의 {"value"}/{"lsl","usl"} 평탄 구조와 다른
    # evaluator(focal_curve/final_xy 등)는 spec_evaluations 감사 컬럼(measured_value/
    # lsl/usl)에 채울 대표값을 여기 명시적으로 채운다 — 비워두면 호출자가 data/spec에서
    # "value"/"lsl"/"usl" 키를 직접 찾다가 NULL로 저장되는 감사 추적 공백이 생긴다.
    measured_value: float | None = None
    lsl: float | None = None
    usl: float | None = None


class SpecEvaluator(Protocol):
    """Phase 6의 FocalCurveEvaluator/FinalXYEvaluator가 구현할 공통 인터페이스."""

    def evaluate(self, data: dict, spec: dict) -> SpecResult: ...


def evaluate_spec(data: dict, spec: dict) -> SpecResult:
    value = data["value"]
    lsl, usl = spec["lsl"], spec["usl"]
    determination: Determination = "OUT_OF_SPEC" if (value < lsl or value > usl) else "IN_SPEC"
    half_range = (usl - lsl) / 2
    center = (usl + lsl) / 2
    if half_range == 0:
        margin_pct = 100.0 if value == center else -100.0
    else:
        margin_pct = (1 - abs(value - center) / half_range) * 100
    return SpecResult(determination=determination, margin_pct=round(margin_pct, 2))


_VALID_OPERATORS = {"<", "<=", ">", ">=", "==", "!="}


def _compare(x: float, operator: str, threshold: float) -> bool:
    if operator == "<":
        return x < threshold
    if operator == "<=":
        return x <= threshold
    if operator == ">":
        return x > threshold
    if operator == ">=":
        return x >= threshold
    if operator == "==":
        return x == threshold
    return x != threshold  # "!="


def evaluate_criterion(data: dict, spec: dict) -> SpecResult:
    """ftpmodule `/spec/map` 기준(criterion) 판정 경로.

    evaluate_spec()의 LSL/USL 평탄 구조와 달리, ftpmodule의 판정 기준은
    `{"operator", "threshold", "operator2", "threshold2", "is_absolute"}` 형태의
    operator/threshold 행(criterion)으로 내려온다(SPEC.md §3.13, README.api.md §14).
    evaluators/*.py에서 순환 임포트 없이 공유하기 위해 evaluate_spec 옆에 둔다.
    어떤 ftpmodule 원시값(raw/필터링/집계)을 data["value"]로 채울지는 이 함수가 아니라
    clients 계층의 ASML API 호출 어댑터 책임이며, 실제 매핑은 부서 확인 대기(TBD)다.
    """
    value = data["value"]
    criterion = spec["criterion"]
    is_absolute = bool(criterion["is_absolute"])
    x = abs(value) if is_absolute else value

    operator = criterion["operator"]
    threshold = criterion["threshold"]
    operator2 = criterion.get("operator2") or ""
    threshold2 = criterion.get("threshold2")

    if operator not in _VALID_OPERATORS:
        raise ValueError(f"알 수 없는 operator: {operator!r}")
    if operator2 and operator2 not in _VALID_OPERATORS:
        raise ValueError(f"알 수 없는 operator2: {operator2!r}")
    if operator2 and threshold2 is None:
        raise ValueError("operator2가 지정됐지만 threshold2가 없습니다.")

    conditions = [
        {"operator": operator, "threshold": threshold, "holds": _compare(x, operator, threshold)}
    ]
    if operator2:
        conditions.append(
            {
                "operator": operator2,
                "threshold": threshold2,
                "holds": _compare(x, operator2, threshold2),
            }
        )

    determination: Determination = (
        "IN_SPEC" if all(c["holds"] for c in conditions) else "OUT_OF_SPEC"
    )

    upper_thresholds = [c["threshold"] for c in conditions if c["operator"] in ("<", "<=")]
    lower_thresholds = [c["threshold"] for c in conditions if c["operator"] in (">", ">=")]
    upper = min(upper_thresholds) if upper_thresholds else None
    lower = max(lower_thresholds) if lower_thresholds else None

    if is_absolute and upper is not None and lower is None:
        # |v| < U 류 단일 상한 + 절대값 조합은 원시값 기준 [-U, U] 대역으로 취급한다.
        if upper == 0:
            margin_pct = 100.0 if determination == "IN_SPEC" else -100.0
        else:
            margin_pct = (1 - abs(value) / upper) * 100
        lsl, usl = -upper, upper
    elif upper is not None and lower is not None:
        half_range = (upper - lower) / 2
        center = (upper + lower) / 2
        if half_range == 0:
            margin_pct = 100.0 if determination == "IN_SPEC" else -100.0
        else:
            margin_pct = (1 - abs(x - center) / half_range) * 100
        lsl, usl = lower, upper
    elif upper is not None:
        if upper == 0:
            margin_pct = 100.0 if determination == "IN_SPEC" else -100.0
        else:
            margin_pct = min((upper - x) / abs(upper) * 100, 100.0)
        lsl, usl = lower, upper
    elif lower is not None:
        if lower == 0:
            margin_pct = 100.0 if determination == "IN_SPEC" else -100.0
        else:
            margin_pct = min((x - lower) / abs(lower) * 100, 100.0)
        lsl, usl = lower, upper
    else:
        margin_pct = 100.0 if determination == "IN_SPEC" else -100.0
        lsl, usl = lower, upper

    if determination == "OUT_OF_SPEC":
        margin_pct = min(margin_pct, 0.0)
    else:
        margin_pct = max(margin_pct, 0.0)

    metrics = {
        "x": x,
        "is_absolute": is_absolute,
        "lower": lower,
        "upper": upper,
        "conditions": conditions,
    }

    return SpecResult(
        determination=determination,
        margin_pct=round(margin_pct, 2),
        metrics=metrics,
        measured_value=value,
        lsl=lsl,
        usl=usl,
    )


class GenericSpecEvaluator:
    """기본 단일값 evaluator (feature_type='generic').

    spec에 "criterion" 키가 있으면 evaluate_criterion()으로 위임한다 — ftpmodule의
    `/spec/map` 판정 기준이 LSL/USL이 아니라 operator/threshold 행으로 내려오기
    때문이며, 어떤 ftpmodule 값을 data["value"]로 채울지는 어댑터 책임(TBD)이다.
    """

    def evaluate(self, data: dict, spec: dict) -> SpecResult:
        if "criterion" in spec:
            return evaluate_criterion(data, spec)
        return evaluate_spec(data, spec)
