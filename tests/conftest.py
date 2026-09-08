import os

import asyncpg
import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL", "postgresql://asmr:asmr@localhost:5432/asmr_rag"
)


@pytest_asyncio.fixture
async def db_pool():
    from config import Settings
    from db.repo import create_pool

    try:
        pool = await create_pool(Settings(_env_file=None, database_url=TEST_DATABASE_URL))
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"Postgres 미기동 또는 접속 불가: {e}")
        return

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE logs_raw, log_chunks, spec_evaluations, judgements RESTART IDENTITY CASCADE"
            )
        yield pool
    finally:
        await pool.close()
