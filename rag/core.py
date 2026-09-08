"""RAG 코어 — run_rag_judgement(): UC1(history)과 UC2(spec_check의 조건부 설명 생성)가
공유하는 단일 함수. 유즈케이스별로 복사해서 만들지 않는다 (CLAUDE.md, NFR-7).
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from clients.embedding_client import EmbeddingClient
from clients.llm_client import LLMClient
from db.repo import save_judgement
from rag.prompt import build_system_prompt, build_user_prompt
from rag.retriever import RetrievedChunk, hybrid_search


@dataclass(frozen=True)
class RagJudgementResult:
    judgement_id: int
    conclusion: str | None
    confidence: float | None
    recommended_action: str | None
    evidence_chunk_ids: list[int]
    retrieved_chunks: list[RetrievedChunk]


async def run_rag_judgement(
    pool: asyncpg.Pool,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
    *,
    use_case: str,
    query: str,
    feature_type: str = "log_general",
    equipment_id: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    prompt_context: str | None = None,
    eval_id: int | None = None,
    top_k: int = 5,
) -> RagJudgementResult:
    [query_embedding] = await embedding_client.embed([query])

    chunks = await hybrid_search(
        pool,
        query_embedding,
        feature_type=feature_type,
        equipment_id=equipment_id,
        period_start=period_start,
        period_end=period_end,
        top_k=top_k,
    )

    system_prompt = build_system_prompt(prompt_context)
    user_prompt = build_user_prompt(query, chunks)
    response = await llm_client.generate_json(system_prompt, user_prompt)

    conclusion = response.get("conclusion")
    confidence = response.get("confidence")
    recommended_action = response.get("recommended_action")
    evidence_chunk_ids = response.get("evidence_chunk_ids") or [c.chunk_id for c in chunks]

    judgement_id = await save_judgement(
        pool,
        use_case=use_case,
        feature_type=feature_type,
        equipment_id=equipment_id,
        eval_id=eval_id,
        query_text=query,
        retrieved_chunk_ids=[c.chunk_id for c in chunks],
        conclusion=conclusion,
        confidence=confidence,
        recommended_action=recommended_action,
        raw_response=response,
    )

    return RagJudgementResult(
        judgement_id=judgement_id,
        conclusion=conclusion,
        confidence=confidence,
        recommended_action=recommended_action,
        evidence_chunk_ids=evidence_chunk_ids,
        retrieved_chunks=chunks,
    )
