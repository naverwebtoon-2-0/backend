#!/usr/bin/env bash
# 네이버클라우드 Server의 Linux Init Script를 출력한다.
#   bash ops/render-init-script.sh > init-script.sh
# ops/secrets.env(커밋 금지)가 있으면 그 KEY=VALUE를 서버 환경변수(/etc/naverwebtoon-backend/backend.env)에 덮어쓴다.
# API 키·CORS origin·초대 코드 등은 이 파일로 넣는다. 형식은 ops/secrets.env.example 참고.
set -euo pipefail

cd -- "$(dirname -- "$0")"
[[ -s release-signing.pub.pem ]]

write_file() {
  printf "cat > '%s' <<'NCP_BOOTSTRAP_FILE'\n" "$2"
  sed '/^# /d' "$1"
  printf 'NCP_BOOTSTRAP_FILE\n'
}

printf '#!/usr/bin/env bash\n'
sed '/^# /d' init.sh
if [[ -s secrets.env ]]; then
  override=$(grep -Ev '^[[:space:]]*(#|$)' secrets.env || true)
  if grep -Evq '^[A-Z][A-Z0-9_]*=[^[:cntrl:]]*$' <<<"$override"; then
    echo 'ops/secrets.env에는 KEY=VALUE 형식의 줄만 쓸 수 있습니다.' >&2
    exit 1
  fi
  printf "(umask 077; cat > /etc/naverwebtoon-backend/override.env <<'NCP_BOOTSTRAP_FILE'\n%s\nNCP_BOOTSTRAP_FILE\n)\n" "$override"
  cat <<'MERGE'
while IFS= read -r line; do
  key=${line%%=*}
  sed -i "/^${key}=/d" /etc/naverwebtoon-backend/backend.env
  printf '%s\n' "$line" >> /etc/naverwebtoon-backend/backend.env
done < /etc/naverwebtoon-backend/override.env
rm -f /etc/naverwebtoon-backend/override.env
MERGE
fi
write_file release-signing.pub.pem /etc/naverwebtoon-backend/release-signing.pub.pem
for unit in backend.service backend-update.service backend-update.timer backend-tls.service backend-tls.timer; do
  write_file "$unit" "/etc/systemd/system/naverwebtoon-$unit"
done
write_file update.sh /usr/local/sbin/naverwebtoon-backend-update
write_file tls.sh /usr/local/sbin/naverwebtoon-backend-tls
write_file compose.sh /usr/local/sbin/naverwebtoon-backend-compose
cat <<'BOOTSTRAP_END'
chmod 0755 /usr/local/sbin/naverwebtoon-backend-update /usr/local/sbin/naverwebtoon-backend-tls \
  /usr/local/sbin/naverwebtoon-backend-compose
chmod 0600 /etc/naverwebtoon-backend/backend.env
nginx -t
systemctl enable --now nginx.service
systemctl reload nginx
systemctl daemon-reload
systemctl enable naverwebtoon-backend.service
systemctl enable --now naverwebtoon-backend-update.timer naverwebtoon-backend-tls.timer
BOOTSTRAP_END
