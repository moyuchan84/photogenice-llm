import pytest

from clients.llm_client import LLMResponseParseError, parse_json_response


def test_parse_plain_json():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_json_with_code_fence():
    raw = '```json\n{"a": 1, "b": "x"}\n```'
    assert parse_json_response(raw) == {"a": 1, "b": "x"}


def test_parse_json_with_bare_fence_no_language():
    raw = '```\n{"a": 1}\n```'
    assert parse_json_response(raw) == {"a": 1}


def test_parse_json_strips_surrounding_whitespace():
    raw = '\n\n  {"a": 1}  \n\n'
    assert parse_json_response(raw) == {"a": 1}


def test_invalid_json_raises_parse_error():
    with pytest.raises(LLMResponseParseError):
        parse_json_response("이건 JSON이 아닙니다")
