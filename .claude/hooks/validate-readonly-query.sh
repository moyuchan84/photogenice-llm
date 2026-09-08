#!/bin/bash
# db-reader 서브에이전트가 실행하는 Bash 명령에서 SQL 쓰기 작업을 차단한다.
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

if echo "$COMMAND" | grep -iE '\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE)\b' > /dev/null; then
  echo "차단됨: db-reader는 읽기 전용입니다. SELECT 쿼리만 실행할 수 있습니다." >&2
  exit 2
fi

exit 0
