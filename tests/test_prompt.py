from datetime import UTC, datetime

from rag.prompt import build_system_prompt, build_user_prompt
from rag.retriever import RetrievedChunk


def _chunk(chunk_id=1, error_codes=None):
    return RetrievedChunk(
        chunk_id=chunk_id,
        equipment_id="EQ-01",
        chunk_text="설비 로그 내용",
        log_ids=[1, 2],
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, 0, 5, tzinfo=UTC),
        error_codes=error_codes or [],
        feature_type="log_general",
        distance=0.12,
    )


def test_build_system_prompt_declares_json_schema_fields():
    prompt = build_system_prompt()
    assert "conclusion" in prompt
    assert "confidence" in prompt
    assert "evidence_chunk_ids" in prompt
    assert "recommended_action" in prompt


def test_build_system_prompt_appends_domain_notes_without_losing_schema():
    prompt = build_system_prompt("focal curve는 필드 중심 편차가 더 중요하다")
    assert "focal curve는 필드 중심 편차가 더 중요하다" in prompt
    assert "conclusion" in prompt


def test_build_user_prompt_includes_query_and_chunk_text():
    prompt = build_user_prompt("EQ-01 최근 에러 원인은?", [_chunk()])
    assert "EQ-01 최근 에러 원인은?" in prompt
    assert "설비 로그 내용" in prompt
    assert "chunk_id=1" in prompt


def test_build_user_prompt_includes_error_codes_when_present():
    prompt = build_user_prompt("질의", [_chunk(error_codes=["E1", "E2"])])
    assert "에러코드: E1, E2" in prompt


def test_build_user_prompt_handles_no_chunks():
    prompt = build_user_prompt("질의", [])
    assert "검색된 과거 사례 없음" in prompt
