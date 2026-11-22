#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=naverwebtoon-2-0/backend
root=/opt/naverwebtoon-backend
env_file=/etc/naverwebtoon-backend/backend.env
release_root="$root/releases"
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
export DOCKER_CONFIG="$root/.docker"

main_revision() {
  local ref
  ref=$(timeout 12 git ls-remote --exit-code "https://github.com/$repo.git" refs/heads/main)
  printf '%s' "${ref%%$'\t'*}"
}

switch_release() {
  ln -s -- "$1" "$staging/next"
  mv -Tf -- "$staging/next" "$root/current"
}

# compose <릴리스 디렉터리> <리비전> <compose 인자...>
compose() {
  local dir=$1 rev=$2
  shift 2
  REVISION="$rev" DB_INIT_DIR="$root/db-init" docker compose --project-name naverwebtoon --project-directory "$dir" \
    --env-file "$env_file" -f "$dir/docker-compose.prod.yml" "$@"
}

healthy() {
  for attempt in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:8080/healthz \
      | jq -e --arg revision "$1" '.status == "ok" and .revision == $revision' >/dev/null &&
      curl -fsS --max-time 3 http://127.0.0.1:8080/readyz >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

deploy() {
  local dir=$1 rev=$2
  compose "$dir" "$rev" run --rm migrate &&
    compose "$dir" "$rev" up -d --remove-orphans --wait --wait-timeout 180 &&
    healthy "$rev"
}

revision=$(main_revision)
[[ "$revision" =~ ^[a-f0-9]{40}$ ]]
release="$release_root/$revision"
previous=$(readlink "$root/current" || true)
[[ "$previous" != "$release" ]] || exit 0

staging=$(mktemp -d "$root/.deploy.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
url="https://github.com/$repo/releases/download/deploy-$revision"
for asset in manifest manifest.sig backend-image.tar.gz deploy.tar.gz; do
  http_code=$(curl --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --max-time 600 --max-filesize 800000000 --output "$staging/$asset" --write-out '%{http_code}' "$url/$asset")
  [[ "$http_code" != 404 ]] || exit 0
  [[ "$http_code" == 200 ]] || exit 1
done

# 서명에 저장소, 커밋, 이미지·배포 묶음 해시가 모두 들어 있어야 배포한다.
openssl pkeyutl -verify -pubin -rawin -inkey /etc/naverwebtoon-backend/release-signing.pub.pem \
  -in "$staging/manifest" -sigfile "$staging/manifest.sig"
(
  cd "$staging"
  {
    printf '%s %s\n' "$repo" "$revision"
    sha256sum backend-image.tar.gz deploy.tar.gz
  } | cmp - manifest
)
[[ "$(main_revision)" == "$revision" ]] || exit 0

install -d -m 0755 "$staging/bundle"
tar -xzf "$staging/deploy.tar.gz" -C "$staging/bundle" --no-same-owner --no-same-permissions
[[ -f "$staging/bundle/docker-compose.prod.yml" ]]
chmod -R go+rX "$staging/bundle"
gunzip -c "$staging/backend-image.tar.gz" | docker load >/dev/null
docker image inspect "naverwebtoon-backend:$revision" >/dev/null
rm -rf -- "$release"
mv -T -- "$staging/bundle" "$release"
if [[ ! -d "$root/db-init" ]]; then
  cp -R -- "$release/docker/db/init" "$root/db-init"
  chmod -R go+rX "$root/db-init"
fi

if deploy "$release" "$revision"; then
  switch_release "$release"
  # 현재·직전 릴리스만 남긴다.
  for dir in "$release_root"/*; do
    [[ "$dir" == "$release" || "$dir" == "$previous" ]] && continue
    docker image rm "naverwebtoon-backend:$(basename -- "$dir")" >/dev/null 2>&1 || true
    rm -rf -- "$dir"
  done
  exit 0
fi

# 새 릴리스가 정상 응답하지 않으면 직전 릴리스로 되돌린다.
# (마이그레이션은 되돌리지 않으므로 스키마 변경은 이전 버전과 호환되게 작성한다.)
if [[ -n "$previous" && -d "$previous" ]]; then
  compose "$previous" "$(basename -- "$previous")" up -d --remove-orphans --wait --wait-timeout 180 || true
else
  compose "$release" "$revision" down || true
fi
rm -rf -- "$release"
exit 1
