#!/bin/bash
# spec 판정 로직(evaluators/*.py, spec_evaluator.py)에 LLM/임베딩/외부 API 호출이
# 섞여 들어가지 않았는지 편집 직후 검사한다 (Edit/MultiEdit/Write 공통). "판정과 설명의 분리" 원칙의 자동 강제.
INPUT=$(cat)
if command -v jq >/dev/null 2>&1; then
  FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
else
  # jq가 없는 환경(예: jq 미설치 Git Bash)을 위한 폴백 — grep/sed만으로 파싱한다.
  FILE_PATH=$(echo "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')
fi

if [[ -z "$FILE_PATH" || ! -f "$FILE_PATH" ]]; then
  exit 0
fi

if [[ "$FILE_PATH" == *"evaluators/"* ]] || [[ "$FILE_PATH" == *"spec_evaluator.py" ]]; then
  if grep -qE "llm_client|embedding_client|asml_api_client" "$FILE_PATH"; then
    echo "차단됨: $FILE_PATH 에서 llm_client/embedding_client/asml_api_client 호출이 감지되었습니다. spec 판정 로직(evaluators/*.py, spec_evaluator.py)은 외부 의존성 없는 순수 함수여야 합니다. 해당 호출을 제거하거나 rag/core.py 쪽으로 옮기세요." >&2
    exit 2
  fi
fi

exit 0
