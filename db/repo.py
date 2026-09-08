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
         determination, margin_pct, raw_data_json, raw_spec_json, feature_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
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
        )
