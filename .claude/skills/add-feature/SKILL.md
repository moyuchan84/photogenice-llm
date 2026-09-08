---
name: add-feature
description: 새 spec_check 기능을 Feature Registry에 스캐폴딩한다. /add-feature <기능이름> 형태로 직접 호출.
disable-model-invocation: true
argument-hint: [feature-name]
---

$ARGUMENTS 기능을 다음 순서로 스캐폴딩한다:

1. `app/rag/evaluators/$ARGUMENTS.py` 생성 — 결정론적 `evaluate()` 함수 스켈레톤
   (spec_evaluator.py의 Evaluator 인터페이스를 따름, LLM/DB/API 호출 절대 금지)
2. `app/rag/features.py`의 FEATURE_REGISTRY에 `$ARGUMENTS` 항목 추가
   (kind: "spec_check", evaluator, prompt_context 자리를 TODO로 채워둠)
3. `app/api/routes_$ARGUMENTS.py` 생성 — SpecCheckRequest를 받아
   resolve_data_and_spec() → run_spec_check()를 호출하는 얇은 라우터
4. `tests/test_$ARGUMENTS.py` 생성 — 경계값(LSL/USL 정확히 일치, 살짝 벗어남,
   LSL == USL 스펙 폭 0) 테스트 스켈레톤 포함
5. 생성한 파일 목록과 다음에 사람이 채워야 할 TODO(구체적인 계산식, spec 필드명 등)를
   요약해서 보고한다.

기존 focal_curve/final_xy 구현이 있으면 그 구조를 그대로 따라간다. 없으면
01-architecture.md의 FocalCurveEvaluator/FinalXYEvaluator 예시를 참고한다.

새 기능이 root_cause 패턴(ID Dump처럼 evaluator 없이 항상 RAG만 쓰는 경우)이면
1번과 2번 대신 `app/rag/$ARGUMENTS.py`에 `run_$ARGUMENTS_analysis()` 함수를 만들고
FEATURE_REGISTRY에 kind: "root_cause"로 등록한다.
