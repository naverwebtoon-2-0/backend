#!/usr/bin/env bash
set -euo pipefail
umask 022

metadata=http://169.254.169.254
token=$(curl -fsS --noproxy '*' --max-time 4 -X PUT "$metadata/latest/api/token" \
  -H 'X-NCP-METADATA-TOKEN-TTL-SECONDS: 60')
ip=$(curl -fsS --noproxy '*' --max-time 4 -H "X-NCP-METADATA-TOKEN: $token" \
  "$metadata/latest/meta-data/publicIp")
[[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]

site=/etc/nginx/conf.d/naverwebtoon-backend-tls.conf
cert=/etc/letsencrypt/live/naverwebtoon-backend
serial() {
  if [[ -r "$cert/fullchain.pem" ]]; then
    openssl x509 -in "$cert/fullchain.pem" -noout -serial
  fi
}

previous=$(serial)
# 짧은 유효 기간의 IP 인증서는 만료 전 자동으로 갱신한다.
/snap/bin/certbot certonly --webroot --webroot-path /var/www/acme \
  --ip-address "$ip" --preferred-profile shortlived --cert-name naverwebtoon-backend \
  --agree-tos --register-unsafely-without-email --non-interactive --keep-until-expiring

if [[ "$(serial)" == "$previous" && -f "$site" ]]; then
  exit 0
fi

cat > "$site" <<NGINX
server {
    listen 443 ssl;
    server_name $ip;
    ssl_certificate $cert/fullchain.pem;
    ssl_certificate_key $cert/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;

    location = /healthz {
        limit_except GET { deny all; }
        proxy_pass http://127.0.0.1:8080;
        proxy_connect_timeout 2s;
        proxy_read_timeout 5s;
        proxy_set_header Host localhost;
        proxy_set_header Cookie "";
        proxy_set_header Authorization "";
    }
    location / { return 404; }
}
NGINX
nginx -t
systemctl reload nginx
