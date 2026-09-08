#!/bin/bash
# 편집된 .py 파일을 ruff format으로 자동 포맷팅한다 (Edit/MultiEdit/Write 공통).
RUFF="ruff"
if ! command -v ruff >/dev/null 2>&1; then
  # PATH에 ruff가 없는 환경(예: venv 미활성화)을 위한 폴백 — 프로젝트 venv에서 직접 찾는다.
  if [[ -x "$CLAUDE_PROJECT_DIR/.venv/Scripts/ruff.exe" ]]; then
    RUFF="$CLAUDE_PROJECT_DIR/.venv/Scripts/ruff.exe"
  elif [[ -x "$CLAUDE_PROJECT_DIR/.venv/bin/ruff" ]]; then
    RUFF="$CLAUDE_PROJECT_DIR/.venv/bin/ruff"
  else
    exit 0
  fi
fi

INPUT=$(cat)
if command -v jq >/dev/null 2>&1; then
  FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
else
  # jq가 없는 환경(예: jq 미설치 Git Bash)을 위한 폴백 — grep/sed만으로 파싱한다.
  FILE_PATH=$(echo "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')
fi

if [[ "$FILE_PATH" == *.py && -f "$FILE_PATH" ]]; then
  "$RUFF" format "$FILE_PATH"
fi

exit 0
