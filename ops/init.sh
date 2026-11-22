set -euo pipefail
umask 022
export DEBIAN_FRONTEND=noninteractive

apt-get -o Acquire::Retries=3 update
apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    ca-certificates curl git jq nginx-common openssl snapd docker.io docker-compose-v2

install -d -m 0755 /opt/naverwebtoon-backend/releases /var/www/acme
install -d -m 0700 /etc/naverwebtoon-backend
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
systemctl enable --now docker.service

# 서버 환경변수: 최초 1회 비밀번호·JWT 키를 무작위로 만든다. API 키는 비워 두고 override 파일로 넣는다.
env_file=/etc/naverwebtoon-backend/backend.env
if [[ ! -s "$env_file" ]]; then
    public_ip=""
    if token=$(curl -fsS --noproxy '*' --max-time 4 -X PUT http://169.254.169.254/latest/api/token \
        -H 'X-NCP-METADATA-TOKEN-TTL-SECONDS: 60'); then
        public_ip=$(curl -fsS --noproxy '*' --max-time 4 -H "X-NCP-METADATA-TOKEN: $token" \
            http://169.254.169.254/latest/meta-data/publicIp || true)
    fi
    umask 077
    cat > "$env_file" <<ENV
APP_ENV=production
LOG_LEVEL=INFO
POSTGRES_PASSWORD=$(openssl rand -hex 24)
NW_OWNER_PASSWORD=$(openssl rand -hex 24)
NW_APP_PASSWORD=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -hex 32)
AUTHOR_INVITE_CODES=$(openssl rand -hex 12)
PUBLIC_BASE_URL=https://${public_ip:-127.0.0.1}
CORS_ALLOWED_ORIGINS=
AI_BACKEND=fake
STORAGE_BACKEND=fake
STORAGE_ENDPOINT=https://kr.object.ncloudstorage.com
STORAGE_REGION=kr-standard
STORAGE_BUCKET=
STORAGE_ACCESS_KEY=
STORAGE_SECRET_KEY=
CLOVA_OCR_INVOKE_URL=
CLOVA_OCR_SECRET=
CLOVA_STUDIO_API_KEY=
CLOVA_VOICE_CLIENT_ID=
CLOVA_VOICE_CLIENT_SECRET=
ENV
    umask 022
fi
