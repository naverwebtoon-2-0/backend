# naverwebtoon 2.0 백엔드

Go 1.27.4에서 `go test ./...`와 `go run .`을 실행한다. 로컬 상태 API는
`http://127.0.0.1:8080/healthz`를 사용한다.

`main`, `dev` 및 두 브랜치 대상 PR에서 CI를 실행한다. `main` 배포 작업은
GitHub Actions `production` 환경의 `BACKEND_RELEASE_SIGNING_KEY`로 Linux amd64
바이너리의 매니페스트에 Ed25519 서명하고 GitHub Release를 발행한다. 배포 작업을
다시 실행하면 동일 커밋의 공개 릴리스를 재사용한다.

## 네이버클라우드

1. `bash ops/render-init-script.sh` 출력으로 Server의 Linux Init Script를 생성한다.
2. Ubuntu 24.04 x86-64 서버를 Public Subnet에 만들고, Init Script와 공인 IPv4를
   지정한다. 전용 ACG에서 인바운드는 TCP 80(ACME)과 443(HTTPS)을 허용한다.
   아웃바운드는 패키지 설치와 GitHub, ACME 통신에 필요하다.
3. HTTPS가 열린 뒤 양쪽 GitHub 저장소의 Actions 변수 `NCP_BACKEND_ORIGIN`을
   공인 IP 기반 HTTPS origin으로 설정하고 `main` 워크플로를 다시 실행한다.

인스턴스는 약 2분마다 GitHub `main`의 릴리스를 확인한다. 현재 `main` 커밋과
서명, 바이너리 해시를 검사한 뒤 서비스 상태가 정상이면 적용한다. 이상이 있으면
직전 릴리스로 복구한다. 인스턴스의 배포에는 SSH 또는 GitHub 접근 토큰이 필요 없다.

서버에서 배포 상태는 `journalctl -u naverwebtoon-backend-update.service`로,
IP 인증서 발급 및 갱신은 `journalctl -u naverwebtoon-backend-tls.service`로 확인한다.
운영 인증서는 최초 발급 전 80번 포트의 ACME 경로가 외부에서 접근 가능해야 한다.
