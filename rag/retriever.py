"""하이브리드 검색 — log_chunks에서 메타데이터 필터 + pgvector 코사인 유사도.

feature_type 필터는 항상 적용된다(log_general이 UC1 기본값) — 메타데이터 필터
없이 전체 테이블에 벡터 유사도 검색을 거는 것을 금지하는 원칙(CLAUDE.md) 때문이다.
equipment_id/기간은 선택적으로 추가 필터링한다.
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg

_HYBRID_SEARCH_SQL = """
    SELECT chunk_id, equipment_id, chunk_text, log_ids, period_start, period_end,
           error_codes, feature_type, embedding <=> $1 AS distance
    FROM log_chunks
    WHERE feature_type = $2
      AND ($3::text IS NULL OR equipment_id = $3)
      AND ($4::timestamptz IS NULL OR period_end >= $4)
      AND ($5::timestamptz IS NULL OR period_start <= $5)
    ORDER BY embedding <=> $1
    LIMIT $6
"""


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    equipment_id: str
    chunk_text: str
    log_ids: list[int]
    period_start: datetime
    period_end: datetime
    error_codes: list[str]
    feature_type: str
    distance: float


async def hybrid_search(
    pool: asyncpg.Pool,
    query_embedding: list[float],
    *,
    feature_type: str = "log_general",
    equipment_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    async with pool.acquire() as conn:
        records = await conn.fetch(
            _HYBRID_SEARCH_SQL,
            query_embedding,
            feature_type,
            equipment_id,
            period_start,
            period_end,
            top_k,
        )
    return [
        RetrievedChunk(
            chunk_id=r["chunk_id"],
            equipment_id=r["equipment_id"],
            chunk_text=r["chunk_text"],
            log_ids=list(r["log_ids"]),
            period_start=r["period_start"],
            period_end=r["period_end"],
            error_codes=list(r["error_codes"]),
            feature_type=r["feature_type"],
            distance=r["distance"],
        )
        for r in records
    ]
