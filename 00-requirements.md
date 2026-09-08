# 00-requirements.md — ASML 설비 로그 RAG 시스템 요구사항 정의서 (v1)

용도: **개발팀 내부용.** 이 문서는 "무엇을 만드는가(요구사항)"를 정의한다. "어떻게 만드는가"는 `01-architecture.md`, "언제/어떤 순서로 만드는가"는 `02-roadmap.md`, "클로드 코드로 세션 단위로 어떻게 실행하는가"는 `CLAUDE.md`를 참고한다.

```
00-requirements.md (무엇을)  →  01-architecture.md (어떻게)  →  02-roadmap.md (언제/순서)  →  CLAUDE.md (세션 단위 실행)
```

---

## 1. 목적과 배경

ASML 설비에서 발생하는 로그/측정 데이터를 기반으로, 엔지니어가 설비 이상 여부를 빠르게 판정하고 과거 유사 사례를 참고해 원인을 파악할 수 있도록 지원하는 RAG(검색증강생성) 기반 판정 시스템을 구축한다.

핵심 문제의식: 스펙 in/out처럼 정답이 명확한 판정을 LLM에게 맡기면 환각으로 인한 오판정 위험이 있다. 따라서 **"판정(숫자 비교)"은 결정론적 코드로, "설명(원인/결론)"은 LLM/RAG로** 책임을 분리하는 것이 이 시스템 전체를 관통하는 핵심 요구사항이다.

## 2. 범위

### 2.1 포함 범위 (In Scope)

- UC1: 부서 ETL이 PostgreSQL에 적재하는 로그 데이터 기반 판정/결론 (history)
- UC2: 부서 ASML API(FastAPI)를 호출해 받은 data+spec 기반 spec in/out 판정 및 원인 설명 (spec-check)
- 확장 기능 3종: Focal Curve, Final XY(모두 UC2/spec_check 패턴), ID Dump(UC1/root_cause 패턴)
- 기존 RDB 데이터의 임베딩 백필(1회성) 및 이후 주기적 동기화(polling)
- 모든 판정/설명 결과의 감사 추적(근거와 함께 DB 저장)
- 클로드 코드 개발 하네스(서브에이전트, hooks, skills) — 개발 프로세스 자체도 요구사항의 일부로 관리

### 2.2 제외 범위 (Out of Scope, 현재 단계)

| 항목 | 제외 사유 | 재검토 조건 |
|---|---|---|
| MCP 기반 `/chat` 탐색형 에이전트 인터페이스 | 핵심 판정 파이프라인 안정화가 우선 | UC1~UC2 및 확장 3기능이 안정화된 이후 (Session 20) |
| 실시간 이벤트 스트리밍(Kafka, DB CDC 등) | 부서 ETL 자체가 이미 배치/주기 방식이라 과설계 | 로그 적재 주기가 실시간에 가깝게 바뀌거나 polling 지연이 SLA를 못 맞출 때 |
| 다중 부서/다중 설비 유형으로의 일반화 | 1차 목표는 ASML 리소그래피 설비 로그 | UC1/UC2가 검증된 이후 다른 설비군으로 확장 논의 |
| 워커 다중 인스턴스 수평 확장(`SKIP LOCKED`) | 현재 로그량 기준 단일 워커로 충분 | 폴링 주기/LIMIT 조정으로도 처리량이 부족해질 때 |

## 3. 이해관계자

- **개발팀**: 본 시스템 구현 담당 (클로드 코드 활용)
- **사내 AI 플랫폼/포토리소그래피 공정 부서**: ASML API·로그 데이터 제공자이자, 판정 결과의 최종 소비자(엔지니어)
- **사내 LLM/임베딩 API 운영팀**: Gemma4-260430, BGE-M3 API 제공

## 4. 용어 정의

| 용어 | 정의 |
|---|---|
| UC1 (history) | DB에 적재된 로그를 하이브리드 검색해 판정/결론을 내는 시나리오 |
| UC2 (spec-check) | ASML API로 받은 data+spec을 결정론적으로 비교해 in/out을 판정하는 시나리오 |
| IN_SPEC / OUT_OF_SPEC | spec 판정 결과값 |
| margin_pct | 측정값이 스펙 한계까지 남은 여유(%). 음수면 스펙 초과 |
| feature_type | focal_curve / final_xy / id_dump / generic 등 기능 구분자. 검색 스코프 분리에 사용 |
| Feature Registry | 기능별 evaluator + prompt_context를 등록하는 확장 패턴 (RAG 코어 본체는 수정하지 않음) |
| judgement | UC1/UC2 공통으로 LLM이 생성한 원인 해석/결론/권고 조치 이력 |

## 5. 기능 요구사항 (Functional Requirements)

### FR-1. UC1 — History 기반 판정 (DB Polling)

- FR-1.1 Embedding Sync Worker는 **polling 방식**(APScheduler cron)으로 동작해야 하며, `embedded_at IS NULL` 커서 기준으로 미임베딩 row를 처리한다.
- FR-1.2 워커가 중간에 중단되어도 재시작 시 자동으로 이어서 처리되는 **멱등성**을 보장해야 한다 (중복 임베딩 방지).
- FR-1.3 `/query/history` 엔드포인트는 자연어 질의 → 질의 임베딩 → `log_chunks` 하이브리드 검색(메타데이터 필터 + 벡터 유사도) → LLM 판정/결론 생성 흐름을 지원해야 한다.
- FR-1.4 기존 RDB 적재 데이터에 대한 1회성 백필 스크립트를 제공해야 한다.

### FR-2. UC2 — Spec-check (API Pull 기반)

- FR-2.1 ASML API 클라이언트는 부서 API를 호출(pull)하여 `data`(실측값) + `spec`(스펙 한계)이 결합된 JSON을 수신해야 하며, **타임아웃과 재시도**를 갖춰야 한다.
- FR-2.2 spec in/out 판정(`determination`)은 **LLM을 거치지 않는 결정론적 순수 함수**로만 계산해야 한다.
- FR-2.3 RAG(LLM) 호출은 `determination == OUT_OF_SPEC` 이거나 `margin_pct`가 임계치(기본 10%) 이하로 스펙에 근접할 때만 조건부로 실행해야 한다 (비용 절감).
- FR-2.4 판정 결과는 항상 `spec_evaluations`에 저장하고, RAG 설명이 생성된 경우 `judgements`에도 근거와 함께 저장해야 한다.
- FR-2.5 UC2는 프론트가 이미 렌더링한 데이터(`inline_data`/`inline_spec`)를 그대로 받는 경로와, identifier만으로 DB/API에서 직접 조회하는 경로를 모두 지원해야 한다.

### FR-3. Focal Curve / Final XY (spec_check 패턴 확장)

- FR-3.1 각 기능은 전용 Evaluator(`FocalCurveEvaluator`, `FinalXYEvaluator`)가 curve/배열 데이터를 결정론적으로 계산(range/sigma/DOF, mean±3σ 등)해야 한다.
- FR-3.2 계산된 요약 통계는 `spec_evaluations.metrics_json`에, 원본 배열은 `raw_data_json`에 저장해야 한다.
- FR-3.3 UC2와 동일하게 OUT_OF_SPEC이거나 margin이 임계치 이하일 때만 RAG를 호출해야 한다.

### FR-4. ID Dump (root_cause 패턴)

- FR-4.1 평가자(evaluator) 없이 **항상 RAG를 호출**해 과거 유사 원인 사례를 검색하고 원인을 추정해야 한다.
- FR-4.2 검색 시 `feature_type = 'id_dump'` 필터를 반드시 적용해 다른 기능의 로그와 섞이지 않아야 한다.
- FR-4.3 초기 정확도 확보를 위해 엔지니어가 직접 원인을 기록한 과거 사례를 `judgements`에 시드 데이터로 채우는 절차가 필요하다.

### FR-5. Feature Registry 확장성

- FR-5.1 새 기능은 `FEATURE_REGISTRY`에 evaluator + prompt_context를 등록하는 방식으로만 추가해야 하며, `run_rag_judgement()`/`run_spec_check()` 본체는 새 기능 추가 시 수정하지 않아야 한다.

### FR-6. 감사 추적 (Audit Trail)

- FR-6.1 모든 판정/설명 결과(UC1: `judgements`, UC2: `spec_evaluations` + OOS 시 `judgements`)는 **근거와 함께** DB에 저장해야 하며, 이 저장을 생략해서는 안 된다.

### FR-7. (후순위/조건부) MCP 기반 탐색 인터페이스

- FR-7.1 `/chat` 엔드포인트는 핵심 판정 엔드포인트(`/query/*`)와 별도 라우터/서비스로 분리해야 한다.
- FR-7.2 MCP 툴은 새로운 계산을 하지 않고, 이미 계산·저장된 결과를 조회하거나 검색 함수를 wrapping하는 역할만 해야 한다 (판정 재계산 금지).
- FR-7.3 착수 전 사내 Gemma4-260430 API의 tool-calling 지원 여부를 확인해야 한다.

## 6. 비기능 요구사항 (Non-Functional Requirements)

| ID | 요구사항 | 비고 |
|---|---|---|
| NFR-1 신뢰성 | spec 판정 로직은 100% 결정론적이어야 하며, 경계값(LSL/USL 일치, LSL==USL 스펙 폭 0 포함)을 포함한 유닛테스트로 검증한다 | `spec-evaluator-tester` 에이전트, purity hook으로 강제 |
| NFR-2 비용 효율 | IN_SPEC이고 여유가 충분한 경우 LLM을 호출하지 않아 불필요한 비용을 발생시키지 않는다 | 임계치는 Phase 5에서 튜닝 (현재 TBD, 기본값 10%) |
| NFR-3 장애 격리 | ASML API/사내 LLM·임베딩 API 장애가 서비스 전체를 블로킹하지 않아야 한다 | `httpx.AsyncClient` + `tenacity` 재시도/타임아웃 |
| NFR-4 보안 | 사내 API 키를 코드에 하드코딩하지 않고 `.env`/환경변수로만 관리한다 | `protect-secrets.sh` hook으로 강제 |
| NFR-5 확장성 | 로그량 증가 시 폴링 주기/LIMIT 조정 → 필요 시 `SELECT ... FOR UPDATE SKIP LOCKED`로 워커 수평 확장 가능해야 한다 | 1차는 단일 워커로 시작 (2.2 제외범위 참고) |
| NFR-6 추적성 | 모든 판정/설명 결과는 재현 가능하도록 근거(evidence chunk 등)와 함께 저장한다 | FR-6과 연결 |
| NFR-7 유지보수성 | RAG 코어(`run_rag_judgement`)는 단일화하고 UC1/UC2/확장 기능별로 로직을 복사하지 않는다 | FR-5, `code-reviewer` 에이전트 체크리스트 |

## 7. 데이터 요구사항

### 7.1 핵심 테이블 (요약)

| 테이블 | 용도 | 핵심 컬럼 |
|---|---|---|
| `logs_raw` | 부서 ETL이 적재하는 원본 로그 | `embedded_at`(polling 커서), `event_time` |
| `log_chunks` | 임베딩된 로그 청크 (벡터 검색 대상) | `feature_type`, 벡터 컬럼(HNSW, cosine) |
| `spec_evaluations` | UC2 계열(spec_check) 판정 이력 | `feature_type`, `determination`, `margin_pct`, `metrics_json`, `raw_data_json`, `raw_spec_json` |
| `judgements` | UC1/UC2 공통 LLM 설명·결론 이력 | `feature_type`, 근거(evidence) |

### 7.2 UC2 입력 데이터 스키마

`data`(실측값) + `spec`(스펙 한계)가 결합된 JSON — 실제 필드명은 부서 스키마 확정 필요 (§11 참고). 핵심은 measured value와 spec limit(lsl/usl)이 항상 짝을 이뤄 온다는 것.

## 8. 외부 인터페이스 요구사항

| 인터페이스 | 제공 주체 | 상태 |
|---|---|---|
| ASML API (FastAPI) | 부서 (기존 운영 중) | 엔드포인트/인증/응답 스키마 **확정 필요** |
| 사내 LLM API (Gemma4-260430) | 사내 플랫폼팀 | 스펙 확인 필요 (특히 tool-calling 지원 여부, FR-7.3) |
| 사내 Embedding API (BGE-M3, 1024차원) | 사내 플랫폼팀 | 스펙 확인 필요 |
| PostgreSQL + pgvector | 기존 인프라 (세팅 완료) | 사용 가능 |

## 9. 제약사항 (Constraints)

- 기술 스택 고정: Python 3.11+ / FastAPI+Uvicorn / asyncpg(동기 ORM 금지) / pgvector(HNSW, cosine) / APScheduler→Celery+Redis(확장 시) / Pydantic v2
- "판정과 설명의 분리" 원칙은 아키텍처 전역 제약이며, 어떤 이유로도 완화하지 않는다 (`CLAUDE.md` 하지 말아야 할 것 참고)
- 신규 기능은 Feature Registry 등록 방식만 허용 — 핵심 함수 본체 분기 금지

## 10. 인수 기준 (Acceptance Criteria)

| UC/기능 | Given | When | Then |
|---|---|---|---|
| UC1 | `logs_raw`에 `embedded_at IS NULL` row 존재 | Embedding Sync Worker 실행 | 해당 row가 청킹·임베딩되어 `log_chunks`에 저장되고 `embedded_at`이 갱신됨. 워커를 중간에 강제 종료 후 재실행해도 중복 임베딩이 생기지 않음 |
| UC1 | 사용자가 자연어로 질의 | `/query/history` 호출 | 메타데이터 필터+벡터 검색 결과를 근거로 한 판정/결론이 `judgements`에 저장되어 반환됨 |
| UC2 | ASML API가 data+spec 응답 | `/query/spec-check` 호출, 값이 LSL/USL 이내 | LLM 호출 없이 `{determination: IN_SPEC, margin_pct}`만 즉시 반환, `spec_evaluations`에 저장 |
| UC2 | 값이 LSL/USL 초과 또는 margin_pct ≤ 임계치 | `/query/spec-check` 호출 | 결정론적 판정 결과 + RAG 기반 원인가설/권고조치가 함께 반환되고 `judgements`에도 저장됨 |
| Focal Curve/Final XY | curve/배열 데이터 입력 | 해당 evaluator 호출 | 결정론적으로 계산된 통계(`metrics_json`)와 판정 결과가 저장됨 |
| ID Dump | 에러 로그 파싱 텍스트 입력 | `run_id_dump_analysis()` 호출 | `feature_type='id_dump'` 필터로 검색된 과거 유사 사례 기반 원인 추정이 근거와 함께 반환됨 |

## 11. 미해결 이슈 (Open Questions / TBD)

- `logs_raw`의 실제 컬럼 스키마 확정 필요 (Phase 0 선행 조건)
- ASML API의 실제 엔드포인트/인증 방식/응답 스키마(필드명) 확정 필요
- 사내 Gemma4-260430 API의 tool-calling(`tools`/`function_call`) 지원 여부 — FR-7.3, Session 20 선행조건
- `margin_pct` 임계치(기본 10%), 하이브리드 검색 `top_k`, 청킹 윈도우 크기 등 파라미터는 Phase 5 평가에서 실측 기반으로 확정
- Focal Curve/Final XY의 정확한 계산식(현재 range/sigma/DOF, mean±3σ는 초안) — 부서 엔지니어 확인 필요

## 12. 관련 문서

- `01-architecture.md` — 시스템 아키텍처 설계
- `02-roadmap.md` — Phase/마일스톤 단위 로드맵
- `CLAUDE.md` — 클로드 코드 세션 단위 작업 목록 및 코딩 컨벤션
- `claude-code-harness-guide.md` — 서브에이전트/hooks/skills 개발 하네스 가이드
