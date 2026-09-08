---
name: spec-check-conventions
description: 이 프로젝트의 spec 판정 아키텍처 규칙 — 판정과 설명의 분리, feature_type 스키마, evaluator 작성 규칙, Feature Registry 패턴. spec 판정, evaluator, RAG 코어 관련 작업 시 항상 참고.
---

## 판정과 설명의 분리 (가장 중요한 원칙)

> 경계값 테스트에는 LSL/USL과 정확히 같은 값뿐 아니라 **LSL == USL(스펙 폭 0)** 케이스도
> 반드시 포함한다 — margin_pct 계산에서 half_range가 0이 되어 0으로 나누는 버그가
> 나기 쉬운 지점이다 (01-architecture.md의 evaluate_spec 예시 참고).
- spec in/out 같은 숫자 비교는 절대 LLM에게 맡기지 않는다. evaluators/*.py의 순수 함수로만 계산한다.
- LLM(Gemma4-260430)은 OUT_OF_SPEC이거나 margin_pct가 임계치(기본 10%) 이하일 때만,
  이미 계산된 결과를 설명하는 용도로만 호출한다.
- evaluator 파일에는 llm_client, embedding_client, asml_api_client를 임포트하지 않는다
  (hook이 자동으로 검사해서 위반 시 차단한다).

## 데이터 소스 유연성
- Focal Curve/Final XY 요청은 inline_data/inline_spec(프론트가 이미 가진 값)이 있으면
  그것을 우선 쓰고, 없으면 DB/API에서 identifier 기준으로 조회한다.

## 스키마 규칙
- spec_evaluations: feature_type, determination, margin_pct, metrics_json(계산된 통계),
  raw_data_json/raw_spec_json(원본 보존)
- log_chunks/judgements에도 feature_type을 반드시 채운다 (검색 스코프 분리용 —
  예: id_dump 검색이 focal_curve 로그와 섞이지 않도록)

## Feature Registry 패턴
- 새 기능은 FEATURE_REGISTRY에 evaluator + prompt_context를 등록하는 방식으로 추가한다.
- run_rag_judgement()/run_spec_check() 본체는 새 기능 추가 시 수정하지 않는다.
- kind: "spec_check"는 evaluator가 있고 조건부로만 RAG를 호출하는 기능(Focal Curve, Final XY).
- kind: "root_cause"는 evaluator 없이 항상 RAG를 호출하는 기능(ID Dump, History).

## 감사 추적
- 모든 판정/설명 결과는 근거(evidence_chunk_ids 등)와 함께 spec_evaluations 또는
  judgements 테이블에 저장한다. 저장을 생략하지 않는다.
