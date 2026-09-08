"""멱등성 통합 테스트 (FR-1.2): 폴링 워커를 강제 중단 후 재시작해도 중복 임베딩이 생기지 않아야 한다.

실제 Postgres(docker-compose)가 필요하다: `docker compose up -d` 후 `pytest -m integration`으로 실행.
"""

from datetime import UTC, datetime, timedelta

import pytest

from workers.embedding_sync_poller import run_embedding_sync_cycle

pytestmark = pytest.mark.integration

BASE = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)

_CYCLE_KW = {
    "batch_size": 100,
    "session_gap_sec": 600,
    "error_window_before": 2,
    "error_window_after": 2,
}


class _FailingEmbeddingClient:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("simulated crash during embedding call")


class _StubEmbeddingClient:
    def __init__(self, dim: int = 1024):
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dim for _ in texts]


async def _insert_sample_rows(pool):
    rows = [
        (
            "EQ-01",
            BASE + timedelta(minutes=i),
            "E1" if i == 2 else None,
            "ERROR" if i == 2 else "INFO",
            f"message {i}",
        )
        for i in range(5)
    ]
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO logs_raw (equipment_id, event_time, error_code, log_level, message)
            VALUES ($1, $2, $3, $4, $5)
            """,
            rows,
        )


async def _unembedded_count(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM logs_raw WHERE embedded_at IS NULL")


async def _chunk_count(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM log_chunks")


@pytest.mark.asyncio
async def test_crash_during_embedding_leaves_no_partial_state(db_pool):
    await _insert_sample_rows(db_pool)

    with pytest.raises(RuntimeError):
        await run_embedding_sync_cycle(db_pool, _FailingEmbeddingClient(), **_CYCLE_KW)

    assert await _chunk_count(db_pool) == 0
    assert await _unembedded_count(db_pool) == 5


@pytest.mark.asyncio
async def test_restart_after_crash_processes_without_duplicates(db_pool):
    await _insert_sample_rows(db_pool)

    with pytest.raises(RuntimeError):
        await run_embedding_sync_cycle(db_pool, _FailingEmbeddingClient(), **_CYCLE_KW)

    processed = await run_embedding_sync_cycle(db_pool, _StubEmbeddingClient(), **_CYCLE_KW)
    assert processed == 5
    assert await _unembedded_count(db_pool) == 0
    chunk_count_after_first_success = await _chunk_count(db_pool)
    assert chunk_count_after_first_success > 0

    # 동일 사이클 재실행 시 더 이상 처리할 row가 없어야 하고, log_chunks도 늘어나지 않아야 한다.
    processed_again = await run_embedding_sync_cycle(db_pool, _StubEmbeddingClient(), **_CYCLE_KW)
    assert processed_again == 0
    assert await _chunk_count(db_pool) == chunk_count_after_first_success
