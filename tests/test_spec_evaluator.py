from rag.spec_evaluator import GenericSpecEvaluator, evaluate_spec


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
