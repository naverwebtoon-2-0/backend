#!/usr/bin/env bash
# 실행 중인 api + worker를 상대로 한 스모크 테스트 (AI_BACKEND=fake, STORAGE_BACKEND=fake 기준).
#   scripts/smoke.sh http://127.0.0.1:8080 [invite_code]
# 가입 → 작품·회차 → 업로드 → 분석(워커) → 검수 → 게시 → 독자 조회 → 음원 다운로드
set -euo pipefail
base=${1:-http://127.0.0.1:8080}
invite=${2:-invite-code-1}
email="smoke-$(date +%s)-$RANDOM@example.com"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

api() {
  local method=$1 path=$2 body=${3:-}
  local args=(-fsS --max-time 15 -X "$method" "$base$path" -H 'Content-Type: application/json')
  [[ -n "${token:-}" ]] && args+=(-H "Authorization: Bearer $token")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}"
}

api POST /v1/auth/signup "{\"email\":\"$email\",\"password\":\"password123\",\"nickname\":\"smoke\",\"invite_code\":\"$invite\"}" >/dev/null
token=$(api POST /v1/auth/login "{\"email\":\"$email\",\"password\":\"password123\"}" | jq -r .access_token)
series=$(api POST /v1/series '{"title":"smoke","genre":"etc"}' | jq -r .id)
episode=$(api POST "/v1/series/$series/episodes" '{"title":"ep1"}' | jq -r .id)

# 1x1 PNG
printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82' > "$tmp/page.png"
size=$(wc -c < "$tmp/page.png" | tr -d ' ')
upload=$(api POST "/v1/episodes/$episode/images/presign" \
  "{\"files\":[{\"filename\":\"page.png\",\"content_type\":\"image/png\",\"size\":$size}]}" | jq -c '.uploads[0]')
url=$(jq -r .url <<<"$upload")
image=$(jq -r .image_id <<<"$upload")
# PUBLIC_BASE_URL이 외부 주소여도 로컬 서버로 올린다.
curl -fsS --max-time 15 -X PUT "$base/_fake-storage/${url#*/_fake-storage/}" -H 'Content-Type: image/png' \
  --data-binary "@$tmp/page.png" >/dev/null
api POST "/v1/episodes/$episode/images/complete" "{\"image_ids\":[\"$image\"]}" >/dev/null
job=$(api POST "/v1/episodes/$episode/process" '{}' | jq -r .job.id)

for attempt in {1..60}; do
  status=$(api GET "/v1/jobs/$job" | jq -r .status)
  [[ "$status" == queued || "$status" == running ]] || break
  sleep 1
done
[[ "$status" == succeeded ]] || { echo "작업 상태: $status" >&2; exit 1; }

api PATCH "/v1/episodes/$episode/images/$image" '{"reviewed":true}' >/dev/null
state=$(api POST "/v1/episodes/$episode/publish" '{}' | jq -r .state)
[[ "$state" == published ]]

token=""
audio=$(api GET "/v1/public/episodes/$episode" | jq -r '[.playback.cues[] | select(.audio_url != null)][0].id')
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base/v1/public/episodes/$episode/cues/$audio/audio")
[[ "$code" == 302 ]]
echo "smoke ok: episode $episode published"
