#!/usr/bin/env bash
# 네이버클라우드 Server의 Linux Init Script를 출력한다.
#   bash ops/render-init-script.sh > init-script.sh
# 운영 연동 키는 GitHub Actions Secrets에서 전달한다.
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
