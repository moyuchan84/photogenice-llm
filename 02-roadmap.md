# 02-roadmap.md — 개발 로드맵 (v1)

용도: **개발팀 내부용.** Phase/마일스톤 단위 로드맵이다. "무엇을"은 `00-requirements.md`, "어떻게"는 `01-architecture.md`를 참고하고, 이 문서에서 정한 순서를 **세션 단위 실행 목록**으로 옮긴 것이 `CLAUDE.md`의 개발 순서 섹션이다. 즉 이 문서는 `CLAUDE.md`의 Session 번호가 어떤 더 큰 목표(Phase)에 속하는지 보여주는 상위 지도 역할을 한다.

```
00-requirements.md (무엇을)  →  01-architecture.md (어떻게)  →  02-roadmap.md (언제/순서, 이 문서)  →  CLAUDE.md (세션 단위 실행)
```

이 문서는 진행하면서 각 Phase의 **상태(state) 컬럼을 직접 갱신**하는 용도로 쓴다. 캘린더 날짜는 아직 확정하지 않았으므로(요구사항 §11 미해결 이슈), Phase 순서와 완료 기준(Exit Criteria) 중심으로 관리한다.

---

## 1. 전체 로드맵 개요

| Phase | 목표 | 관련 세션(`CLAUDE.md`) | 관련 하네스 요소 | 상태 |
|---|---|---|---|---|
| Phase 0 — 하네스 & 스펙 확정 | 안전장치부터 세팅, 미확정 스펙 확인 | Session 0 | `settings.json`(hooks+permissions) | ✅ 완료 |
| Phase 1 — UC1 임베딩 동기화 | DB polling 기반 백그라운드 임베딩 파이프라인 구축 | Session 1~5 | `schema-migrator`, `db-reader` | ✅ 완료 |
| Phase 2 — UC1 판정 서비스 | `/query/history` 엔드포인트 구현 | Session 6~8 | `rag-core-builder` | ✅ 완료 |
| Phase 3 — UC2 Spec Evaluator | 결정론적 spec in/out 판정 로직 | Session 9 | `spec-check-conventions` 스킬, `spec-evaluator-tester`, evaluator 순수성 hook | ✅ 완료 |
| Phase 4 — UC2 API 클라이언트 + RAG 결합 | ASML API 연동 및 조건부 RAG 호출 | Session 10~11 | `rag-core-builder` (재사용) | ✅ 완료 |
| Phase 5 — 평가/튜닝 | 골든셋 기반 품질 평가, 임계치·프롬프트 튜닝 | Session 12 | — | ⬜ 예정 |
| Phase 6 — 기능 확장 (Focal Curve/Final XY/ID Dump) | Feature Registry 패턴으로 3개 기능 추가 | Session 13~19 | `/add-feature` 스킬, worktree 병렬 서브에이전트 | ⬜ 예정 |
| Phase 7 — MCP 탐색 인터페이스 (선택) | `/chat` 탐색형 에이전트 인터페이스 | Session 20 | `mcp-tool-builder`(신규 필요) | ⬜ 보류 (선택 사항, 조건부 착수) |
| 상시 | 아키텍처 원칙 위반 여부 점검 | 각 Phase 종료 시점마다 | `code-reviewer` | 🔁 지속 |

상태 범례: ✅ 완료 · 🔶 진행중 · ⬜ 예정 · 🔁 지속 · ⏸ 보류

---

## 2. Phase 상세

### Phase 0 — 하네스 & 스펙 확정 ✅ 완료

- **선행조건**: 없음 (코드가 한 줄도 없을 때부터 시작)
- **산출물**: `.claude/settings.json`, 5개 서브에이전트, 2개 스킬, 3개 hook 스크립트 (모두 저장소 루트에 배포 및 실행권한 설정 완료)
- **남은 것**: `00-requirements.md` §11의 미확정 스펙(logs_raw 실제 스키마, ASML API 스펙, LLM/Embedding API 스펙) 확인 — 이건 부서/플랫폼팀과의 확인이 필요해 하네스 세팅과 별개로 계속 열려 있음
- **완료 기준(Exit Criteria)**: 하네스가 실제로 동작함(hook이 evaluator 파일에 LLM 호출 삽입 시 차단하는지 등)을 1회 확인

### Phase 1 — UC1 임베딩 동기화 (Session 1~5)

- **목표**: `logs_raw` → 청킹 → BGE-M3 임베딩 → `log_chunks` 저장까지의 배치 파이프라인
- **선행조건**: `logs_raw` 실제 스키마 확정, BGE-M3 API 스펙 확인
- **산출물**: `db/schema.sql`, `config.py`, `clients/embedding_client.py`, `workers/chunker.py`, `workers/embedding_sync_poller.py`, `scripts/backfill_embeddings.py`
- **완료 기준**: 소량 데이터로 폴링 워커를 강제 중단 후 재시작해도 중복 임베딩이 생기지 않음을 확인 (요구사항 FR-1.2)

### Phase 2 — UC1 판정 서비스 (Session 6~8) ✅ 완료

- **목표**: `/query/history` 엔드포인트로 자연어 질의 → 하이브리드 검색 → LLM 판정까지 end-to-end 동작
- **선행조건**: Phase 1 완료, Gemma4-260430 API 스펙 확인
- **산출물**: `clients/llm_client.py`, `rag/retriever.py`, `rag/prompt.py`, `rag/core.py`, `api/routes_history.py`, `api/deps.py`, `models/schemas.py`, `main.py`, `db/repo.py`(신규 — `create_pool`/`save_judgement` 단일화)
- **완료 기준**: 실제 과거 이슈로 수동 검증 통과 (FR-1.3) — 로컬 Ollama(bge-m3/llama3)로 시드 로그(반복되는 overlay 정렬 실패 시나리오)를 백필 후 `/query/history` 호출해 근거 chunk 기반 결론·권고조치가 생성되고 `judgements`에 저장됨을 확인
- **참고**: 사내 Gemma4-260430 API 스펙은 여전히 TBD(§11) — `LLM_PROVIDER=internal`로 전환만 하면 되도록 어댑터 뒤에 숨겨둠(Phase 0 스펙 확정과 무관하게 UC1 파이프라인 자체는 완결)

### Phase 3 — UC2 Spec Evaluator (Session 9) ✅ 완료

- **목표**: "판정과 설명의 분리" 원칙이 코드로 처음 구현되는 시점 — 프로젝트에서 **가장 먼저 강제해야 할 규칙**
- **선행조건**: 없음 (Phase 1/2와 독립적으로 시작 가능)
- **산출물**: `rag/spec_evaluator.py`(`SpecResult`, `SpecEvaluator` Protocol, `evaluate_spec()`, `GenericSpecEvaluator`) + `tests/test_spec_evaluator.py`(경계값: LSL/USL 일치, LSL==USL 스펙 폭 0 포함 12개 테스트)
- **완료 기준**: 유닛테스트 100% 통과(12/12), `enforce-evaluator-purity.sh` hook이 실제로 LLM 호출 삽입을 차단하는 것을 확인 (FR-2.2, NFR-1)
- **참고**: 검증 중 hook이 `jq` 의존성 때문에 이 개발 환경(jq 미설치 Git Bash)에서 조용히 no-op 되는 것을 발견 — `enforce-evaluator-purity.sh`에 jq 부재 시 grep/sed로 파싱하는 폴백을 추가해 실제로 차단되는 것을 확인함. `run_spec_check()`(DB 저장 + 조건부 RAG 호출)은 순수성 유지를 위해 의도적으로 Phase 4로 위임함

### Phase 4 — UC2 API 클라이언트 + RAG 결합 (Session 10~11) ✅ 완료

- **목표**: ASML API pull → 결정론적 판정 → 조건부 RAG 설명까지 `/query/spec-check`로 통합
- **선행조건**: Phase 3 완료, ASML API 실제 엔드포인트/인증/스키마 확정
- **산출물**: `clients/asml_api_client.py`(`HttpAsmlApiClient`, httpx+tenacity 타임아웃/재시도, 부서 스키마 확정 전까지 `_parse_response()`만 조정하면 되는 격리된 파싱 경계), `api/routes_spec_check.py`, `models/schemas.py`(`SpecCheckRequest`/`SpecCheckResponse` — identifier 기반 최소 스키마), `db/repo.py`(`save_spec_evaluation()`), `api/deps.py`(`AsmlApiClientDep`, `SettingsDep`)
- **완료 기준**: `spec_evaluations` 저장 확인, OOS 조건에서만 RAG가 호출됨을 확인 (FR-2.3, FR-2.4) — `tests/test_routes_spec_check.py`로 IN_SPEC(여유)/OUT_OF_SPEC/근접-margin 세 경로 모두 검증
- **참고**: ASML API 실제 엔드포인트/인증/응답 필드명은 여전히 TBD(§11) — `InternalLLMClient`/`InternalEmbeddingClient`와 동일한 패턴으로 합리적 추정 계약을 구현하고 파싱만 격리해둠. `SpecCheckRequest`의 `inline_data`/`inline_spec` 지원은 의도적으로 Phase 6(Session 17)으로 미룸

### Phase 5 — 평가/튜닝 (Session 12)

- **목표**: 과거 실제 OOS 사례 골든셋으로 원인 가설 품질 측정
- **선행조건**: Phase 2, Phase 4 완료
- **산출물**: 평가 스크립트, 튜닝된 `margin_pct` 임계치/`top_k`/프롬프트
- **완료 기준**: `00-requirements.md` §11의 파라미터 TBD 항목이 실측값으로 확정됨

### Phase 6 — 기능 확장: Focal Curve / Final XY / ID Dump (Session 13~19)

- **목표**: Feature Registry 패턴으로 3개 기능을 동일한 구조로 스캐폴딩
- **선행조건**: Phase 3(spec_check 패턴), Phase 2(root_cause 패턴, ID Dump용) 완료
- **산출물**: `rag/features.py`(FEATURE_REGISTRY), `rag/evaluators/focal_curve.py`, `rag/evaluators/final_xy.py`, `rag/id_dump.py`, `models/schemas.py`(SpecCheckRequest), 각 기능 라우터 3개
- **병렬화 포인트**: focal_curve/final_xy/id_dump는 서로 다른 파일만 건드리므로 `isolation: worktree` 서브에이전트로 병렬 진행 가능 (`claude-code-harness-guide.md` §2 참고)
- **완료 기준**: 3개 기능 모두 Feature Registry 등록만으로 동작하며 `run_rag_judgement()`/`run_spec_check()` 본체가 수정되지 않았음을 `code-reviewer`가 확인 (FR-5.1)

### Phase 7 — MCP 탐색 인터페이스 (선택, Session 20)

- **목표**: 엔지니어가 판정 결과를 놓고 탐색적으로 대화할 수 있는 `/chat` 인터페이스 (핵심 판정 파이프라인과는 별도)
- **선행조건**: Phase 1~6 안정화, 사내 Gemma4-260430의 tool-calling 지원 여부 확인 (FR-7.3)
- **착수 여부 판단 기준**: tool-calling 미지원 시 ReAct 프롬프트로 흉내내야 하며 신뢰도가 낮아지므로, 이 경우 착수 자체를 보류하고 별도 재검토
- **산출물**: `mcp_server/tools.py`(기존 결정론적 함수 wrapping만, 재계산 금지), `/chat` 라우터
- **완료 기준**: MCP 툴이 새로운 판정 계산을 하지 않고 조회/검색만 수행함을 `code-reviewer`가 확인 (FR-7.2)

---

## 3. 의존성 및 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| ASML API 실제 스키마/인증 미확정 | Phase 4 착수 지연 | Phase 0~3 기간 중 부서와 스펙 확정 병행 진행 (Phase 1~3은 ASML API와 독립적으로 진행 가능하도록 로드맵 순서를 설계함) |
| Gemma4-260430 tool-calling 미지원 | Phase 7 재설계 또는 보류 | Phase 7은 애초에 "선택 사항"으로 분리, 핵심 판정 파이프라인(Phase 0~6)에는 영향 없음 |
| 로그 볼륨 급증 | Phase 1 폴링 워커 처리량 부족 | 1차는 폴링 주기/LIMIT 조정 → 그래도 부족하면 `SKIP LOCKED`로 수평 확장 (00-requirements.md §2.2에서 현재는 범위 제외로 명시) |
| margin_pct 등 임계치가 조기에 하드코딩되어 굳어짐 | Phase 5 튜닝 무의미화 | 임계치는 반드시 환경변수(`SPEC_CHECK_MARGIN_THRESHOLD_PCT`)로 빼두고, Phase 5 이전까지는 "확정값 아님"으로 취급 |
| Phase 6에서 병렬 서브에이전트 작업 시 파일 충돌 | worktree 병합 시 conflict | evaluator별 파일이 서로 겹치지 않는지 착수 전 재확인 (현재 설계상 겹치지 않음) |

## 4. 마일스톤 체크리스트

- [x] Phase 0 — 하네스 세팅 완료
- [x] Phase 1 — UC1 임베딩 동기화
- [x] Phase 2 — UC1 판정 서비스 (`/query/history`)
- [x] Phase 3 — UC2 Spec Evaluator (판정/설명 분리 원칙 최초 구현)
- [x] Phase 4 — UC2 API 클라이언트 + RAG 결합 (`/query/spec-check`)
- [ ] Phase 5 — 평가/튜닝
- [ ] Phase 6 — Focal Curve / Final XY / ID Dump
- [ ] Phase 7 — MCP 탐색 인터페이스 (선택, 조건부)

> 이 체크리스트는 Phase 완료 시 직접 `[x]`로 갱신한다. 세션 단위의 더 세부적인 체크리스트는 `CLAUDE.md`의 "개발 순서" 섹션을 사용한다.

## 5. 관련 문서

- `00-requirements.md` — 요구사항 정의서
- `01-architecture.md` — 시스템 아키텍처 설계
- `CLAUDE.md` — 클로드 코드 세션 단위 작업 목록
- `claude-code-harness-guide.md` — 서브에이전트/hooks/skills 개발 하네스 가이드 (§6에 하네스 요소를 "언제 추가할지"에 대한 별도 표가 있으며, 이 로드맵의 Phase와 대응됨)
