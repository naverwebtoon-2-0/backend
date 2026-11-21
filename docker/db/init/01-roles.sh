#!/bin/bash
# 최초 초기화 때 한 번 실행된다 (postgres 이미지의 /docker-entrypoint-initdb.d).
# nw_owner: 스키마 소유자 (migrate 전용, DDL)
# nw_app:   api·worker용 (DML만)
set -euo pipefail
: "${NW_OWNER_PASSWORD:?NW_OWNER_PASSWORD가 필요합니다}"
: "${NW_APP_PASSWORD:?NW_APP_PASSWORD가 필요합니다}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner_pw="$NW_OWNER_PASSWORD" -v app_pw="$NW_APP_PASSWORD" -f /docker-entrypoint-initdb.d/roles.sql.tpl
