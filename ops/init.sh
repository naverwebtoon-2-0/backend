set -euo pipefail
umask 022
export DEBIAN_FRONTEND=noninteractive

apt-get -o Acquire::Retries=3 update
apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    ca-certificates curl git jq nginx-common openssl snapd

install -d -m 0755 /opt/naverwebtoon-backend/releases /etc/naverwebtoon-backend /var/www/acme
# IPv6가 비활성화된 인스턴스에서 Nginx 패키지를 설치하기 전에 IPv4 사이트를 설정한다.
if [[ -L /etc/nginx/sites-enabled/default ]] &&
    [[ "$(readlink -f /etc/nginx/sites-enabled/default)" == /etc/nginx/sites-available/default ]]; then
    unlink /etc/nginx/sites-enabled/default
fi
cat > /etc/nginx/conf.d/naverwebtoon-backend-acme.conf <<'NGINX'
server {
    listen 80 default_server;
    server_name _;
    server_tokens off;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/acme;
        try_files $uri =404;
    }
    location / { return 404; }
}
NGINX

apt-get -o Acquire::Retries=3 install -y --no-install-recommends nginx
snap install certbot --classic
# 인증서 갱신은 새 인증서를 로드하는 프로젝트 타이머에서 처리한다.
systemctl disable --now snap.certbot.renew.timer || true
