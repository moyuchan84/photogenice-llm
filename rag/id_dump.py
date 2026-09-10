"""ID Dump — root_cause 패턴(UC1과 동일). Evaluator 없이 항상 run_rag_judgement()를
호출해 과거 유사 원인 사례를 검색한다(FR-4.1). rag/core.py 본체는 수정하지 않는다.
"""

import asyncpg

from clients.embedding_client import EmbeddingClient
from clients.llm_client import LLMClient
from rag.core import RagJudgementResult, run_rag_judgement
from rag.features import FEATURE_REGISTRY

_FEATURE_TYPE = "id_dump"


def _build_id_dump_query(error_dump_text: str, equipment_id: str) -> str:
    return (
        f"[{equipment_id}] 에러 덤프 분석 요청:\n{error_dump_text}\n\n"
        "과거 유사 사례를 참고해 원인과 권고 조치를 설명해 주세요."
    )


async def run_id_dump_analysis(
    pool: asyncpg.Pool,
    embedding_client: EmbeddingClient,
    llm_client: LLMClient,
    *,
    equipment_id: str,
    error_dump_text: str,
    top_k: int = 5,
) -> RagJudgementResult:
    return await run_rag_judgement(
        pool,
        embedding_client,
        llm_client,
        use_case="root_cause",
        query=_build_id_dump_query(error_dump_text, equipment_id),
        feature_type=_FEATURE_TYPE,
        equipment_id=equipment_id,
        prompt_context=FEATURE_REGISTRY[_FEATURE_TYPE].prompt_context,
        top_k=top_k,
    )
