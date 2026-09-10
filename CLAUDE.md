# CLAUDE.md — ASML 설비 로그 RAG 시스템 개발 가이드 (v2)

이 문서는 클로드 코드(Claude Code)로 이 프로젝트를 개발할 때 세션마다 참고할 프로젝트 컨텍스트입니다. 저장소 루트에 두고 사용하세요.

> 문서 지도: `00-requirements.md`(무엇을) → `01-architecture.md`(어떻게) → `02-roadmap.md`(언제/순서) → `CLAUDE.md`(이 문서, 세션 단위 실행) → `claude-code-harness-guide.md`(하네스: 서브에이전트/hooks/skills)

## 프로젝트 개요

ASML 설비 로그를 기반으로 한 RAG 판정 시스템. 두 가지 유즈케이스를 지원한다.

- **UC1 (history)**: 부서 ETL이 주기적으로 PostgreSQL에 적재하는 로그 데이터를 대상으로, 우리 시스템이 **DB를 polling**해서 미임베딩 데이터를 찾아 벡터화하고, 사용자 질의에 대해 하이브리드 검색 + LLM 판정을 제공.
- **UC2 (spec-check)**: 부서가 이미 운영 중인 **ASML API(FastAPI 백엔드)를 우리가 호출(pull)**해서 `data`(실측값) + `spec`(스펙 한계) 두 종류가 결합된 JSON을 받아, **결정론적 코드로 spec in/out을 판정**하고, OUT_OF_SPEC이거나 스펙에 근접한 경우에만 RAG로 과거 유사 사례를 찾아 LLM이 원인/권고를 설명.

두 유즈케이스는 동일한 RAG 코어(`run_rag_judgement()`)를 공유한다. **판정(숫자 비교)과 설명(원인/결론)의 책임을 절대 섞지 말 것** — spec in/out처럼 정답이 명확한 계산은 LLM에 맡기지 않고 `spec_evaluator.py`의 순수 함수로만 처리한다.

### 세부 기능: Focal Curve / Final XY / ID Dump

이 세 기능은 **별도 LLM이 아니라 하나의 RAG 코어를 공유하는 Feature Registry 패턴**으로 구현한다.

- `focal_curve`, `final_xy` → UC2(spec_check) 패턴. 각각 전용 Evaluator(`FocalCurveEvaluator`, `FinalXYEvaluator`)가 curve/배열 데이터를 결정론적으로 계산하고, OUT_OF_SPEC이거나 margin이 임계치 이하일 때만 RAG를 호출한다.
- `id_dump` → UC1(root_cause) 패턴. 평가자 없이 항상 RAG를 호출해 과거 유사 원인 사례를 검색하고 원인을 추정한다.
- 기능별 "특화"는 모델을 나누는 게 아니라 `FEATURE_REGISTRY`의 `prompt_context`(도메인 지식)와 검색 시 `feature_type` 필터로 구현한다. 새 기능이 추가되면 이 레지스트리에 항목만 추가하고, `run_rag_judgement()`/`run_spec_check()` 본체는 수정하지 않는 것이 원칙이다.

## 기존 백엔드 참고 자료 — `ftpmodule/` (git 비추적, 로컬 클론)

부서가 이미 운영 중인 ASML 로그 수집/파싱 백엔드(FTP fetch + 파싱 + 스케줄러 + 자체 FastAPI + PostgreSQL 스키마, 코드네임 `fleet`)를 저장소 루트에 `ftpmodule/`로 클론해 두었다. **별도 git 저장소이며 `.gitignore`에 등록되어 이 프로젝트의 커밋에는 포함되지 않는다** — 읽기 전용 참고 자료로만 쓰고 수정하지 않는다.

이 문서와 `00-requirements.md`/`01-architecture.md`에 "확정 필요(TBD)"로 남아 있는 ASML API 응답 스키마, spec 판정 기준 데이터, item(focal/overlay 등)별 파서 레코드 스키마는 **추측해서 설계하지 말고 먼저 `ftpmodule` 문서를 확인한다**:

| 확인하려는 것 | 참고 문서 |
|---|---|
| ASML API 엔드포인트/요청·응답 계약 (HTTP) | `ftpmodule/README.api.md` |
| 설계 불변식·전체 스펙 원문 | `ftpmodule/SPEC.md` |
| item(파서)별 레코드 스키마 — `dump`/`focal`/`overlay`/`sy`/`focalspec`/`overlayspec`/`tree` | `ftpmodule/fleet/processing/parse/<item>/interface.md` |
| DB 영속화 정책(테이블 목록·마이그레이션 규칙) | `ftpmodule/fleet/data/__spec__.md`, `ftpmodule/fleet/data/migrations/` |
| spec 판정 기준 데이터(기준정보) — `spec_criteria`/`spec_notes`, `/spec/map` | `ftpmodule/SPEC.md` §3.13, `ftpmodule/README.api.md` §14 |
| 손으로 돌리는 운영 스크립트(시계 동기화·초기화 등) | `ftpmodule/README.scripts.md` |

주의할 점:
- `ftpmodule`은 **`data`+`spec`이 한 응답에 결합돼 오지 않는다** — item 실행(`POST /servers/{id}/items/{item}`)이 실측값을, `/spec/map`·`/spec/criteria`가 판정 기준을 **별도 호출**로 준다. `01-architecture.md` §3.1의 `data`+`spec` 결합 JSON 예시는 초안 가정이므로, `clients/asml_api_client.py`/`rag/spec_evaluator.py` 구현·수정 시 실제로는 두 호출을 조합해야 할 수 있다는 점을 감안한다.
- `focal_curve`/`final_xy` 기능이 `ftpmodule`의 어느 item(`focal`/`overlay` 등)에 대응하는지는 아직 부서 확인 전이다 — Session 14(`FEATURE_REGISTRY` 골격) 착수 전에 확정할 것.
- 시각 필드는 전부 KST(+09:00) aware datetime으로 내려온다(SPEC §3.4) — 우리 쪽 `measured_at` 등 시각 파싱 시 그대로 신뢰 가능하다.

## 기술 스택 (고정)

- 언어: Python 3.11+
- 웹 프레임워크: FastAPI + Uvicorn
- DB: PostgreSQL + pgvector (HNSW 인덱스, cosine distance)
- DB 접근: asyncpg (동기 ORM 금지)
- 외부 API 호출: `httpx.AsyncClient` + `tenacity`(재시도/타임아웃) — ASML API, 사내 LLM/임베딩 API 모두 동일하게 적용
- 백그라운드 작업/스케줄링: APScheduler (초기), 처리량 부족 시 Celery + Redis로 확장
- 스키마 검증: Pydantic v2
- LLM: 사내 API의 `gemma4-260430`
- Embedding: 사내 API의 `bge-m3` (차원 1024)

## 디렉토리 구조 (권장, v2)

```
app/
  api/
    routes_history.py        # UC1: POST /query/history
    routes_spec_check.py     # UC2: POST /query/spec-check
  rag/
    core.py                  # run_rag_judgement() — UC1/UC2 공용
    retriever.py             # 하이브리드 검색 (pgvector + 메타데이터 필터)
    prompt.py                # 프롬프트 템플릿, JSON 출력 스키마
    spec_evaluator.py         # [신규] Evaluator 공통 인터페이스(Protocol) + run_spec_check()
    features.py                # [신규] FEATURE_REGISTRY — 기능별 evaluator/prompt_context 등록
    evaluators/
      focal_curve.py            # [신규] FocalCurveEvaluator — curve range/sigma/DOF 계산
      final_xy.py                # [신규] FinalXYEvaluator — X/Y mean±3σ 계산
    id_dump.py                   # [신규] run_id_dump_analysis() — 평가자 없이 RAG만 호출
  clients/
    llm_client.py             # Gemma4-260430 API 어댑터
    embedding_client.py       # BGE-M3 API 어댑터
    asml_api_client.py        # [신규] 부서 ASML API(FastAPI) 호출 어댑터
  db/
    schema.sql                 # DDL (logs_raw, log_chunks, spec_evaluations, judgements)
    repo.py                     # asyncpg 쿼리 모음
  workers/
    embedding_backfill.py       # 기존 RDB 데이터 임베딩 백필 (1회성)
    embedding_sync_poller.py    # [신규] UC1용 — 주기적 polling으로 미임베딩 row 처리
    chunker.py                  # 로그 청킹 로직 (세션/에러 윈도우 기반)
  models/
    schemas.py                  # Pydantic 모델
  config.py                     # 환경변수 로딩
tests/
scripts/
  backfill_embeddings.py
```

## 코딩 컨벤션

- 모든 DB/외부 API 호출은 `async def` + `await`로 작성한다.
- `spec_evaluator.py`의 판정 함수는 **외부 의존성(DB, API, LLM) 없는 순수 함수**로 유지하고, 유닛테스트로 경계값(LSL/USL 정확히 일치하는 값 포함)을 반드시 검증한다.
- ASML API, LLM API, Embedding API는 각각 인터페이스로 분리하고, RAG 코어/스펙 평가 로직에서 `httpx`를 직접 호출하지 않는다 (항상 `clients/` 하위 어댑터를 통해서만 호출).
- UC2에서 RAG(LLM) 호출은 **OUT_OF_SPEC이거나 margin_pct가 임계치 이하일 때만** 조건부로 실행한다 — 불필요한 LLM 호출 비용을 줄이기 위함이며, 이 조건 로직을 임의로 제거하지 않는다.
- `embedding_sync_poller.py`는 `embedded_at IS NULL` 커서 기반으로 동작해야 하며, 워커가 중간에 중단되어도 재시작 시 자동으로 이어서 처리되는 멱등성을 유지해야 한다 (재시작 시 중복 임베딩이 생기지 않도록 UPDATE 순서에 주의).
- 모든 판정 결과(UC1: `judgements`, UC2: `spec_evaluations` + OOS 시 `judgements`)는 근거와 함께 DB에 저장한다 (감사 추적 목적, 생략 금지).

## 환경변수 (.env 예시)

```
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
INTERNAL_LLM_API_BASE=https://internal-api.company.local/llm
INTERNAL_LLM_API_KEY=xxx
INTERNAL_LLM_MODEL=gemma4-260430
INTERNAL_EMBEDDING_API_BASE=https://internal-api.company.local/embedding
INTERNAL_EMBEDDING_API_KEY=xxx
INTERNAL_EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024

# 실제 엔드포인트/요청·응답 계약은 ftpmodule/README.api.md 참고 (§"기존 백엔드 참고 자료")
ASML_API_BASE=https://asml-backend.internal.company.local
ASML_API_KEY=xxx

EMBEDDING_SYNC_POLL_INTERVAL_SEC=300
SPEC_CHECK_MARGIN_THRESHOLD_PCT=10
```

## 개발 순서 (클로드 코드 세션 단위 작업 목록, v2)

- [x] **Session 0**: 개발 하네스 세팅 완료 — `.claude/settings.json`(hooks+permissions), `.claude/agents/*`(schema-migrator, db-reader, spec-evaluator-tester, rag-core-builder, code-reviewer), `.claude/skills/*`(spec-check-conventions, add-feature) 배포 및 hooks 실행권한 설정. 자세한 내용은 `claude-code-harness-guide.md` 참고.
- [x] **Session 1**: `db/schema.sql` 작성(`logs_raw` + `embedded_at` 컬럼, `log_chunks`, `spec_evaluations`, `judgements`) 및 마이그레이션, `config.py` 환경변수 로딩
- [x] **Session 2**: `clients/embedding_client.py` (BGE-M3) 구현 및 단건 테스트
- [x] **Session 3**: `workers/chunker.py` 구현 + 유닛테스트 (세션/에러 윈도우 청킹)
- [x] **Session 4**: `workers/embedding_sync_poller.py` — DB polling 방식 임베딩 동기화 워커, APScheduler 등록, 재시작 시 멱등성 테스트
- [x] **Session 5**: `scripts/backfill_embeddings.py` — 기존 데이터 1회성 백필 (Session 4 로직 재사용)
- [x] **Session 6**: `clients/llm_client.py` (Gemma4-260430) + JSON 강제 출력 파싱
- [x] **Session 7**: `rag/retriever.py`, `rag/prompt.py`, `rag/core.py` — `run_rag_judgement()` 구현
- [x] **Session 8**: `api/routes_history.py` — UC1 엔드포인트, 실제 과거 이슈로 수동 검증
- [x] **Session 9**: `rag/spec_evaluator.py` — 결정론적 spec in/out 판정 함수 + 경계값 유닛테스트
- [x] **Session 10**: `clients/asml_api_client.py` — ASML API 호출 어댑터 (타임아웃/재시도 포함)
- [x] **Session 11**: `api/routes_spec_check.py` — UC2 엔드포인트, 조건부 RAG 호출 로직(Session 7 재사용), `spec_evaluations` 저장 확인
- [x] **Session 12**: 평가 스크립트(`scripts/evaluate_golden_set.py`, `scripts/evaluate_chunk_window.py`) — 합성 골든셋(부서 실이력 TBD)으로 원인 가설 품질 측정, `margin_pct` 임계치 10%→15%/`top_k`=5/프롬프트 튜닝 확정. 상세: `00-requirements.md` §11.1, `02-roadmap.md` Phase 5
- [x] **Session 13**: `db/schema.sql`에 `feature_type`(spec_evaluations/log_chunks/judgements), `metrics_json`(spec_evaluations) 컬럼 추가 마이그레이션 — 실제로는 최초 커밋(Session 1)부터 이미 포함되어 있었음이 Phase 6 착수 시 확인됨(별도 작업 불필요)
- [x] **Session 14**: `rag/features.py` — `FEATURE_REGISTRY` 작성(focal_curve/final_xy/id_dump 항목 + prompt_context) + `resolve_data_and_spec()` + 공용 `run_spec_check()`(순수성 훅 때문에 spec_evaluator.py가 아닌 이 파일이 소유). ftpmodule 매핑 확인 결과: focal_curve↔`focal`/`focalspec`, final_xy↔`overlay`/`overlayspec`, id_dump↔`dump`
- [x] **Session 15**: `rag/evaluators/focal_curve.py` — curve 배열 range/sigma/DOF 계산 로직 + 유닛테스트(`tests/test_evaluators_focal_curve.py`). 정확한 계산식은 여전히 부서 확인 대기 중인 TBD — CLAUDE.md 초안 정의(range/sigma/DOF)를 그대로 구현
- [x] **Session 16**: `rag/evaluators/final_xy.py` — X/Y mean±3σ 계산 로직 + 유닛테스트(`tests/test_evaluators_final_xy.py`). ftpmodule overlay의 10-파라미터 모델 적합 residual 공식은 더 정교한 대안으로 남겨둠(부서 확인 후 채택 여부 결정)
- [x] **Session 17**: `models/schemas.py`에 `SpecCheckRequest.inline_data`/`inline_spec`, `IdDumpRequest`/`IdDumpResponse` 추가, `rag/features.py::resolve_data_and_spec()` 구현. identifier 기반 조회는 `clients/asml_api_client.py::fetch_feature_data_and_spec()`로 구현했으나 실제 ftpmodule 2-호출 계약(item 실행 + spec 페어링, equipment_id→서버 id 매핑)은 여전히 TBD — Phase 4와 동일하게 합리적으로 추정한 REST 엔드포인트로 파싱만 격리해둠
- [x] **Session 18**: `rag/id_dump.py` — `run_id_dump_analysis()` 구현(+ `tests/test_id_dump.py`). `judgements` 시드 데이터 채우기는 부서 실제 원인 분석 이력 확보 후 별도 작업으로 남음(Phase 5의 골든셋과 동일 사정)
- [x] **Session 19**: `api/routes_focal_curve.py`, `api/routes_final_xy.py`, `api/routes_id_dump.py` — 3개 엔드포인트, `rag/features.py`의 공용 `run_spec_check()`/`resolve_data_and_spec()`를 통해 로직 재사용 확인(`main.py`에 라우터 등록 완료). 기존 `api/routes_spec_check.py`도 동일 공용 함수로 리팩터링해 FR-5.1(본체 무수정) 요건을 실제로 만족시킴
- [ ] **Session 20 (선택, UC1~7 안정화 이후)**: 사내 Gemma4-260430 API의 tool-calling 지원 여부 확인 → 지원 시 `mcp_server/tools.py` 구현(기존 결정론적 함수 wrapping) → `/chat` 탐색 엔드포인트 추가

## 하지 말아야 할 것

- spec in/out 판정을 LLM에게 직접 계산시키지 말 것 — 반드시 `spec_evaluator.py`의 결정론적 함수로만 판정한다
- **핵심 판정 엔드포인트(`/query/*`)에 MCP/tool-calling 에이전트 방식을 적용하지 말 것** — 이 원칙은 향후 누군가 "LLM이 알아서 API 호출하게 하자"고 제안해도 유지한다. MCP는 8절에서 정의한 `/chat` 탐색 엔드포인트에서만 사용하고, 거기서도 툴은 기존 결정론적 함수를 감싸기만 하며 LLM이 숫자를 재계산하지 않도록 한다
- UC1/UC2용으로 RAG 코어 로직을 각각 복사해서 만들지 말 것
- pgvector 검색에서 메타데이터 필터 없이 전체 테이블에 벡터 유사도 검색을 걸지 말 것
- ASML API 호출에 타임아웃/재시도 없이 직접 `httpx.get()`을 뿌려 쓰지 말 것 (부서 API 장애 전파 방지)
- 사내 API 키를 코드에 하드코딩하지 말 것 — 반드시 `.env` / 환경변수 사용
