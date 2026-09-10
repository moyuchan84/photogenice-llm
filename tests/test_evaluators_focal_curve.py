from rag.evaluators.focal_curve import FocalCurveEvaluator


def test_range_exactly_at_usl_is_in_spec_with_zero_margin():
    # curve: min=0, max=10 -> range=10
    result = FocalCurveEvaluator().evaluate({"curve": [0.0, 10.0]}, {"lsl": 0.0, "usl": 10.0})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 0.0
    assert result.metrics["range"] == 10.0
    assert result.metrics["dof"] == 1
    assert result.metrics["n"] == 2


def test_range_above_usl_is_out_of_spec():
    result = FocalCurveEvaluator().evaluate({"curve": [0.0, 12.0]}, {"lsl": 0.0, "usl": 10.0})
    assert result.determination == "OUT_OF_SPEC"


def test_single_point_curve_has_zero_range_and_zero_sigma():
    result = FocalCurveEvaluator().evaluate({"curve": [5.0]}, {"lsl": 0.0, "usl": 10.0})
    assert result.metrics["range"] == 0.0
    assert result.metrics["sigma"] == 0.0
    assert result.metrics["dof"] == 0
    assert result.determination == "IN_SPEC"


def test_zero_width_spec_range_matches_limit_is_in_spec():
    result = FocalCurveEvaluator().evaluate({"curve": [1.0, 1.0]}, {"lsl": 0.0, "usl": 0.0})
    assert result.metrics["range"] == 0.0
    assert result.determination == "IN_SPEC"


def test_zero_width_spec_range_above_limit_is_out_of_spec():
    result = FocalCurveEvaluator().evaluate({"curve": [0.0, 1.0]}, {"lsl": 0.0, "usl": 0.0})
    assert result.determination == "OUT_OF_SPEC"


def test_sigma_computed_over_curve_points():
    result = FocalCurveEvaluator().evaluate({"curve": [0.0, 5.0, 10.0]}, {"lsl": 0.0, "usl": 20.0})
    assert result.metrics["dof"] == 2
    assert result.metrics["sigma"] > 0.0
