"""UC1 임베딩 동기화 워커 — DB polling 기반, APScheduler로 주기 실행.

멱등성: SELECT/임베딩 호출은 트랜잭션 밖에서, INSERT(log_chunks)+UPDATE(logs_raw.embedded_at)는
하나의 트랜잭션으로 묶어 재시작 시 중복 임베딩 없이 이어서 처리되도록 보장한다.
동시성 제어(SKIP LOCKED)는 Phase 1 범위 제외 (단일 워커 전제, 00-requirements.md §2.2).
"""

import asyncio
import logging
from datetime import UTC, datetime

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from clients.embedding_client import EmbeddingClient, get_embedding_client
from config import Settings, get_settings
from db.repo import create_pool
from rag.features import build_log_keyword_map
from workers.chunker import LogChunk, RawLogRow, chunk_logs

logger = logging.getLogger(__name__)

__all__ = ["create_pool", "main", "run_embedding_sync_cycle"]

_SELECT_UNEMBEDDED_SQL = """
    SELECT log_id, equipment_id, event_time, error_code, log_level, message
    FROM logs_raw
    WHERE embedded_at IS NULL
    ORDER BY event_time
    LIMIT $1
"""

_INSERT_CHUNK_SQL = """
    INSERT INTO log_chunks
        (equipment_id, chunk_text, embedding, log_ids, period_start, period_end, error_codes, feature_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

_MARK_EMBEDDED_SQL = "UPDATE logs_raw SET embedded_at = now() WHERE log_id = ANY($1::bigint[])"


def _zip_chunks_with_embeddings(
    chunks: list[LogChunk], embeddings: list[list[float]]
) -> list[tuple[LogChunk, list[float]]]:
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"chunk 수({len(chunks)})와 embedding 수({len(embeddings)})가 일치하지 않습니다."
        )
    return list(zip(chunks, embeddings))


async def run_embedding_sync_cycle(
    pool: asyncpg.Pool,
    embedding_client: EmbeddingClient,
    *,
    batch_size: int,
    session_gap_sec: int,
    error_window_before: int,
    error_window_after: int,
) -> int:
    """폴링 1회분 처리. 반환값 = 이번 사이클에서 처리(embedded_at 갱신)된 row 수."""
    async with pool.acquire() as conn:
        records = await conn.fetch(_SELECT_UNEMBEDDED_SQL, batch_size)
    if not records:
        return 0

    raw_rows = [
        RawLogRow(
            log_id=r["log_id"],
            equipment_id=r["equipment_id"],
            event_time=r["event_time"],
            error_code=r["error_code"],
            log_level=r["log_level"],
            message=r["message"],
        )
        for r in records
    ]

    # feature_type 분류 키워드는 FEATURE_REGISTRY가 소유한다 — chunker는 순수 함수로
    # 유지해야 하므로 여기서 읽어 주입한다. 이 주입이 빠지면 모든 청크가 log_general로
    # 적재되어 focal_curve/final_xy/id_dump 엔드포인트의 검색이 항상 0건이 된다.
    chunks = chunk_logs(
        raw_rows,
        session_gap_sec=session_gap_sec,
        error_window_before=error_window_before,
        error_window_after=error_window_after,
        feature_keywords=build_log_keyword_map(),
    )

    # embed() 호출은 DB 트랜잭션 밖에서 실행된다 — 느린 네트워크 호출 동안 커넥션을 점유하지 않기 위함.
    # 이 시점에 크래시가 나도 아무것도 커밋되지 않았으므로 다음 폴링에서 동일 row가 다시 처리된다(멱등).
    embeddings = await embedding_client.embed([c.chunk_text for c in chunks]) if chunks else []
    pairs = _zip_chunks_with_embeddings(chunks, embeddings)
    log_ids_flat = [r.log_id for r in raw_rows]

    async with pool.acquire() as conn, conn.transaction():
        for chunk, embedding in pairs:
            await conn.execute(
                _INSERT_CHUNK_SQL,
                chunk.equipment_id,
                chunk.chunk_text,
                embedding,
                chunk.log_ids,
                chunk.period_start,
                chunk.period_end,
                chunk.error_codes,
                chunk.feature_type,
            )
        # 청크 윈도우 밖이라 어떤 chunk에도 포함되지 않은 row도 포함해 전부 embedded_at을 갱신한다.
        # (그렇지 않으면 해당 row가 매 폴링마다 계속 재조회되어 무한 재시도된다.)
        await conn.execute(_MARK_EMBEDDED_SQL, log_ids_flat)

    logger.info("embedding sync cycle: %d rows -> %d chunks", len(raw_rows), len(chunks))
    return len(raw_rows)


async def _run_forever(settings: Settings) -> None:
    pool = await create_pool(settings)
    embedding_client = get_embedding_client(settings)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_embedding_sync_cycle,
        trigger="interval",
        seconds=settings.embedding_sync_poll_interval_sec,
        next_run_time=datetime.now(UTC),  # 시작 즉시 1회 실행 후 주기 반복
        args=[pool, embedding_client],
        kwargs={
            "batch_size": settings.embedding_sync_batch_size,
            "session_gap_sec": settings.chunk_session_gap_sec,
            "error_window_before": settings.chunk_error_window_before,
            "error_window_after": settings.chunk_error_window_after,
        },
        id="embedding_sync_poller",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)
        await embedding_client.aclose()
        await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_forever(get_settings()))


if __name__ == "__main__":
    main()
