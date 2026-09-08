#!/bin/bash
# .env, credentials 등 민감 파일에 대한 편집을 차단한다 (PreToolUse).
# 파일 전체 경로가 아니라 basename 기준으로 매칭해서 ".env.example" 같은
# 정상 샘플 파일까지 과잉 차단하지 않도록 한다.
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ -z "$FILE_PATH" ]]; then
  exit 0
fi

BASENAME=$(basename -- "$FILE_PATH")

# 정확히 이 이름들이거나, .env로 시작하되 .example/.sample로 끝나지 않는 파일만 차단
if [[ "$BASENAME" == "credentials.json" ]] || [[ "$BASENAME" == "credentials.yaml" ]] || [[ "$BASENAME" == "credentials.yml" ]]; then
  echo "차단됨: $FILE_PATH 는 보호된 파일입니다. 값은 환경변수로 관리하고, 이 파일은 직접 수정하지 마세요." >&2
  exit 2
fi

if [[ "$BASENAME" =~ ^\.env(\..+)?$ ]] && [[ "$BASENAME" != *.example ]] && [[ "$BASENAME" != *.sample ]]; then
  echo "차단됨: $FILE_PATH 는 보호된 파일입니다. 값은 환경변수로 관리하고, 이 파일은 직접 수정하지 마세요. (.env.example/.env.sample 같은 샘플 파일은 허용됩니다)" >&2
  exit 2
fi

exit 0
