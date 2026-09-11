import pytest

from rag.spec_evaluator import GenericSpecEvaluator, evaluate_criterion, evaluate_spec


def _case(value, lsl, usl):
    return {"value": value}, {"lsl": lsl, "usl": usl}


def test_value_at_lsl_is_in_spec_with_zero_margin():
    data, spec = _case(10.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 0.0


def test_value_at_usl_is_in_spec_with_zero_margin():
    data, spec = _case(20.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 0.0


def test_value_at_center_is_in_spec_with_full_margin():
    data, spec = _case(15.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_value_within_range_has_partial_margin():
    data, spec = _case(17.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 60.0


def test_value_below_lsl_is_out_of_spec():
    data, spec = _case(5.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_value_above_usl_is_out_of_spec():
    data, spec = _case(30.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -200.0


def test_value_slightly_above_usl_has_small_negative_margin():
    data, spec = _case(21.0, 10, 20)
    result = evaluate_spec(data, spec)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -20.0


def test_margin_pct_rounds_to_two_decimal_places():
    data, spec = _case(1.0, 0, 3)
    result = evaluate_spec(data, spec)
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 66.67


def test_zero_width_spec_value_matches_limit_is_in_spec():
    data, spec = _case(15.0, 15, 15)
    result = evaluate_spec(data, spec)
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_zero_width_spec_value_above_limit_is_out_of_spec():
    data, spec = _case(16.0, 15, 15)
    result = evaluate_spec(data, spec)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_zero_width_spec_value_below_limit_is_out_of_spec():
    data, spec = _case(14.0, 15, 15)
    result = evaluate_spec(data, spec)
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_generic_spec_evaluator_delegates_to_evaluate_spec():
    data, spec = _case(17.0, 10, 20)
    assert GenericSpecEvaluator().evaluate(data, spec) == evaluate_spec(data, spec)


# --- evaluate_criterion (ftpmodule /spec/map operator/threshold 기준) --------------


def _criterion(operator, threshold, *, operator2="", threshold2=None, is_absolute=False):
    return {
        "operator": operator,
        "threshold": threshold,
        "operator2": operator2,
        "threshold2": threshold2,
        "is_absolute": is_absolute,
    }


def _two_sided_criterion():
    # -0.3 < v <= 1.2
    return _criterion("<=", 1.2, operator2=">", threshold2=-0.3)


def test_operator_lt_boundary_is_out_of_spec_with_zero_margin():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion("<", 5.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == 0.0


def test_operator_lt_below_threshold_is_in_spec():
    result = evaluate_criterion({"value": 4.0}, {"criterion": _criterion("<", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 20.0


def test_operator_le_boundary_is_in_spec_with_zero_margin():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion("<=", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 0.0


def test_operator_le_above_threshold_is_out_of_spec():
    result = evaluate_criterion({"value": 5.1}, {"criterion": _criterion("<=", 5.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -2.0


def test_operator_gt_boundary_is_out_of_spec_with_zero_margin():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion(">", 5.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == 0.0


def test_operator_gt_above_threshold_is_in_spec():
    result = evaluate_criterion({"value": 6.0}, {"criterion": _criterion(">", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 20.0


def test_operator_ge_boundary_is_in_spec_with_zero_margin():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion(">=", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 0.0


def test_operator_ge_below_threshold_is_out_of_spec():
    result = evaluate_criterion({"value": 4.9}, {"criterion": _criterion(">=", 5.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -2.0


def test_is_absolute_negative_value_at_boundary_is_out_of_spec():
    result = evaluate_criterion(
        {"value": -0.5}, {"criterion": _criterion("<", 0.5, is_absolute=True)}
    )
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == 0.0


def test_is_absolute_negative_value_within_bound_is_in_spec():
    result = evaluate_criterion(
        {"value": -0.43}, {"criterion": _criterion("<", 0.5, is_absolute=True)}
    )
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 14.0


def test_two_sided_upper_boundary_is_in_spec_with_zero_margin():
    result = evaluate_criterion({"value": 1.2}, {"criterion": _two_sided_criterion()})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 0.0


def test_two_sided_lower_boundary_is_out_of_spec_with_zero_margin():
    result = evaluate_criterion({"value": -0.3}, {"criterion": _two_sided_criterion()})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == 0.0


def test_two_sided_center_has_full_margin():
    result = evaluate_criterion({"value": 0.45}, {"criterion": _two_sided_criterion()})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_one_sided_upper_margin_caps_at_100():
    result = evaluate_criterion({"value": -1000.0}, {"criterion": _criterion("<", 10.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_one_sided_upper_zero_threshold_in_spec_has_full_margin():
    result = evaluate_criterion({"value": -1.0}, {"criterion": _criterion("<", 0.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_one_sided_upper_zero_threshold_out_of_spec_has_negative_full_margin():
    result = evaluate_criterion({"value": 1.0}, {"criterion": _criterion("<", 0.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_one_sided_lower_margin_caps_at_100():
    result = evaluate_criterion({"value": 1000.0}, {"criterion": _criterion(">=", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_one_sided_lower_zero_threshold_in_spec_has_full_margin():
    result = evaluate_criterion({"value": 1.0}, {"criterion": _criterion(">", 0.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_one_sided_lower_zero_threshold_out_of_spec_has_negative_full_margin():
    result = evaluate_criterion({"value": -1.0}, {"criterion": _criterion(">", 0.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_operator_eq_matching_value_is_in_spec_with_full_margin():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion("==", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_operator_eq_mismatched_value_is_out_of_spec():
    result = evaluate_criterion({"value": 5.1}, {"criterion": _criterion("==", 5.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_operator_ne_mismatched_value_is_in_spec():
    result = evaluate_criterion({"value": 5.1}, {"criterion": _criterion("!=", 5.0)})
    assert result.determination == "IN_SPEC"
    assert result.margin_pct == 100.0


def test_operator_ne_matching_value_is_out_of_spec():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion("!=", 5.0)})
    assert result.determination == "OUT_OF_SPEC"
    assert result.margin_pct == -100.0


def test_empty_operator2_string_is_ignored():
    result = evaluate_criterion({"value": 4.0}, {"criterion": _criterion("<", 5.0)})
    assert len(result.metrics["conditions"]) == 1


def test_unknown_operator_raises_value_error():
    with pytest.raises(ValueError):
        evaluate_criterion({"value": 1.0}, {"criterion": _criterion("<>", 5.0)})


def test_unknown_operator2_raises_value_error():
    spec = {"criterion": _criterion("<", 5.0, operator2="<>", threshold2=1.0)}
    with pytest.raises(ValueError):
        evaluate_criterion({"value": 1.0}, spec)


def test_operator2_without_threshold2_raises_value_error():
    spec = {"criterion": _criterion("<", 5.0, operator2=">", threshold2=None)}
    with pytest.raises(ValueError):
        evaluate_criterion({"value": 1.0}, spec)


def test_criterion_metrics_lsl_usl_measured_value_two_sided():
    result = evaluate_criterion({"value": 0.45}, {"criterion": _two_sided_criterion()})
    assert result.measured_value == 0.45
    assert result.lsl == -0.3
    assert result.usl == 1.2
    assert result.metrics == {
        "x": 0.45,
        "is_absolute": False,
        "lower": -0.3,
        "upper": 1.2,
        "conditions": [
            {"operator": "<=", "threshold": 1.2, "holds": True},
            {"operator": ">", "threshold": -0.3, "holds": True},
        ],
    }


def test_criterion_metrics_lsl_usl_absolute_only_upper_uses_negated_band():
    result = evaluate_criterion(
        {"value": -0.43}, {"criterion": _criterion("<", 0.5, is_absolute=True)}
    )
    assert result.measured_value == -0.43
    assert result.lsl == -0.5
    assert result.usl == 0.5
    assert result.metrics["x"] == 0.43
    assert result.metrics["is_absolute"] is True


def test_criterion_metrics_no_bounds_when_only_equality_operator():
    result = evaluate_criterion({"value": 5.0}, {"criterion": _criterion("==", 5.0)})
    assert result.lsl is None
    assert result.usl is None
    assert result.metrics["lower"] is None
    assert result.metrics["upper"] is None


def test_generic_spec_evaluator_dispatches_to_criterion_when_present():
    data, spec = {"value": 4.0}, {"criterion": _criterion("<", 5.0)}
    assert GenericSpecEvaluator().evaluate(data, spec) == evaluate_criterion(data, spec)
