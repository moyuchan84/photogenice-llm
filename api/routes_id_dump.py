"""ID Dump — root_cause 패턴(FR-4). 항상 rag/id_dump.py::run_id_dump_analysis()를
호출한다(evaluator 없음)."""

from fastapi import APIRouter

from api.deps import DbPool, EmbeddingClientDep, LLMClientDep
from models.schemas import IdDumpRequest, IdDumpResponse
from rag.id_dump import run_id_dump_analysis

router = APIRouter(prefix="/query", tags=["id-dump"])


@router.post("/id-dump", response_model=IdDumpResponse)
async def query_id_dump(
    req: IdDumpRequest,
    pool: DbPool,
    embedding_client: EmbeddingClientDep,
    llm_client: LLMClientDep,
) -> IdDumpResponse:
    result = await run_id_dump_analysis(
        pool,
        embedding_client,
        llm_client,
        equipment_id=req.equipment_id,
        error_dump_text=req.error_dump_text,
        top_k=req.top_k,
    )
    return IdDumpResponse(
        judgement_id=result.judgement_id,
        conclusion=result.conclusion,
        confidence=result.confidence,
        recommended_action=result.recommended_action,
        evidence_chunk_ids=result.evidence_chunk_ids,
    )
