"""rag/retriever.py 통합 테스트 — 실제 Postgres(pgvector) 필요.

`docker compose up -d` 후 `pytest -m integration`으로 실행.
"""

from datetime import UTC, datetime

import pytest

from rag.retriever import hybrid_search

pytestmark = pytest.mark.integration

DIM = 1024
BASE = datetime(2026, 9, 1, tzinfo=UTC)

_INSERT_CHUNK_SQL = """
    INSERT INTO log_chunks
        (equipment_id, chunk_text, embedding, log_ids, period_start, period_end, error_codes, feature_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


def _one_hot(index: int, dim: int = DIM) -> list[float]:
    v = [0.0] * dim
    v[index] = 1.0
    return v


async def _insert(
    pool,
    *,
    equipment_id: str,
    embedding: list[float],
    feature_type: str = "log_general",
    text: str = "chunk",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_CHUNK_SQL,
            equipment_id,
            text,
            embedding,
            [1],
            BASE,
            BASE,
            [],
            feature_type,
        )


@pytest.mark.asyncio
async def test_orders_by_cosine_distance_nearest_first(db_pool):
    await _insert(db_pool, equipment_id="EQ-01", embedding=_one_hot(1), text="far")
    await _insert(db_pool, equipment_id="EQ-01", embedding=_one_hot(0), text="near")

    results = await hybrid_search(db_pool, _one_hot(0), feature_type="log_general", top_k=5)

    assert [r.chunk_text for r in results] == ["near", "far"]
    assert results[0].distance < results[1].distance


@pytest.mark.asyncio
async def test_filters_by_equipment_id(db_pool):
    await _insert(db_pool, equipment_id="EQ-01", embedding=_one_hot(0))
    await _insert(db_pool, equipment_id="EQ-02", embedding=_one_hot(0))

    results = await hybrid_search(
        db_pool, _one_hot(0), feature_type="log_general", equipment_id="EQ-01", top_k=5
    )

    assert len(results) == 1
    assert results[0].equipment_id == "EQ-01"


@pytest.mark.asyncio
async def test_filters_by_feature_type(db_pool):
    await _insert(db_pool, equipment_id="EQ-01", embedding=_one_hot(0), feature_type="log_general")
    await _insert(db_pool, equipment_id="EQ-01", embedding=_one_hot(0), feature_type="id_dump")

    results = await hybrid_search(db_pool, _one_hot(0), feature_type="id_dump", top_k=5)

    assert len(results) == 1
    assert results[0].feature_type == "id_dump"


@pytest.mark.asyncio
async def test_respects_top_k(db_pool):
    for _ in range(3):
        await _insert(db_pool, equipment_id="EQ-01", embedding=_one_hot(0))

    results = await hybrid_search(db_pool, _one_hot(0), feature_type="log_general", top_k=2)

    assert len(results) == 2


@pytest.mark.asyncio
async def test_filters_by_period(db_pool):
    async def _insert_with_period(period_start, period_end):
        async with db_pool.acquire() as conn:
            await conn.execute(
                _INSERT_CHUNK_SQL,
                "EQ-01",
                "chunk",
                _one_hot(0),
                [1],
                period_start,
                period_end,
                [],
                "log_general",
            )

    await _insert_with_period(BASE, BASE)
    later = datetime(2026, 9, 10, tzinfo=UTC)
    await _insert_with_period(later, later)

    results = await hybrid_search(
        db_pool,
        _one_hot(0),
        feature_type="log_general",
        period_start=datetime(2026, 9, 5, tzinfo=UTC),
        top_k=5,
    )

    assert len(results) == 1
    assert results[0].period_start == later
