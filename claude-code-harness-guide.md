# 클로드 코드 개발 하네스 가이드 — ASML 로그 RAG 시스템

이 문서는 지금까지 정리한 아키텍처(01-architecture.md, CLAUDE.md)를 **클로드 코드로 실제로, 안정적으로 구현**하기 위한 개발 환경 구성 가이드입니다. 서브에이전트(멀티에이전트 구조), hooks, skills, worktree, 권한/샌드박스 설정을 이 프로젝트에 맞춰 구체적으로 설계했습니다.

이 문서는 claude.ai가 아니라 로컬 터미널에서 **Claude Code**(`claude` CLI)로 작업할 때를 전제로 합니다.

---

## 0. 왜 하네스가 필요한가

이 프로젝트는 "판정은 결정론적 코드, LLM은 설명만"이라는 원칙이 아키텍처 전체를 관통합니다. 문제는 **이 원칙을 사람이 매번 프롬프트로 상기시키는 방식은 지켜지지 않는다는 것**입니다 — 세션이 길어지거나 컨텍스트가 압축(compaction)되면 잊혀집니다. 하네스의 역할은 이 원칙과 프로젝트 규칙을 다음 세 계층으로 코드화해서, 사람이 매번 챙기지 않아도 지켜지게 만드는 것입니다.

| 계층 | 역할 | 이 프로젝트에서의 예 |
|---|---|---|
| CLAUDE.md | 항상 로드되는 사실/규칙 | 디렉토리 구조, 코딩 컨벤션, 하지 말아야 할 것 |
| Skills | 필요할 때만 로드되는 절차/도메인 지식 | spec 판정 컨벤션, 기능 스캐폴딩 절차 |
| Hooks | LLM 판단 없이 항상 실행되는 결정론적 체크 | evaluator 파일에 LLM 호출이 섞였는지 자동 검사 |
| Subagents | 격리된 컨텍스트에서 특정 역할만 수행 | DB 마이그레이션 작성, spec evaluator 테스트, 코드 리뷰 |

---

## 1. `.claude/` 디렉토리 구조

```
your-repo/
├── CLAUDE.md                          # 이미 작성됨 — 프로젝트 개요, 컨벤션, 세션 체크리스트
├── .claude/
│   ├── settings.json                  # 팀 공유 설정: hooks, permissions (버전관리 O)
│   ├── settings.local.json            # 개인 설정 (버전관리 X, gitignore)
│   ├── agents/
│   │   ├── schema-migrator.md
│   │   ├── db-reader.md
│   │   ├── spec-evaluator-tester.md
│   │   ├── rag-core-builder.md
│   │   └── code-reviewer.md
│   ├── skills/
│   │   ├── spec-check-conventions/SKILL.md
│   │   └── add-feature/SKILL.md
│   └── hooks/
│       ├── protect-secrets.sh
│       ├── enforce-evaluator-purity.sh
│       └── format-python.sh
├── app/                                # 이전 문서의 애플리케이션 코드
└── tests/
```

**[적용 완료]** 이 구조는 이미 저장소 루트(`D:\code\samsung\asmr-rag`)에 실제 `.claude/`와 `CLAUDE.md`로 배포되어 있습니다 (hooks는 실행 권한까지 설정됨). 별도의 `claude-starter-kit/` 사본을 복사하는 절차는 더 이상 필요하지 않습니다 — 저장소 루트에서 바로 `claude` 명령을 실행하면 됩니다.

---

## 2. 서브에이전트 구조 (멀티에이전트)

서브에이전트는 "메인 대화를 흐리지 않고 격리된 컨텍스트에서 처리해야 하는 일"에 씁니다. 이 프로젝트는 성격이 뚜렷이 다른 작업들(DB 마이그레이션, 결정론적 로직 검증, RAG 프롬프트 튜닝, 코드 리뷰)이 섞여 있어서 서브에이전트로 나누기 좋습니다.

| 서브에이전트 | 역할 | 도구 제한 | 언제 위임 |
|---|---|---|---|
| `schema-migrator` | `db/schema.sql` 작성·수정, 마이그레이션 검토 | Read, Edit, Grep, Bash(제한적) | Session 1, 13 (스키마 변경) |
| `db-reader` | 읽기 전용 DB 조회로 데이터 확인 | Bash(SELECT만, hook으로 강제) | 개발 중 데이터 확인용 |
| `spec-evaluator-tester` | `spec_evaluator.py`/`evaluators/*.py` 구현과 유닛테스트 | Read, Edit, Bash(pytest) | Session 9, 15, 16 |
| `rag-core-builder` | `retriever.py`/`prompt.py`/`core.py` 구현 | Read, Edit, Grep, Bash | Session 6, 7 |
| `code-reviewer` | 커밋 전 아키텍처 원칙 위반 여부 검토 | Read, Grep, Bash(git diff) | 매 Phase 종료 시점 |

각 파일을 그대로 `.claude/agents/`에 두면 됩니다. 예시(`spec-evaluator-tester.md`):

```markdown
---
name: spec-evaluator-tester
description: spec_evaluator.py와 evaluators/*.py를 구현하고 유닛테스트를 작성·실행한다. spec in/out 판정 로직을 다룰 때 반드시 사용.
tools: Read, Edit, Grep, Glob, Bash
model: sonnet
skills:
  - spec-check-conventions
---

당신은 이 프로젝트의 spec 판정 로직(spec_evaluator.py, evaluators/*.py)만 담당하는
전문 에이전트입니다.

절대 규칙 (위반 시 리뷰 반려됨):
- 이 파일들에는 어떤 형태로도 LLM/임베딩 API를 호출하는 코드를 작성하지 않는다.
  (import llm_client, import embedding_client 금지)
- 모든 평가 함수는 외부 의존성 없는 순수 함수로 작성한다 (DB, API, LLM 호출 없음).
- 새 evaluator를 추가하거나 수정할 때마다 경계값(LSL/USL 정확히 일치하는 값 포함)에
  대한 유닛테스트를 함께 작성한다.

작업 순서:
1. 기존 evaluator 코드와 테스트를 확인한다.
2. 요구된 로직을 구현하거나 수정한다.
3. pytest로 해당 모듈의 테스트를 실행하고 결과를 보고한다.
4. 테스트가 실패하면 원인을 분석하고 고친다.
```

`skills: [spec-check-conventions]`로 이 에이전트가 시작할 때 도메인 규칙(아래 4절)이 항상 주입되도록 했습니다.

### 왜 이렇게 나눴는가

- **`schema-migrator`와 `spec-evaluator-tester`를 분리한 이유**: DB 스키마 변경과 판정 로직 변경은 리스크 프로파일이 다릅니다. 스키마 변경은 되돌리기 어렵고, 판정 로직은 순수 함수라 실수해도 테스트로 바로 드러납니다. 도구 권한도 다르게 줄 수 있어야 합니다.
- **`code-reviewer`를 읽기 전용으로 만든 이유**: 이 에이전트의 유일한 임무는 "판정과 설명의 분리" 같은 아키텍처 원칙이 지켜졌는지 확인하는 것이지, 코드를 고치는 게 아닙니다. 쓰기 권한이 없으면 실수로 원칙 위반 코드를 스스로 만들 위험도 없습니다.
- **`rag-core-builder`는 넓은 권한**: 여러 파일(retriever/prompt/core)을 오가며 반복 수정해야 하므로 tools를 좁게 제한하지 않았습니다. 대신 이 에이전트가 만지는 파일 범위는 hooks(4절)로 별도 감시합니다.

### 병렬 작업 (worktree)

Focal Curve(Session 15)와 Final XY(Session 16), ID Dump(Session 18)는 서로 다른 파일(`evaluators/focal_curve.py` vs `evaluators/final_xy.py` vs `id_dump.py`)만 건드리므로 병렬로 진행해도 충돌이 없습니다. 이런 경우 서브에이전트에 `isolation: worktree`를 주면 각자 독립된 git worktree에서 작업하므로 안전합니다:

```yaml
---
name: focal-curve-builder
description: focal_curve evaluator만 전담. Final XY/ID Dump 작업과 병렬 실행 가능.
tools: Read, Edit, Bash
isolation: worktree
skills:
  - spec-check-conventions
---
```

메인 세션에서 "focal-curve-builder로 Focal Curve evaluator 작업하고, 동시에 다른 세션에서 final-xy-builder로 Final XY 작업해줘" 식으로 지시하면, 각각 격리된 워크트리에서 작업 후 결과만 메인 대화로 돌아옵니다.

---

## 3. Skills — 도메인 지식과 반복 절차

### 3.1 참조형 스킬: `spec-check-conventions`

evaluator를 만들 때마다 아키텍처 원칙을 매번 설명하지 않도록, 상시 참조되는 도메인 지식을 스킬로 뺍니다. `spec-evaluator-tester`와 `rag-core-builder`에 `skills:` 필드로 미리 로드해 둡니다.

```markdown
---
name: spec-check-conventions
description: 이 프로젝트의 spec 판정 아키텍처 규칙 — 판정과 설명의 분리, feature_type 스키마, evaluator 작성 규칙.
---

## 판정과 설명의 분리
- spec in/out 같은 숫자 비교는 절대 LLM에게 맡기지 않는다. evaluators/*.py의 순수 함수로만 계산한다.
- LLM(Gemma4-260430)은 OUT_OF_SPEC이거나 margin_pct가 임계치(기본 10%) 이하일 때만,
  이미 계산된 결과를 설명하는 용도로만 호출한다.

## 스키마 규칙
- spec_evaluations: feature_type, determination, margin_pct, metrics_json(계산된 통계),
  raw_data_json/raw_spec_json(원본 보존)
- log_chunks/judgements에도 feature_type을 반드시 채운다 (검색 스코프 분리용)

## Feature Registry 패턴
- 새 기능은 FEATURE_REGISTRY에 evaluator + prompt_context를 등록하는 방식으로 추가한다.
- run_rag_judgement()/run_spec_check() 본체는 새 기능 추가 시 수정하지 않는다.
```

### 3.2 작업형 스킬: `/add-feature`

Session 14~19에서 반복되는 "새 기능 스캐폴딩" 작업(Feature Registry 등록, evaluator 파일 생성, 엔드포인트 생성, 테스트 파일 생성)을 매번 수동으로 하지 않도록 절차형 스킬로 만듭니다. 사이드 이펙트가 있는 작업이라 `disable-model-invocation: true`로 사람이 직접 호출할 때만 실행되게 합니다.

```markdown
---
name: add-feature
description: 새 spec_check 기능을 Feature Registry에 스캐폴딩한다.
disable-model-invocation: true
argument-hint: [feature-name]
---

$ARGUMENTS 기능을 다음 순서로 스캐폴딩한다:

1. app/rag/evaluators/$ARGUMENTS.py 생성 — 결정론적 evaluate() 함수 스켈레톤
   (spec_evaluator.py의 Evaluator 인터페이스를 따름, LLM/DB/API 호출 절대 금지)
2. app/rag/features.py의 FEATURE_REGISTRY에 $ARGUMENTS 항목 추가
   (kind: "spec_check", evaluator, prompt_context 자리 채움)
3. app/api/routes_$ARGUMENTS.py 생성 — SpecCheckRequest를 받아
   resolve_data_and_spec() → run_spec_check() 호출하는 얇은 라우터
4. tests/test_$ARGUMENTS.py 생성 — 경계값 테스트 스켈레톤 포함
5. 생성한 파일 목록과 다음에 채워야 할 TODO를 요약해서 보고한다.

기존 focal_curve/final_xy 구현을 참고 패턴으로 삼는다.
```

`/add-feature ion-dose` 처럼 호출하면 매번 같은 구조로 스캐폴딩되어, 기능이 늘어나도 구조가 흔들리지 않습니다.

---

## 4. Hooks — 원칙을 코드로 강제하기

Hooks는 "LLM이 규칙을 기억하길 바라는 것"과 "규칙이 항상 지켜지는 것"의 차이를 메웁니다. 이 프로젝트에서 가장 중요한 hook은 **판정 로직에 LLM 호출이 섞이지 않았는지 자동 검사하는 것**입니다.

### 4.1 evaluator 순수성 검사 (PostToolUse)

`.claude/hooks/enforce-evaluator-purity.sh`:

```bash
#!/bin/bash
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# evaluators/ 디렉토리나 spec_evaluator.py가 수정된 경우에만 검사
if [[ "$FILE_PATH" == *"evaluators/"* ]] || [[ "$FILE_PATH" == *"spec_evaluator.py" ]]; then
  if grep -qE "llm_client|embedding_client|asml_api_client" "$FILE_PATH" 2>/dev/null; then
    echo "차단됨: $FILE_PATH 에 LLM/임베딩/API 클라이언트 호출이 감지됨. spec 판정 로직은 순수 함수여야 합니다." >&2
    exit 2
  fi
fi

exit 0
```

`.claude/settings.json`에 등록:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/enforce-evaluator-purity.sh" }
        ]
      }
    ]
  }
}
```

`PostToolUse`라서 편집 자체는 막지 못하지만, 즉시 되돌리라는 피드백이 Claude에게 전달됩니다. 편집 자체를 막고 싶다면 같은 스크립트를 `PreToolUse`에 걸고 `tool_input.new_string`(또는 `content`)을 검사하도록 바꾸면 됩니다.

### 4.2 시크릿/설정 파일 보호 (PreToolUse)

`.env`, `db/schema.sql`(직접 수정보다 마이그레이션을 통해야 함) 등을 실수로 건드리지 않도록 보호합니다.

```bash
#!/bin/bash
# protect-secrets.sh
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
PROTECTED=(".env" ".env.local" "credentials.json")

for pattern in "${PROTECTED[@]}"; do
  if [[ "$FILE_PATH" == *"$pattern"* ]]; then
    echo "차단됨: $FILE_PATH 는 보호된 파일입니다. 직접 수정하지 말고 환경변수로 관리하세요." >&2
    exit 2
  fi
done
exit 0
```

### 4.3 커밋 전 자동 포맷 (PostToolUse)

Python 파일 편집 후 자동으로 정리해서, 스타일 문제로 리뷰 시간을 낭비하지 않게 합니다.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          { "type": "command", "command": "jq -r '.tool_input.file_path' | grep '\\.py$' | xargs -r ruff format" }
        ]
      }
    ]
  }
}
```

### 4.4 세션 시작 시 원칙 리마인드 (SessionStart)

컨텍스트가 compaction되거나 새 세션을 시작해도 핵심 원칙이 항상 눈에 띄도록 합니다.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '핵심 원칙: (1) spec 판정은 evaluators/*.py의 순수 함수로만 (2) LLM은 OOS/근접 시에만 설명 생성 (3) 새 기능은 FEATURE_REGISTRY 등록 방식으로, run_rag_judgement 본체는 수정하지 않음'"
          }
        ]
      }
    ]
  }
}
```

### 4.5 db-reader를 읽기 전용으로 강제 (서브에이전트 hook)

앞서 만든 `db-reader` 서브에이전트가 실수로라도 쓰기 쿼리를 실행하지 못하도록, 서브에이전트 frontmatter에 직접 hook을 건다(공식 문서의 database query validator 패턴을 그대로 적용):

```yaml
---
name: db-reader
description: 읽기 전용 DB 조회로 데이터를 확인한다. UPDATE/INSERT/DELETE는 절대 실행하지 않는다.
tools: Bash
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/validate-readonly-query.sh"
---

당신은 읽기 전용 DB 분석가입니다. SELECT 쿼리로만 데이터를 확인합니다.
쓰기 작업이 필요하면 직접 실행하지 말고 사람에게 어떤 마이그레이션/쓰기가
필요한지 설명하세요.
```

---

## 5. 권한/샌드박스 기본값

`.claude/settings.json`에 다음을 함께 넣어두는 것을 권장합니다.

```json
{
  "permissions": {
    "deny": [
      "Read(.env)",
      "Read(**/*credentials*)",
      "Bash(rm -rf *)"
    ],
    "ask": [
      "Bash(alembic upgrade *)",
      "Bash(psql * -c DROP*)"
    ]
  }
}
```

- 마이그레이션 적용(`alembic upgrade`)이나 DB `DROP` 계열 명령은 되돌리기 어려우므로 자동 승인 대신 매번 확인받도록 `ask`에 넣었습니다.
- `.env`나 credentials 파일은 읽기 자체를 막아서, 실수로 로그나 커밋 메시지에 값이 노출되는 것을 예방합니다.

네트워크 샌드박스를 쓰는 환경이라면, 이 프로젝트가 실제로 호출하는 외부 호스트만 허용 목록에 넣습니다: 사내 LLM/임베딩 API 엔드포인트, ASML API 엔드포인트, PostgreSQL 호스트. 그 외 아웃바운드는 막아두면 사내망 정책 위반이나 의도치 않은 외부 호출을 원천 차단할 수 있습니다.

---

## 6. 하네스 구축 로드맵

앞선 프로젝트 로드맵(Phase 0~5, 확장 Phase)에 맞춰 하네스 요소를 언제 추가할지 정리했습니다. **하네스는 한 번에 다 만들지 않고, 필요해지는 시점에 하나씩 추가**하는 것이 맞습니다 — 안 쓰는 서브에이전트/스킬은 컨텍스트만 차지합니다.

| 시점 | 추가할 것 | 이유 |
|---|---|---|
| 프로젝트 시작 (Phase 0 이전) | `CLAUDE.md`, `settings.json`(시크릿 보호 hook + 권한 설정) | 코드가 한 줄도 없을 때부터 안전장치부터 |
| Phase 0~1 (스키마/백필) | `schema-migrator`, `db-reader` 서브에이전트 | DB 작업이 시작되는 시점 |
| Phase 2 (UC1 서비스) | `rag-core-builder` 서브에이전트 | retriever/prompt/core 구현 시작 |
| Phase 3 (Spec Evaluator) | `spec-check-conventions` 스킬, `spec-evaluator-tester` 서브에이전트, evaluator 순수성 hook(4.1) | "판정과 설명 분리" 원칙이 코드로 처음 구현되는 시점 — 가장 먼저 강제해야 할 규칙 |
| Session 13~19 (Focal Curve/Final XY/ID Dump) | `/add-feature` 스킬, worktree 병렬 서브에이전트 | 같은 패턴이 3번 반복되는 시점 — 스캐폴딩 자동화 가치가 커짐 |
| 상시 | `code-reviewer` 서브에이전트 | 각 Phase 종료 시점마다 아키텍처 원칙 위반 여부 점검 |
| Session 20 (MCP, 선택) | `mcp-tool-builder` 서브에이전트(도구를 mcp_server/ 디렉토리로 제한) | MCP 레이어 착수 시점 — 8절 원칙(재계산 금지)을 이 서브에이전트 tools 제한으로도 이중 강제 |

---

## 7. 함께 제공하는 스타터 킷

`claude-starter-kit/.claude/` 아래에 이 문서에서 설명한 설정을 실제로 동작하는 파일로 만들어 두었습니다.

```
claude-starter-kit/.claude/
├── settings.json                              # hooks + permissions 예시
├── agents/
│   ├── schema-migrator.md
│   ├── db-reader.md
│   ├── spec-evaluator-tester.md
│   ├── rag-core-builder.md
│   └── code-reviewer.md
├── skills/
│   ├── spec-check-conventions/SKILL.md
│   └── add-feature/SKILL.md
└── hooks/
    ├── protect-secrets.sh
    ├── enforce-evaluator-purity.sh
    └── validate-readonly-query.sh
```

**[적용 완료]** 위 파일들은 이미 저장소 루트 `.claude/`에 그대로 존재하며 `chmod +x`도 적용되어 있습니다. 새로 세팅할 때만 이 섹션을 참고해서 동일한 구조로 복사하면 됩니다.
