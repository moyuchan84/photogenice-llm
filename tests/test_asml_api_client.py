import pytest

from clients.asml_api_client import HttpAsmlApiClient


def test_parse_response_splits_data_and_spec():
    payload = {
        "data": {"equipment_id": "EQ-01", "parameter": "focus", "value": 12.3},
        "spec": {"parameter": "focus", "lsl": 10.0, "usl": 20.0},
    }
    data, spec = HttpAsmlApiClient._parse_response(payload)
    assert data == payload["data"]
    assert spec == payload["spec"]


def test_parse_response_missing_data_key_raises():
    with pytest.raises(ValueError):
        HttpAsmlApiClient._parse_response({"spec": {"lsl": 1, "usl": 2}})


def test_parse_response_missing_spec_key_raises():
    with pytest.raises(ValueError):
        HttpAsmlApiClient._parse_response({"data": {"value": 1}})
