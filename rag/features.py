"""Feature Registry — Focal Curve/Final XY(spec_check 패턴)와 ID Dump(root_cause 패턴,
prompt_context만 여기서 참조)를 등록한다. 새 기능은 이 레지스트리에 항목을 추가하는
방식으로만 확장하고, run_rag_judgement()/run_spec_check() 본체는 수정하지 않는다
(FR-5.1, CLAUDE.md).

run_spec_check()는 DB 저장 + 조건부 RAG 호출이 필요해 순수성 훅이 막는
rag/spec_evaluator.py에 둘 수 없으므로 이 파일이 대신 소유한다(Phase 3 완료 노트
참고 — 의도적으로 여기로 위임됨).
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg
from fastapi import HTTPException

from clients.asml_api_client import AsmlApiClient
from clients.embedding_client import EmbeddingClient
from clients.llm_client import LLMClient
from config import Settings
from db.repo import save_spec_evaluation
from models.schemas import SpecCheckRequest
from rag.core import run_rag_judgement
from rag.evaluators.final_xy import FinalXYEvaluator
from rag.evaluators.focal_curve import FocalCurveEvaluator
from rag.spec_evaluator import GenericSpecEvaluator, SpecEvaluator

FOCAL_CURVE_DOMAIN_NOTES = (
    "Focal curve는 필드 중심에서의 초점 편차가 가장자리보다 공정에 미치는 영향이 크다. "
    "range가 클수록 초점 심도(DOF) 여유가 줄어드는 것으로 해석한다."
)
FINAL_XY_DOMAIN_NOTES = (
    "Final XY(overlay) 오차는 X/Y 각 축의 mean±3σ 범위로 평가하며, 두 축 중 스펙 여유가 "
    "더 작은 쪽이 전체 판정을 좌우한다."
)
ID_DUMP_DOMAIN_NOTES = (
    "장비 에러 덤프는 detector/스캔 메타 정보를 포함한다. 과거 유사 에러 코드·메시지 "
    "패턴이 있으면 그 원인 사례를 우선적으로 참고해 원인을 추정한다."
)


@dataclass(frozen=True)
class FeatureConfig:
    kind: str  # "spec_check" | "root_cause"
    evaluator: SpecEvaluator | None
    prompt_context: str | None


FEATURE_REGISTRY: dict[str, FeatureConfig] = {
    "focal_curve": FeatureConfig(
        kind="spec_check", evaluator=FocalCurveEvaluator(), prompt_context=FOCAL_CURVE_DOMAIN_NOTES
    ),
    "final_xy": FeatureConfig(
        kind="spec_check", evaluator=FinalXYEvaluator(), prompt_context=FINAL_XY_DOMAIN_NOTES
    ),
    "id_dump": FeatureConfig(
        kind="root_cause", evaluator=None, prompt_context=ID_DUMP_DOMAIN_NOTES
    ),
}


def _get_feature_config(feature_type: str) -> FeatureConfig:
    if feature_type == "generic":
        return FeatureConfig(
            kind="spec_check", evaluator=GenericSpecEvaluator(), prompt_context=None
        )
    return FEATURE_REGISTRY[feature_type]


async def resolve_data_and_spec(
    req: SpecCheckRequest, *, feature_type: str, asml_client: AsmlApiClient | None
) -> tuple[dict, dict]:
    """프론트가 이미 렌더링한 데이터(inline_data/inline_spec)가 있으면 그대로 쓰고(이
    경로는 ASML API를 전혀 호출하지 않는다 — asml_client가 None이어도 동작해야 함),
    없으면 identifier(equipment_id/parameter) 기준으로 ASML API에서 조회한다
    (FR-2.5). asml_client는 실제로 호출이 필요한 시점에만 None 여부를 확인한다 —
    api/deps.py::get_asml_api_client_dep()가 더 이상 즉시 503을 던지지 않기 때문."""
    if req.inline_data is not None and req.inline_spec is not None:
        return req.inline_data, req.inline_spec
    if asml_client is None:
        raise HTTPException(
            status_code=503,
            detail="ASML API가 설정되지 않았습니다 (ASML_API_BASE/ASML_API_KEY 확인 필요).",
        )
    if feature_type == "generic":
        return await asml_client.fetch_data_and_spec(
            equipment_id=req.equipment_id, parameter=req.parameter
        )
    return await asml_client.fetch_feature_data_and_spec(
        feature_type=feature_type, equipment_id=req.equipment_id, parameter=req.parameter
    )


_FEATURE_LABELS = {
    "generic": "설비 판정",
    "focal_curve": "Focal Curve",
    "final_xy": "Final XY",
}


def _build_explanation_query(
    feature_type: str,
    equipment_id: str,
    parameter: str,
    data: dict,
    spec: dict,
    determination: str,
    margin_pct: float | None,
) -> str:
    label = _FEATURE_LABELS.get(feature_type, feature_type)
    return (
        f"[{label}] {equipment_id}의 {parameter} 측정값이 spec을 벗어났거나 근접했습니다 "
        f"(판정={determination}, margin_pct={margin_pct}%, data={data}, spec={spec}). "
        "과거 유사 사례를 참고해 원인과 권고 조치를 설명해 주세요."
    )


def _coerce_measured_at(value: object) -> datetime | None:
    """ASML API/inline_data의 measured_at은 JSON을 거치며 ISO 문자열로 들어온다.
    asyncpg는 timestamptz 파라미터에 str을 받으면 DataError를 내므로(= 판정은 됐는데
    감사 레코드가 유실됨) 저장 직전에 datetime으로 정규화한다. 시각 필드는 KST(+09:00)
    aware로 내려오므로(ftpmodule SPEC §3.4) tz 정보는 그대로 보존한다."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class SpecCheckOutcome:
    eval_id: int
    determination: str
    margin_pct: float | None
    judgement_id: int | None
    conclusion: str | None
    confidence: float | None
    recommended_action: str | None
    evidence_chunk_ids: list[int] | None


async def run_spec_check(
    pool: asyncpg.Pool,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
    settings: Settings,
    *,
    feature_type: str,
    equipment_id: str,
    parameter: str,
    data: dict,
    spec: dict,
    top_k: int = 5,
) -> SpecCheckOutcome:
    """평가(evaluator) → spec_evaluations 저장 → 조건부 RAG 설명(FR-2.2~2.4)까지의 공용
    흐름. feature_type만 바꾸면 generic/focal_curve/final_xy 모두 이 함수를 그대로
    재사용한다(FR-5.1)."""
    config = _get_feature_config(feature_type)
    result = config.evaluator.evaluate(data, spec)

    # data/spec이 evaluate_spec()의 평탄 구조({"value"}/{"lsl","usl"})를 따르지 않는
    # evaluator(focal_curve/final_xy)는 SpecResult.measured_value/lsl/usl에 감사용
    # 대표값을 채워 넣는다 — 없으면(generic) data/spec에서 직접 찾은 값으로 대체한다.
    measured_value = (
        result.measured_value if result.measured_value is not None else data.get("value")
    )
    lsl = result.lsl if result.lsl is not None else spec.get("lsl")
    usl = result.usl if result.usl is not None else spec.get("usl")

    eval_id = await save_spec_evaluation(
        pool,
        equipment_id=equipment_id,
        parameter=parameter,
        measured_value=measured_value,
        unit=data.get("unit") or spec.get("unit"),
        lsl=lsl,
        usl=usl,
        target=spec.get("target"),
        measured_at=_coerce_measured_at(data.get("measured_at")),
        determination=result.determination,
        margin_pct=result.margin_pct,
        raw_data=data,
        raw_spec=spec,
        feature_type=feature_type,
        metrics=result.metrics,
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
                feature_type,
                equipment_id,
                parameter,
                data,
                spec,
                result.determination,
                result.margin_pct,
            ),
            feature_type=feature_type if feature_type != "generic" else "log_general",
            equipment_id=equipment_id,
            prompt_context=config.prompt_context,
            eval_id=eval_id,
            top_k=top_k,
        )
        judgement_id = rag_result.judgement_id
        conclusion = rag_result.conclusion
        confidence = rag_result.confidence
        recommended_action = rag_result.recommended_action
        evidence_chunk_ids = rag_result.evidence_chunk_ids

    return SpecCheckOutcome(
        eval_id=eval_id,
        determination=result.determination,
        margin_pct=result.margin_pct,
        judgement_id=judgement_id,
        conclusion=conclusion,
        confidence=confidence,
        recommended_action=recommended_action,
        evidence_chunk_ids=evidence_chunk_ids,
    )
