#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=naverwebtoon-2-0/backend
root=/opt/naverwebtoon-backend
service=naverwebtoon-backend.service
artifact=backend-linux-amd64
release_root="$root/releases"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0

main_revision() {
  local ref
  ref=$(timeout 12 git ls-remote --exit-code "https://github.com/$repo.git" refs/heads/main)
  printf '%s' "${ref%%$'\t'*}"
}

switch_release() {
  ln -s -- "$1" "$staging/next"
  mv -Tf -- "$staging/next" "$root/current"
}

revision=$(main_revision)
[[ "$revision" =~ ^[a-f0-9]{40}$ ]]
release="$release_root/$revision"
previous=$(readlink "$root/current" || true)
[[ "$previous" != "$release" ]] || exit 0

staging=$(mktemp -d "$root/.deploy.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
url="https://github.com/$repo/releases/download/deploy-$revision"
for asset in manifest manifest.sig "$artifact"; do
  http_code=$(curl --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --max-time 15 --max-filesize 25000000 --output "$staging/$asset" --write-out '%{http_code}' "$url/$asset")
  [[ "$http_code" != 404 ]] || exit 0
  [[ "$http_code" == 200 ]] || exit 1
done

# 서명에 저장소, 커밋, 바이너리 해시가 모두 들어 있어야 배포한다.
openssl pkeyutl -verify -pubin -rawin -inkey /etc/naverwebtoon-backend/release-signing.pub.pem \
  -in "$staging/manifest" -sigfile "$staging/manifest.sig"
(
  cd "$staging"
  {
    printf '%s %s\n' "$repo" "$revision"
    sha256sum "$artifact"
  } | cmp - manifest
)
[[ "$(main_revision)" == "$revision" ]] || exit 0

install -d -m 0755 "$release"
install -m 0755 "$staging/$artifact" "$release/$artifact"
switch_release "$release"
if systemctl restart "$service"; then
  for attempt in {1..10}; do
    if curl -fsS --max-time 1 http://127.0.0.1:8080/healthz \
      | jq -e --arg revision "$revision" '.status == "ok" and .revision == $revision' >/dev/null; then
      find "$release_root" -mindepth 1 -maxdepth 1 -type d \
        ! -path "$release" ! -path "$previous" -exec rm -rf -- {} +
      exit 0
    fi
    sleep 1
  done
fi

# 새 릴리스가 정상 응답하지 않으면 직전 실행 파일로 복구한다.
if [[ -n "$previous" ]]; then
  switch_release "$previous"
  systemctl restart "$service"
else
  rm -- "$root/current"
  systemctl stop "$service"
fi
exit 1
