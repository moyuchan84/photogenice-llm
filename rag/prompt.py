"""RAG 판정 프롬프트 템플릿 — JSON 강제 출력 스키마를 시스템 프롬프트에 명시한다.

기능별 특화(Focal Curve/Final XY/ID Dump)는 이 파일의 본체를 수정하지 않고
FEATURE_REGISTRY의 prompt_context를 build_system_prompt()의 domain_notes 인자로
전달하는 방식으로만 이루어진다 (01-architecture.md §7.2 Feature Registry 패턴,
Session 14에서 registry가 생기면 그쪽에서 이 인자로 연결한다).
"""

from rag.retriever import RetrievedChunk

_BASE_SYSTEM_PROMPT = """당신은 ASML 리소그래피 설비 로그를 분석하는 엔지니어링 어시스턴트입니다.
아래 제공되는 과거 로그 근거(evidence)만을 근거로 판단하고, 근거에 없는 내용을 추측해서
단정하지 마세요. 근거가 부족하면 confidence를 낮게 설정하세요.

반드시 아래 JSON 스키마로만 응답하세요 (다른 텍스트나 설명을 덧붙이지 마세요):
{
  "conclusion": "<상황에 대한 결론을 한국어로 서술>",
  "confidence": <0.0~1.0 사이의 확신도>,
  "evidence_chunk_ids": [<판단에 사용한 chunk_id 정수 목록>],
  "recommended_action": "<권고 조치를 한국어로 서술>"
}"""


def build_system_prompt(domain_notes: str | None = None) -> str:
    if not domain_notes:
        return _BASE_SYSTEM_PROMPT
    return f"{_BASE_SYSTEM_PROMPT}\n\n[도메인 지식]\n{domain_notes}"


def _format_chunk(chunk: RetrievedChunk) -> str:
    error_part = f", 에러코드: {', '.join(chunk.error_codes)}" if chunk.error_codes else ""
    return (
        f"- chunk_id={chunk.chunk_id} (설비: {chunk.equipment_id}, "
        f"유사도 거리: {chunk.distance:.4f}{error_part})\n{chunk.chunk_text}"
    )


def build_user_prompt(query: str, chunks: list[RetrievedChunk]) -> str:
    evidence = (
        "\n\n".join(_format_chunk(c) for c in chunks) if chunks else "(검색된 과거 사례 없음)"
    )
    return f"질의: {query}\n\n[검색된 과거 로그 근거]\n{evidence}"
