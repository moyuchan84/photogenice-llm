"""rag/core.py 통합 테스트 — run_rag_judgement() 전체 흐름(검색 → LLM → judgements 저장).

실제 Postgres 필요. `docker compose up -d` 후 `pytest -m integration`으로 실행.
LLM/임베딩 클라이언트는 스텁으로 대체해 외부 API 의존 없이 흐름을 검증한다.
"""

from datetime import UTC, datetime

import pytest

from rag.core import run_rag_judgement

pytestmark = pytest.mark.integration

DIM = 1024
BASE = datetime(2026, 9, 1, tzinfo=UTC)

_INSERT_CHUNK_SQL = """
    INSERT INTO log_chunks
        (equipment_id, chunk_text, embedding, log_ids, period_start, period_end, error_codes, feature_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    RETURNING chunk_id
"""

_QUERY_VECTOR = [1.0] + [0.0] * (DIM - 1)


class _StubEmbeddingClient:
    def __init__(self, vector: list[float]):
        self._vector = vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


class _StubLLMClient:
    def __init__(self, response: dict):
        self._response = response

    async def generate_json(
        self, system_prompt: str, user_prompt: str, *, temperature: float = 0.0
    ) -> dict:
        return self._response


async def _insert_chunk(
    pool, *, equipment_id: str = "EQ-01", feature_type: str = "log_general"
) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            _INSERT_CHUNK_SQL,
            equipment_id,
            "테스트 청크",
            _QUERY_VECTOR,
            [1],
            BASE,
            BASE,
            [],
            feature_type,
        )


@pytest.mark.asyncio
async def test_run_rag_judgement_saves_and_returns_judgement(db_pool):
    chunk_id = await _insert_chunk(db_pool)
    embedding_client = _StubEmbeddingClient(_QUERY_VECTOR)
    llm_client = _StubLLMClient(
        {
            "conclusion": "과거 유사 사례와 동일한 원인으로 추정됨",
            "confidence": 0.8,
            "evidence_chunk_ids": [chunk_id],
            "recommended_action": "필터 교체 권장",
        }
    )

    result = await run_rag_judgement(
        db_pool,
        embedding_client,
        llm_client,
        use_case="history",
        query="EQ-01 최근 이상 원인은?",
        equipment_id="EQ-01",
    )

    assert result.conclusion == "과거 유사 사례와 동일한 원인으로 추정됨"
    assert result.confidence == 0.8
    assert result.evidence_chunk_ids == [chunk_id]
    assert result.retrieved_chunks[0].chunk_id == chunk_id

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM judgements WHERE judgement_id = $1", result.judgement_id
        )
    assert row["use_case"] == "history"
    assert row["equipment_id"] == "EQ-01"
    assert list(row["retrieved_chunk_ids"]) == [chunk_id]
    assert row["conclusion"] == "과거 유사 사례와 동일한 원인으로 추정됨"


@pytest.mark.asyncio
async def test_run_rag_judgement_falls_back_to_retrieved_ids_when_llm_omits_evidence(db_pool):
    chunk_id = await _insert_chunk(db_pool)
    embedding_client = _StubEmbeddingClient(_QUERY_VECTOR)
    llm_client = _StubLLMClient(
        {"conclusion": "원인 불명", "confidence": 0.2, "recommended_action": "추가 조사 필요"}
    )

    result = await run_rag_judgement(
        db_pool,
        embedding_client,
        llm_client,
        use_case="history",
        query="질의",
    )

    assert result.evidence_chunk_ids == [chunk_id]


@pytest.mark.asyncio
async def test_run_rag_judgement_respects_feature_type_filter(db_pool):
    await _insert_chunk(db_pool, feature_type="id_dump")
    embedding_client = _StubEmbeddingClient(_QUERY_VECTOR)
    llm_client = _StubLLMClient({"conclusion": "관련 사례 없음", "confidence": 0.1})

    result = await run_rag_judgement(
        db_pool,
        embedding_client,
        llm_client,
        use_case="history",
        query="질의",
        feature_type="log_general",
    )

    assert result.retrieved_chunks == []
    assert result.evidence_chunk_ids == []
