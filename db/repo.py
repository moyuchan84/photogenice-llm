"""asyncpg 커넥션 풀 생성 및 judgements 저장 쿼리.

풀 생성(create_pool)은 워커/API 등 여러 진입점이 공유하는 단일 지점이다 — 중복 생성 금지.
"""

import json
from datetime import datetime

import asyncpg
from pgvector.asyncpg import register_vector

from config import Settings, get_settings

_INSERT_JUDGEMENT_SQL = """
    INSERT INTO judgements
        (use_case, feature_type, equipment_id, eval_id, query_text,
         retrieved_chunk_ids, conclusion, confidence, recommended_action, raw_response)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    RETURNING judgement_id
"""

_INSERT_SPEC_EVALUATION_SQL = """
    INSERT INTO spec_evaluations
        (equipment_id, parameter, measured_value, unit, lsl, usl, target, measured_at,
         determination, margin_pct, raw_data_json, raw_spec_json, feature_type, metrics_json)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
    RETURNING eval_id
"""


async def create_pool(settings: Settings | None = None) -> asyncpg.Pool:
    settings = settings or get_settings()

    async def _init(conn: asyncpg.Connection) -> None:
        await register_vector(conn)

    return await asyncpg.create_pool(settings.database_url, init=_init)


async def save_judgement(
    pool: asyncpg.Pool,
    *,
    use_case: str,
    feature_type: str | None,
    equipment_id: str | None,
    eval_id: int | None,
    query_text: str | None,
    retrieved_chunk_ids: list[int],
    conclusion: str | None,
    confidence: float | None,
    recommended_action: str | None,
    raw_response: dict,
) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            _INSERT_JUDGEMENT_SQL,
            use_case,
            feature_type,
            equipment_id,
            eval_id,
            query_text,
            retrieved_chunk_ids,
            conclusion,
            confidence,
            recommended_action,
            json.dumps(raw_response),
        )


async def save_spec_evaluation(
    pool: asyncpg.Pool,
    *,
    equipment_id: str,
    parameter: str,
    measured_value: float | None,
    unit: str | None,
    lsl: float | None,
    usl: float | None,
    target: float | None,
    measured_at: datetime | None,
    determination: str,
    margin_pct: float | None,
    raw_data: dict,
    raw_spec: dict,
    feature_type: str = "generic",
    metrics: dict | None = None,
) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            _INSERT_SPEC_EVALUATION_SQL,
            equipment_id,
            parameter,
            measured_value,
            unit,
            lsl,
            usl,
            target,
            measured_at,
            determination,
            margin_pct,
            json.dumps(raw_data),
            json.dumps(raw_spec),
            feature_type,
            json.dumps(metrics) if metrics is not None else None,
        )


# ---------------------------------------------------------------------------
# 읽기 전용 조회 — 감사 추적(FR-6.1) readback 및 향후 MCP 툴(FR-7.2, "재계산 금지,
# 이미 저장된 결과 조회만")이 공유하는 지점. 쓰기 쿼리와 달리 판정 로직을 전혀
# 포함하지 않는다.
# ---------------------------------------------------------------------------

_SELECT_SPEC_EVALUATION_SQL = """
    SELECT eval_id, equipment_id, parameter, measured_value, unit, lsl, usl, target,
           measured_at, determination, margin_pct, raw_data_json, raw_spec_json,
           feature_type, metrics_json, created_at
    FROM spec_evaluations
    WHERE eval_id = $1
"""

_SELECT_JUDGEMENT_SQL = """
    SELECT judgement_id, use_case, feature_type, equipment_id, eval_id, query_text,
           retrieved_chunk_ids, conclusion, confidence, recommended_action,
           raw_response, created_at
    FROM judgements
    WHERE judgement_id = $1
"""

# array_position으로 정렬해 호출자가 넘긴 chunk_id 순서(= 유사도 순)를 보존한다.
_SELECT_CHUNKS_BY_IDS_SQL = """
    SELECT chunk_id, equipment_id, chunk_text, log_ids, period_start, period_end,
           error_codes, feature_type, created_at
    FROM log_chunks
    WHERE chunk_id = ANY($1::bigint[])
    ORDER BY array_position($1::bigint[], chunk_id)
"""


async def fetch_spec_evaluation(pool: asyncpg.Pool, eval_id: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(_SELECT_SPEC_EVALUATION_SQL, eval_id)


async def fetch_judgement(pool: asyncpg.Pool, judgement_id: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(_SELECT_JUDGEMENT_SQL, judgement_id)


async def fetch_chunks_by_ids(pool: asyncpg.Pool, chunk_ids: list[int]) -> list[asyncpg.Record]:
    if not chunk_ids:
        return []
    async with pool.acquire() as conn:
        return list(await conn.fetch(_SELECT_CHUNKS_BY_IDS_SQL, chunk_ids))


# /chat 탐색용 읽기 전용 조회 — 이미 저장된 판정/설명 이력을 보여줄 뿐 새로 판정하지 않는다
# (FR-7.2). spec_evaluations 1건에 judgements는 최대 1건(OOS/근접 시)이다.
_SELECT_RECENT_EVALUATIONS_SQL = """
    SELECT e.eval_id, e.equipment_id, e.parameter, e.feature_type, e.measured_value, e.unit,
           e.lsl, e.usl, e.determination, e.margin_pct, e.measured_at, e.created_at,
           j.judgement_id, j.conclusion, j.recommended_action
    FROM spec_evaluations e
    LEFT JOIN judgements j ON j.eval_id = e.eval_id
    WHERE ($1::text IS NULL OR e.equipment_id = $1)
      AND ($2::text IS NULL OR e.parameter = $2)
      AND (NOT $3::boolean OR e.determination = 'OUT_OF_SPEC')
    ORDER BY e.created_at DESC
    LIMIT $4
"""


async def fetch_recent_evaluations(
    pool: asyncpg.Pool,
    *,
    equipment_id: str | None = None,
    parameter: str | None = None,
    only_out_of_spec: bool = False,
    limit: int = 10,
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return list(
            await conn.fetch(
                _SELECT_RECENT_EVALUATIONS_SQL, equipment_id, parameter, only_out_of_spec, limit
            )
        )
