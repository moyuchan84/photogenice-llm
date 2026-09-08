"""1회성 백필: logs_raw의 embedded_at IS NULL row가 없어질 때까지
run_embedding_sync_cycle을 반복 호출한다 (embedding_sync_poller.py 로직 재사용, 중복 구현 없음)."""

import asyncio
import logging

from clients.embedding_client import get_embedding_client
from config import get_settings
from workers.embedding_sync_poller import create_pool, run_embedding_sync_cycle

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    pool = await create_pool(settings)
    embedding_client = get_embedding_client(settings)
    total = 0
    try:
        while True:
            processed = await run_embedding_sync_cycle(
                pool,
                embedding_client,
                batch_size=settings.embedding_sync_batch_size,
                session_gap_sec=settings.chunk_session_gap_sec,
                error_window_before=settings.chunk_error_window_before,
                error_window_after=settings.chunk_error_window_after,
            )
            total += processed
            logger.info("processed %d rows (total %d)", processed, total)
            if processed == 0:
                break
    finally:
        await embedding_client.aclose()
        await pool.close()
    logger.info("backfill done. total processed: %d", total)


if __name__ == "__main__":
    asyncio.run(main())
