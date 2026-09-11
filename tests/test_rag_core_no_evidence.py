"""rag/core.py::run_rag_judgement() — 검색 근거가 0건일 때 LLM을 호출하지 않는 가드.

배경: 신규 3개 기능(focal_curve/final_xy/id_dump)은 검색 시 feature_type으로
필터링하므로, 해당 feature_type의 청크가 아직 적재되지 않았으면 근거가 0건이 된다.
이때 LLM을 그대로 호출하면 근거 없는 conclusion이 judgements에 감사 기록으로 남는다.
"""

from unittest.mock import AsyncMock

import pytest

from rag import core


@pytest.fixture
def patched(monkeypatch):
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(core, "hybrid_search", mock_search)
    mock_save = AsyncMock(return_value=42)
    monkeypatch.setattr(core, "save_judgement", mock_save)
    embedding_client = AsyncMock()
    embedding_client.embed = AsyncMock(return_value=[[0.1] * 8])
    llm_client = AsyncMock()
    return mock_search, mock_save, embedding_client, llm_client


async def _run(embedding_client, llm_client, **overrides):
    kwargs = {
        "use_case": "spec_check",
        "query": "focal curve 편차 원인",
        "feature_type": "focal_curve",
        "equipment_id": "EQ-01",
        "eval_id": 7,
    }
    kwargs.update(overrides)
    return await core.run_rag_judgement(None, embedding_client, llm_client, **kwargs)


async def test_no_chunks_skips_llm_call(patched):
    _, _, embedding_client, llm_client = patched

    result = await _run(embedding_client, llm_client)

    llm_client.generate_json.assert_not_called()
    assert result.conclusion is None
    assert result.recommended_action is None
    assert result.confidence == 0.0
    assert result.evidence_chunk_ids == []
    assert result.retrieved_chunks == []


async def test_no_chunks_still_saves_audit_record(patched):
    """판정 이력 자체는 생략하지 않는다 (CLAUDE.md '감사 추적, 생략 금지')."""
    _, mock_save, embedding_client, llm_client = patched

    result = await _run(embedding_client, llm_client)

    mock_save.assert_called_once()
    _, kwargs = mock_save.call_args
    assert result.judgement_id == 42
    assert kwargs["use_case"] == "spec_check"
    assert kwargs["feature_type"] == "focal_curve"
    assert kwargs["equipment_id"] == "EQ-01"
    assert kwargs["eval_id"] == 7
    assert kwargs["retrieved_chunk_ids"] == []
    assert kwargs["conclusion"] is None
    assert kwargs["raw_response"]["skipped"] is True
    assert "reason" in kwargs["raw_response"]


async def test_chunks_present_still_calls_llm(monkeypatch, patched):
    """가드가 정상 경로를 막지 않는지 — 근거가 1건이라도 있으면 기존대로 LLM을 호출한다."""
    from datetime import UTC, datetime

    from rag.retriever import RetrievedChunk

    _, mock_save, embedding_client, llm_client = patched
    chunk = RetrievedChunk(
        chunk_id=3,
        equipment_id="EQ-01",
        chunk_text="과거 사례",
        log_ids=[1],
        period_start=datetime(2026, 9, 8, tzinfo=UTC),
        period_end=datetime(2026, 9, 8, tzinfo=UTC),
        error_codes=["E1"],
        feature_type="focal_curve",
        distance=0.12,
    )
    monkeypatch.setattr(core, "hybrid_search", AsyncMock(return_value=[chunk]))
    llm_client.generate_json = AsyncMock(
        return_value={
            "conclusion": "렌즈 가열 드리프트",
            "confidence": 0.7,
            "recommended_action": "재캘리브레이션",
            "evidence_chunk_ids": [3],
        }
    )

    result = await _run(embedding_client, llm_client)

    llm_client.generate_json.assert_awaited_once()
    assert result.conclusion == "렌즈 가열 드리프트"
    _, kwargs = mock_save.call_args
    assert kwargs["retrieved_chunk_ids"] == [3]
