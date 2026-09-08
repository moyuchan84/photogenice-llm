"""UC2: POST /query/spec-check — ASML API pull → 결정론적 spec 판정 → 조건부 RAG 설명
(FR-2.1~2.4). spec in/out 판정은 rag/spec_evaluator.py의 순수 함수로만 계산하고,
LLM은 OUT_OF_SPEC이거나 margin_pct가 임계치 이하일 때만 호출한다 — 이 조건 로직을
임의로 제거하지 않는다.
"""

from fastapi import APIRouter

from api.deps import AsmlApiClientDep, DbPool, EmbeddingClientDep, LLMClientDep, SettingsDep
from db.repo import save_spec_evaluation
from models.schemas import SpecCheckRequest, SpecCheckResponse
from rag.core import run_rag_judgement
from rag.spec_evaluator import evaluate_spec

router = APIRouter(prefix="/query", tags=["spec-check"])


def _build_explanation_query(
    equipment_id: str, parameter: str, data: dict, spec: dict, determination: str, margin_pct
) -> str:
    return (
        f"{equipment_id}의 {parameter} 측정값이 spec을 벗어났거나 근접했습니다 "
        f"(measured={data.get('value')}, lsl={spec.get('lsl')}, usl={spec.get('usl')}, "
        f"determination={determination}, margin_pct={margin_pct}%). "
        "과거 유사 사례를 참고해 원인과 권고 조치를 설명해 주세요."
    )


@router.post("/spec-check", response_model=SpecCheckResponse)
async def query_spec_check(
    req: SpecCheckRequest,
    pool: DbPool,
    embedding_client: EmbeddingClientDep,
    llm_client: LLMClientDep,
    asml_client: AsmlApiClientDep,
    settings: SettingsDep,
) -> SpecCheckResponse:
    data, spec = await asml_client.fetch_data_and_spec(
        equipment_id=req.equipment_id, parameter=req.parameter
    )
    result = evaluate_spec(data, spec)

    eval_id = await save_spec_evaluation(
        pool,
        equipment_id=req.equipment_id,
        parameter=req.parameter,
        measured_value=data.get("value"),
        unit=data.get("unit") or spec.get("unit"),
        lsl=spec.get("lsl"),
        usl=spec.get("usl"),
        target=spec.get("target"),
        measured_at=data.get("measured_at"),
        determination=result.determination,
        margin_pct=result.margin_pct,
        raw_data=data,
        raw_spec=spec,
    )

    judgement_id = conclusion = confidence = recommended_action = evidence_chunk_ids = None
    if (
        result.determination == "OUT_OF_SPEC"
        or result.margin_pct <= settings.spec_check_margin_threshold_pct
    ):
        rag_result = await run_rag_judgement(
            pool,
            embedding_client,
            llm_client,
            use_case="spec_check",
            query=_build_explanation_query(
                req.equipment_id, req.parameter, data, spec, result.determination, result.margin_pct
            ),
            equipment_id=req.equipment_id,
            eval_id=eval_id,
            top_k=req.top_k,
        )
        judgement_id = rag_result.judgement_id
        conclusion = rag_result.conclusion
        confidence = rag_result.confidence
        recommended_action = rag_result.recommended_action
        evidence_chunk_ids = rag_result.evidence_chunk_ids

    return SpecCheckResponse(
        eval_id=eval_id,
        determination=result.determination,
        margin_pct=result.margin_pct,
        judgement_id=judgement_id,
        conclusion=conclusion,
        confidence=confidence,
        recommended_action=recommended_action,
        evidence_chunk_ids=evidence_chunk_ids,
    )
