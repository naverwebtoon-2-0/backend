#!/usr/bin/env bash
# /healthz가 지정한 revision을 돌려주고 /readyz가 200이 될 때까지 기다린다.
#   scripts/wait-healthy.sh <revision> [base_url]
set -euo pipefail
revision=$1
base=${2:-http://127.0.0.1:8080}
for attempt in {1..60}; do
  if curl -fsS --max-time 2 "$base/healthz" | jq -e --arg r "$revision" '.status == "ok" and .revision == $r' >/dev/null &&
    curl -fsS --max-time 3 "$base/readyz" >/dev/null; then
    echo "healthy after $attempt checks"
    exit 0
  fi
  sleep 2
done
echo "서버가 정상 상태가 되지 않았습니다: $base" >&2
exit 1
