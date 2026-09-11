"""우리 DB의 logs_raw에 scenario.HISTORY_INCIDENTS(과거 이력 텍스트 로그)를 넣고 임베딩까지 끝낸다.

ftpmodule DB에는 텍스트 로그 테이블이 없으므로 이 부분은 "부서 ETL이 logs_raw에 적재했다"고
가정한 목업이다. 임베딩은 실제 워커 함수(run_embedding_sync_cycle)를 그대로 반복 호출한다
(scripts/backfill_embeddings.py와 같은 경로 — 검증용 사본 없음).

대상은 equipment_id가 'MOCK-'로 시작하는 row로 한정한다.
  기본       : MOCK- 로그가 이미 있으면 적재는 건너뛰고 임베딩만 이어서 한다(재실행 안전).
  --reset    : MOCK- 범위의 judgements -> spec_evaluations -> log_chunks -> logs_raw 를 지운 뒤 다시 적재.

실행(저장소 루트, 우리 venv — .env의 DATABASE_URL/Ollama 설정 사용):
    .venv/Scripts/python -m scripts.mock_fleet.seed_history_logs [--reset]
"""

import argparse
import asyncio
from datetime import datetime, timedelta

from clients.embedding_client import get_embedding_client
from config import get_settings
from scripts.mock_fleet import scenario
from workers.embedding_sync_poller import create_pool, run_embedding_sync_cycle

MOCK_PREFIX = "MOCK-%"

_INSERT_SQL = """
    INSERT INTO logs_raw (equipment_id, event_time, error_code, log_level, message)
    VALUES ($1, $2, $3, $4, $5)
"""


def build_rows() -> list[tuple]:
    rows = []
    for incident in scenario.HISTORY_INCIDENTS:
        start = datetime.fromisoformat(incident["start"])
        for i, (level, code, message) in enumerate(incident["logs"]):
            rows.append(
                (incident["equipment_id"], start + timedelta(seconds=60 * i), code, level, message)
            )
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="MOCK- 범위 데이터를 지우고 다시 적재")
    args = parser.parse_args()

    settings = get_settings()
    pool = await create_pool(settings)
    embedding_client = get_embedding_client(settings)
    try:
        async with pool.acquire() as conn, conn.transaction():
            if args.reset:
                for table in ("judgements", "spec_evaluations", "log_chunks", "logs_raw"):
                    status = await conn.execute(
                        f"DELETE FROM {table} WHERE equipment_id LIKE $1", MOCK_PREFIX
                    )
                    print(f"reset {table}: {status}")
            existing = await conn.fetchval(
                "SELECT count(*) FROM logs_raw WHERE equipment_id LIKE $1", MOCK_PREFIX
            )
            if existing:
                print(f"logs_raw에 MOCK- 로그 {existing}건이 이미 있어 적재를 건너뜁니다.")
            else:
                rows = build_rows()
                await conn.executemany(_INSERT_SQL, rows)
                print(f"logs_raw 적재: {len(rows)}건")

        total = 0
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
            if processed == 0:
                break
        print(f"임베딩 동기화: {total}건 처리")

        async with pool.acquire() as conn:
            chunks = await conn.fetch(
                "SELECT equipment_id, feature_type, count(*) AS n FROM log_chunks"
                " WHERE equipment_id LIKE $1 GROUP BY 1, 2 ORDER BY 1, 2",
                MOCK_PREFIX,
            )
        for r in chunks:
            print(f"  log_chunks {r['equipment_id']:<12} {r['feature_type']:<12} {r['n']}건")
    finally:
        await embedding_client.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
