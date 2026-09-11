# 로컬 1차 E2E — ftpmodule 목업 → 우리 RAG 앱 → LLM

부서 백엔드(ftpmodule, `fleet`)를 **코드 수정 없이 로컬에 그대로 띄우고**, 그 DB에 목업을 넣은 뒤,
우리 앱이 실제 HTTP 계약으로 값·기준을 받아 판정하고 LLM 설명까지 내는지 확인한다.

```
[docker pgvector :5434]
   ├─ DB fleet     ← seed_fleet.py        (servers / file_manifest / file_records / spec_criteria …)
   └─ DB asmr_rag  ← seed_history_logs.py (logs_raw 과거 이력 → log_chunks 임베딩)

ftpmodule API :8001 (source=cache, FTP 0회) ──HTTP──▶ 우리 앱 :8000 ──▶ Ollama bge-m3 / llama3
                                                          ▲
                                               run_e2e.py (시나리오 호출 + 감사 레코드 확인)
```

## 무엇이 목업이고 무엇이 실제인가

| 구간 | 상태 |
|---|---|
| ftpmodule API·Store·마이그레이션·짝짓기(`focalspec_pairs`)·`/spec/map` | **실제 코드** (ftpmodule 무수정) |
| `file_records.records` 내용 | 목업 — `interface.md` 계약 모양대로 손으로 쓴 값(`scenario.py`) |
| `logs_raw` 과거 이력 | 목업 — ftpmodule DB엔 텍스트 로그 표가 없음(부서 ETL 원천 TBD) |
| 우리 앱 → ftpmodule 호출 조합 | 실제 계약 기준 구현(`clients/asml_api_client.py`) |
| 판정 | `rag/spec_evaluator.py::evaluate_criterion` (operator/threshold, 순수 함수) |
| LLM | Ollama `llama3` (사내 Gemma4 대역) |

## 시나리오 (`scenario.py`)

| 호출 | 값 vs 기준(verify) | 기대 |
|---|---|---|
| focal-curve MOCK-EUV-01 SCALE_CH1 | 0.62 vs \|v\| < 0.5 | OUT_OF_SPEC → RAG |
| focal-curve MOCK-EUV-01 ROTATION_CH1 | 0.43 vs \|v\| < 0.5 | IN_SPEC, margin 14% → RAG(근접) |
| focal-curve MOCK-EUV-01 TRANSLATION_CH1 | 0.12 vs \|v\| ≤ 1.0 | IN_SPEC, margin 88% → RAG 없음 |
| final-xy MOCK-EUV-01 RESIDUAL_X_AFTER | 3.4 vs 0 ≤ v < 3.0 | OUT_OF_SPEC → RAG |
| final-xy MOCK-EUV-01 RESIDUAL_Y_AFTER | 2.8 vs 0 ≤ v < 3.0 | IN_SPEC, margin 13% → RAG(근접) |
| focal-curve MOCK-EUV-02 SCALE_CH1 | 0.18 | IN_SPEC → RAG 없음(대조군) |
| id-dump MOCK-EUV-01 | dump item의 DETECTOR_TRP used=false로 질의 생성 | 과거 커넥터 산화 사례 검색 |

## 실행 (PowerShell, 저장소 루트)

사전 조건: Docker Desktop 실행 중, Ollama에 `bge-m3`·`llama3` pull 완료, `uv` 설치.

```powershell
# 0) 한 번만: DB 컨테이너 + ftpmodule 전용 venv + fleet DB
docker compose up -d
uv venv .venv-fleet --python 3.12
uv pip install --python .venv-fleet\Scripts\python.exe "fastapi>=0.110" "uvicorn[standard]>=0.29" "psycopg[binary,pool]>=3.2"
docker exec asmr-rag-db-1 psql -U asmr -d postgres -c "CREATE DATABASE fleet"
cd ftpmodule; ..\.venv-fleet\Scripts\python -m fleet.data.migrate up --dsn postgresql://asmr:asmr@localhost:5434/fleet; cd ..

# 1) .env (없으면 .env.example 복사) — 아래 값만 맞추면 된다
#    DATABASE_URL=postgresql://asmr:asmr@localhost:5434/asmr_rag
#    ASML_API_BASE=http://localhost:8001
#    ASML_API_KEY=
#    VERIFY_UI_ENABLED=true   # /verify, /verify/chat 화면 (기본 false)
#    CHAT_ENABLED=true        # POST /chat (기본 false)
Copy-Item .env.example .env   # 이미 있으면 생략하고 ASML_API_BASE만 수정

# 2) 목업 적재 (둘 다 재실행 안전)
$env:PYTHONIOENCODING="utf-8"
.venv-fleet\Scripts\python scripts\mock_fleet\seed_fleet.py
.venv\Scripts\python -m scripts.mock_fleet.seed_history_logs          # --reset: MOCK- 범위 지우고 재적재

# 3) 두 API 기동 (터미널 2개)
#    [터미널 A] ftpmodule
cd ftpmodule; $env:FLEET_DB_DSN="postgresql://asmr:asmr@localhost:5434/fleet"; $env:FLEET_ARCHIVE_URL="..\.fleet_archive"; ..\.venv-fleet\Scripts\uvicorn fleet.application.api:app --port 8001
#    [터미널 B] 우리 앱
.venv\Scripts\uvicorn main:app --port 8000

# 4) 시나리오 실행 (LLM 호출당 수십 초~1분+)
.venv\Scripts\python -m scripts.mock_fleet.run_e2e
.venv\Scripts\python -m scripts.mock_fleet.run_e2e --only SCALE_CH1   # 한 건만
```

### 채팅으로 확인하기 — `http://localhost:8000/verify/chat`

자연어로 대화하면 `POST /chat`(FR-7 탐색 인터페이스)이 한 턴을 이렇게 처리한다.

```
사용자 문장 ──▶ LLM 계획(JSON: 어떤 도구를 어떤 인자로)  ──▶ 서버 검증(도구 스키마 + ftpmodule 등록 설비/항목)
            ──▶ 도구 실행(mcp_server/tools.py — 기존 run_spec_check / run_id_dump_analysis / run_rag_judgement 그대로)
            ──▶ 답변 = 도구 결과로 조립한 문장 + 결과 카드(판정·margin·기준식·RAG 설명·근거 청크·감사 번호)
```

- LLM은 **도구 선택만** 한다. 판정·margin·수치는 결정론적 도구 결과에서만 나오고, LLM이 계획과 함께 낸 문장은 버린다.
- LLM 계획이 깨지거나 없는 이름을 만들면 규칙 기반 계획(fallback)으로 전환한다(화면에 `규칙(fallback)` 표시).
- 문장 근거가 분명한 요청(항목명·"값"·"찾아줘"·"판정 이력"·"dump")은 LLM이 고른 도구가 규칙과 다르면 규칙 계획을 쓴다
  — 로컬 llama3 8B가 명확한 요청에도 엉뚱한 도구를 고르거나 직전 턴에 답하는 것이 실측됐기 때문. LLM은 모호한 요청을 맡는다.
- FOCAL/OVERLAY 양쪽에 있는 항목명(SCALE_CH1 등)은 문장이나 직전 문맥에 focal/overlay 근거가 없으면 되묻는다.
- 직전 턴의 설비/기능 문맥이 이어진다 — `MOCK-EUV-01 SCALE_CH1 focal 판정해줘` 다음에 `그럼 ROTATION_CH1은?`
- 왼쪽 **시나리오** 버튼을 위에서부터 누르면 00-requirements.md 흐름(조회 → UC2 Focal → 후속 → Final XY → ID Dump → UC1 → 감사 이력 → 대조군)대로 진행된다.
- 로컬 llama3 기준 계획 15~30초, RAG 설명이 붙는 도구는 건당 1~2분 추가.

| 도구 | 하는 일 | 기록 |
|---|---|---|
| `list_equipment` / `list_spec_items` / `get_latest_spec_values` | ftpmodule 설비·기준표·최신 원시값 조회(판정 없음) | - |
| `check_spec` | 결정론 판정 + 조건부 RAG (`/query/focal-curve·final-xy·spec-check`와 같은 함수) | spec_evaluations (+judgements) |
| `analyze_id_dump` | ftpmodule dump 요약 → 항상 RAG (`/query/id-dump`와 같은 함수) | judgements |
| `ask_history` | 이력 질의 — 질의를 적재 청크와 같은 규칙으로 기능 분류 후 검색 + LLM | judgements |
| `search_history` | 기능별 필터 검색 합산(LLM 없음) | - |
| `get_recent_evaluations` | 저장된 판정/설명 이력 조회 | - |

Swagger(`http://localhost:8000/docs`)에서 `inline_data` 없이
`{"equipment_id": "MOCK-EUV-01", "parameter": "SCALE_CH1"}` 로 `/query/focal-curve`를 호출한다.
ftpmodule 쪽 원본 응답은 `http://localhost:8001/docs`에서 확인한다
(`POST /servers/1/items/focal` 본문 `{"source":"cache"}`).

## 정리

- 우리 DB 목업만: `.venv\Scripts\python -m scripts.mock_fleet.seed_history_logs --reset` (MOCK- 범위만 삭제 후 재적재)
- fleet DB 전체: `docker exec asmr-rag-db-1 psql -U asmr -d postgres -c "DROP DATABASE fleet"` 후 0)부터

⚠ pytest의 `db_pool` fixture는 `TRUNCATE`를 한다. `.env`가 개발 DB를 가리키면 목업이 지워지므로
테스트는 `TEST_DATABASE_URL`에 별도 DB를 지정해서 돌린다.

## 이 목업이 확정해주지 않는 것 (TBD — 부서 확인)

- `equipment_id` = ftpmodule `servers.servername` 로 취급
- `/spec/map` 필터(line/model/model_type)와 판정 `spec_level`은 설정값(`ASML_SPEC_*`) 고정
- "최신 측정" = anchor set(focal/overlay)에서 mtime 최대 파일 한 세대
- focal_curve/final_xy 기능이 **focalspec/overlayspec의 tag_value**를 판정한다는 해석
  (curve·overlay 배열 기반 range/σ 계산은 inline 경로에만 남아 있음)
- `logs_raw`의 실제 원천·메시지 어휘
