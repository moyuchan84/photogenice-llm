from unittest.mock import AsyncMock

from rag import id_dump
from rag.core import RagJudgementResult


async def test_run_id_dump_analysis_calls_run_rag_judgement_with_id_dump_scope(monkeypatch):
    fake_result = RagJudgementResult(
        judgement_id=3,
        conclusion="과거 유사 사례 기준 원인 추정",
        confidence=0.7,
        recommended_action="점검 권고",
        evidence_chunk_ids=[10, 11],
        retrieved_chunks=[],
    )
    mock_rag = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(id_dump, "run_rag_judgement", mock_rag)

    result = await id_dump.run_id_dump_analysis(
        None, None, None, equipment_id="EQ-01", error_dump_text="ERR_CODE_1234 timeout", top_k=5
    )

    assert result is fake_result
    mock_rag.assert_called_once()
    _, kwargs = mock_rag.call_args
    assert kwargs["use_case"] == "root_cause"
    assert kwargs["feature_type"] == "id_dump"
    assert kwargs["equipment_id"] == "EQ-01"
    assert "ERR_CODE_1234" in kwargs["query"]
    assert kwargs["prompt_context"]  # id_dump 도메인 노트가 전달됨
