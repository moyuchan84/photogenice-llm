"""UC1: POST /query/history — 자연어 질의 → 하이브리드 검색 → LLM 판정/결론 (FR-1.3)."""

from fastapi import APIRouter

from api.deps import DbPool, EmbeddingClientDep, LLMClientDep
from models.schemas import HistoryQueryRequest, HistoryQueryResponse
from rag.core import run_rag_judgement

router = APIRouter(prefix="/query", tags=["history"])


@router.post("/history", response_model=HistoryQueryResponse)
async def query_history(
    req: HistoryQueryRequest,
    pool: DbPool,
    embedding_client: EmbeddingClientDep,
    llm_client: LLMClientDep,
) -> HistoryQueryResponse:
    result = await run_rag_judgement(
        pool,
        embedding_client,
        llm_client,
        use_case="history",
        query=req.query,
        equipment_id=req.equipment_id,
        period_start=req.period_start,
        period_end=req.period_end,
        top_k=req.top_k,
    )
    return HistoryQueryResponse(
        judgement_id=result.judgement_id,
        conclusion=result.conclusion,
        confidence=result.confidence,
        recommended_action=result.recommended_action,
        evidence_chunk_ids=result.evidence_chunk_ids,
    )
