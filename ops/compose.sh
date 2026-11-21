#!/usr/bin/env bash
# 현재 릴리스로 docker compose를 실행한다. 예: naverwebtoon-backend-compose ps
set -euo pipefail
root=/opt/naverwebtoon-backend
current=$(readlink -f "$root/current")
export REVISION="${current##*/}"
export DB_INIT_DIR="$root/db-init"
export DOCKER_CONFIG="$root/.docker"
exec docker compose --project-name naverwebtoon --project-directory "$current" \
  --env-file /etc/naverwebtoon-backend/backend.env -f "$current/docker-compose.prod.yml" "$@"
