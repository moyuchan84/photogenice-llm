"""Focal Curve — spec_check 패턴(FR-3). routes_spec_check.py와 동일하게
rag/features.py의 공용 흐름을 feature_type="focal_curve"로 재사용하는 얇은
라우터다.
"""

from fastapi import APIRouter

from api.deps import AsmlApiClientDep, DbPool, EmbeddingClientDep, LLMClientDep, SettingsDep
from models.schemas import SpecCheckRequest, SpecCheckResponse
from rag.features import resolve_data_and_spec, run_spec_check

router = APIRouter(prefix="/query", tags=["focal-curve"])


@router.post("/focal-curve", response_model=SpecCheckResponse)
async def query_focal_curve(
    req: SpecCheckRequest,
    pool: DbPool,
    embedding_client: EmbeddingClientDep,
    llm_client: LLMClientDep,
    asml_client: AsmlApiClientDep,
    settings: SettingsDep,
) -> SpecCheckResponse:
    data, spec = await resolve_data_and_spec(
        req, feature_type="focal_curve", asml_client=asml_client
    )
    outcome = await run_spec_check(
        pool,
        embedding_client,
        llm_client,
        settings,
        feature_type="focal_curve",
        equipment_id=req.equipment_id,
        parameter=req.parameter,
        data=data,
        spec=spec,
        top_k=req.top_k,
    )
    return SpecCheckResponse(
        eval_id=outcome.eval_id,
        determination=outcome.determination,
        margin_pct=outcome.margin_pct,
        judgement_id=outcome.judgement_id,
        conclusion=outcome.conclusion,
        confidence=outcome.confidence,
        recommended_action=outcome.recommended_action,
        evidence_chunk_ids=outcome.evidence_chunk_ids,
    )
