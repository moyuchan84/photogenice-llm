from rag.evaluators.final_xy import FinalXYEvaluator

_SPEC = {"x": {"lsl": -5.0, "usl": 5.0}, "y": {"lsl": -5.0, "usl": 5.0}}


def test_both_axes_within_bounds_are_in_spec():
    result = FinalXYEvaluator().evaluate({"x": [0.0, 0.0, 0.0], "y": [1.0, 1.0, 1.0]}, _SPEC)
    assert result.determination == "IN_SPEC"


def test_upper_bound_exceeding_usl_is_out_of_spec():
    result = FinalXYEvaluator().evaluate({"x": [10.0, 10.0, 10.0], "y": [0.0, 0.0, 0.0]}, _SPEC)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_lower_bound_below_lsl_is_out_of_spec():
    result = FinalXYEvaluator().evaluate({"x": [-10.0, -10.0, -10.0], "y": [0.0, 0.0, 0.0]}, _SPEC)
    assert result.determination == "OUT_OF_SPEC"


def test_zero_width_spec_axis_value_matches_limit_is_in_spec():
    spec = {"x": {"lsl": 5.0, "usl": 5.0}, "y": {"lsl": -5.0, "usl": 5.0}}
    result = FinalXYEvaluator().evaluate({"x": [5.0, 5.0], "y": [0.0, 0.0]}, spec)
    assert result.determination == "IN_SPEC"
    assert result.metrics["x"]["sigma"] == 0.0


def test_single_point_axis_has_zero_sigma_and_equal_bounds():
    result = FinalXYEvaluator().evaluate({"x": [3.0], "y": [0.0]}, _SPEC)
    assert result.metrics["x"]["sigma"] == 0.0
    assert result.metrics["x"]["lower_bound"] == result.metrics["x"]["upper_bound"] == 3.0
    assert result.determination == "IN_SPEC"


def test_worse_axis_determines_overall_margin():
    # x well within spec (margin 100), y out of spec (margin -100) -> overall must reflect y.
    result = FinalXYEvaluator().evaluate({"x": [0.0, 0.0, 0.0], "y": [10.0, 10.0, 10.0]}, _SPEC)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0
