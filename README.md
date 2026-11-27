# naverwebtoon 2.0 백엔드

작가가 웹툰 원고를 올리면 AI(네이버 클라우드 CLOVA)가 인물 음성과 효과음을 입히고, 독자는 보면서 듣는 몰입형 웹툰 서비스의 API 서버.

- API 명세: [`docs/api.md`](docs/api.md) (v0.3, 기준 문서)
- 구현 계획: [`docs/implementation-plan.md`](docs/implementation-plan.md)
- 명세와 다른 점·열린 질문: [`docs/open-questions.md`](docs/open-questions.md)
- OpenAPI: [`docs/openapi.json`](docs/openapi.json) (`python scripts/export_openapi.py`로 생성)

## 기술 스택

Python 3.12 · FastAPI · Uvicorn · PostgreSQL 16 · SQLAlchemy 2.x · psycopg 3 · Alembic · pydantic-settings ·
argon2 · JWT · boto3(Naver Object Storage) · pytest · ruff. 작업 큐는 별도 브로커 없이 Postgres `jobs` 테이블을
`SELECT ... FOR UPDATE SKIP LOCKED`로 폴링하는 워커가 처리한다.

```
app/
  main.py            create_app, /healthz, /readyz, CORS, 예외 핸들러
  config.py db.py security.py ids.py errors.py logging.py pagination.py idempotency.py
  models/            SQLAlchemy 모델
  schemas/           요청·응답 Pydantic 모델
  api/v1/            auth me series episodes images jobs studio characters catalog publish regenerate public
  services/          상태 규칙·집계, 게시 스냅샷, 쿼터, 업로드 검증, 음원 캐시
  integrations/      storage ocr structure dubbing (인터페이스 + CLOVA 구현 + Fake 구현)
  worker/            runner(폴링 루프) pipeline(ocr→structure→dubbing→build_cues) scheduler(예약 게시)
alembic/  tests/  docker/  scripts/  docs/
```

## 로컬 실행

### Docker Compose

```sh
cp .env.example .env        # 비밀번호·JWT_SECRET 수정
docker compose up --build   # db → migrate(1회) → api, worker
curl http://127.0.0.1:8080/healthz
```

- `db`: postgres:16 (UTF-8, `C.UTF-8`, 볼륨 `pgdata`). 최초 기동 시 `docker/db/init`이 `nw_owner`(스키마 소유, DDL)와 `nw_app`(DML만) 역할을 만든다
- `migrate`: `nw_owner`로 `alembic upgrade head`를 1회 실행
- `api`: `nw_app`, `127.0.0.1:8080`에만 노출
- `worker`: 같은 이미지, `python -m app.worker.runner`
- 이미지 빌드 시 `REVISION` 인자가 `/healthz`의 `revision`이 된다: `REVISION=$(git rev-parse HEAD) docker compose up --build`

### 직접 실행 (개발)

```sh
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/naverwebtoon
alembic upgrade head
uvicorn app.main:app --port 8080                 # API
python -m app.worker.runner                      # 워커 (다른 터미널)
```

기본값(`AI_BACKEND=fake`, `STORAGE_BACKEND=fake`)이면 외부 키 없이 전체 흐름이 동작한다. 가짜 저장소는
`FAKE_STORAGE_DIR`에 파일을 두고 `/_fake-storage/*` 경로가 presigned URL 역할을 한다(운영에서는 비활성).

### 테스트·린트

테스트는 **실제 PostgreSQL**을 쓴다. 테스트 DB의 `public` 스키마를 지우고 다시 만드므로 전용 DB를 지정한다.

```sh
export TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/nw_test
pytest
ruff check .
alembic upgrade head && alembic check
python scripts/export_openapi.py --check
```

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `APP_ENV` | `development` | `production`이면 `JWT_SECRET` 검증 |
| `PRODUCTION_DOCS_ENABLED` | `false` (운영 Compose는 `true`) | 운영 Swagger `/v1/docs`와 명세 `/v1/openapi.json` 활성화 |
| `REVISION` | `local` | `/healthz` 응답의 배포 리비전 (Docker 빌드 인자로 주입) |
| `LOG_LEVEL` | `INFO` | JSON 로그 레벨 |
| `DATABASE_URL` | `postgresql+psycopg://nw_app:...@127.0.0.1:5432/naverwebtoon` | 앱 DB 접속 (DML 역할) |
| `MIGRATION_DATABASE_URL` | (없음) | Alembic 전용 접속. 없으면 `DATABASE_URL` |
| `CORS_ALLOWED_ORIGINS` | (없음) | 허용 origin, 쉼표 구분. 쿠키 미사용 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8080` | 가짜 저장소 URL 생성용 |
| `JWT_SECRET` | 개발용 값 | access 토큰 서명 키. 운영은 32자 이상 필수 |
| `ACCESS_TOKEN_TTL_SECONDS` | `3600` | access 토큰 유효 시간 |
| `REFRESH_TOKEN_TTL_DAYS` | `30` | refresh 토큰 유효 기간 (회전식, DB에는 해시만) |
| `PASSWORD_MIN_LENGTH` | `8` | 비밀번호 최소 길이 |
| `AUTHOR_INVITE_CODES` | (없음) | 작가 가입 초대 코드, 쉼표 구분 |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `10` / `300` | 로그인 시도 제한 |
| `AI_BACKEND` | `fake` | `fake` 또는 `clova` |
| `STORAGE_BACKEND` | `fake` | `fake` 또는 `s3`(Naver Object Storage) |
| `STORAGE_ENDPOINT` / `STORAGE_REGION` | `https://kr.object.ncloudstorage.com` / `kr-standard` | Object Storage |
| `STORAGE_BUCKET` / `STORAGE_ACCESS_KEY` / `STORAGE_SECRET_KEY` | | Object Storage 버킷·키 |
| `FAKE_STORAGE_DIR` | `.fake-storage` | 가짜 저장소 디렉터리 (api·worker 공유) |
| `UPLOAD_URL_TTL_SECONDS` | `600` | presigned PUT 유효 시간 |
| `IMAGE_URL_TTL_SECONDS` | `3600` | 원고 이미지·작가용 음원 서명 URL |
| `AUDIO_URL_TTL_SECONDS` | `21600` | 독자용 음원 서명 URL |
| `MAX_IMAGE_BYTES` / `MAX_IMAGES_PER_EPISODE` | `20971520` / `100` | 업로드 제한 (PNG·JPEG·WebP) |
| `CLOVA_OCR_INVOKE_URL` / `CLOVA_OCR_SECRET` | | CLOVA OCR |
| `CLOVA_STUDIO_URL` / `CLOVA_STUDIO_API_KEY` | HCX-005 chat completions | CLOVA Studio |
| `CLOVA_VOICE_URL` | `https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts` | CLOVA Voice Premium TTS |
| `CLOVA_VOICE_CLIENT_ID` / `CLOVA_VOICE_CLIENT_SECRET` | | CLOVA Voice 키 (NCP Application의 Client ID·Secret) |
| `SFX_PRESET_PREFIX` | `sfx/presets` | 효과음 프리셋 음원 위치 (`{prefix}/{tag}.wav`) |
| `AI_TIMEOUT_SECONDS` / `AI_MAX_RETRIES` | `30` / `3` | 외부 호출 타임아웃·지수 백오프 재시도 |
| `LOW_OCR_CONFIDENCE_THRESHOLD` | `0.8` | 문제 장면 판정 기준 |
| `QUOTA_DUBBING_CHARS` / `QUOTA_OCR_IMAGES` | `100000` / `500` | 월 쿼터 (초과 시 429) |
| `WORKER_POLL_SECONDS` | `1.0` | 워커 폴링 간격 |
| `SCHEDULER_INTERVAL_SECONDS` | `30` | 예약 게시 확인 주기 |
| `JOB_HEARTBEAT_TIMEOUT_SECONDS` / `JOB_MAX_ATTEMPTS` | `120` / `3` | 비정상 종료 작업 회수 |

## 구현 현황

| 마일스톤 | 범위 | 상태 |
|---|---|---|
| M0 | `/healthz`(revision, no-store), `/readyz`(DB 불가 시 503), 설정·로깅·오류 형식, 전체 데이터 모델·마이그레이션 | 완료 |
| M1 | §3 인증(argon2, JWT, 회전식 refresh, 시도 제한), §4.1~4.4 작품·표지·회차·이미지 presign/complete·순서·삭제, 커서 페이지네이션, Idempotency-Key | 완료 |
| M2 | §5 파이프라인(process·jobs·cancel), 워커(SKIP LOCKED, heartbeat 회수), 단계별 재개, 부분 음성 실패, 음원 캐시 | 완료 |
| M3 | §4.5 검수·alt, §4.6 대시보드, §6 스튜디오·대사·인물·효과음·미리듣기, §8 게시·예약 게시·스냅샷, §9.1~9.3 공개 API·대본 | 완료 |
| M4 | §7 재생성(stale/lines/all, 라인 단건, 작업 중 수정 폐기), 쿼터, §9.4~9.5 독자 설정·진행 위치, 구조화 로깅 | 완료 |
| 확장 | §10 Papago 번역 | 미구현 (범위 밖) |

API 키를 넣기 전까지는 `AI_BACKEND=fake`, `STORAGE_BACKEND=fake`로 동작한다. 키를 넣는 방법은 아래 "운영 배포"를 참고한다.

## 외부 연동

| 기능 | 구현 | 필요한 값 |
|---|---|---|
| 원고 저장 | Naver Object Storage (S3 호환, presigned PUT/GET) | `STORAGE_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`, `STORAGE_BACKEND=s3` |
| 글자 인식 | CLOVA OCR General V2 | `CLOVA_OCR_INVOKE_URL`, `CLOVA_OCR_SECRET` |
| 대사·인물 정리 | CLOVA Studio Chat Completions v3 (HCX-005) | `CLOVA_STUDIO_API_KEY` |
| 음성 | CLOVA Voice Premium TTS (화자·속도·높낮이·감정·감정 강도, 200자 초과 문장은 나눠 합성 후 연결) | `CLOVA_VOICE_CLIENT_ID`, `CLOVA_VOICE_CLIENT_SECRET` |
| 효과음 | Object Storage의 `sfx/presets/{tag}.wav`, 없으면 내장 생성 음원 | (선택) 프리셋 음원 업로드 |

`AI_BACKEND=clova`로 바꾸면 위 클라이언트를 쓴다. CLOVA Dubbing은 웹 콘텐츠 제작 서비스라 실시간 API가 없어
음성은 NCP 안내대로 CLOVA Voice로 만든다 (`docs/open-questions.md` G1).

## 운영 배포 (네이버클라우드)

서버에 SSH나 GitHub 토큰 없이, 서버가 서명된 릴리스를 직접 받아 적용하는 방식이다.

```
push main → CI: 테스트 · docker build · docker compose up 검증(로컬용·운영용 compose)
          → release: 이미지(docker save)·배포 묶음(compose + DB 초기화)·manifest를 Ed25519로 서명해 GitHub Release 발행
서버 (2분마다, ops/update.sh): main 커밋 확인 → 서명·해시 검증 → docker load
          → migrate(run --rm) → docker compose up -d --wait → /healthz revision·/readyz 확인
          → 실패하면 직전 릴리스로 되돌림
```

1. 외부 연동 값과 `BACKEND_CONFIG_SSH_KEY`를 백엔드 저장소의 GitHub Actions Secrets에 등록한다.
2. `bash ops/render-init-script.sh > init-script.sh`로 Linux Init Script를 만든다. 외부 연동 키는 이 파일에 넣지 않는다.
3. Ubuntu 24.04 x86-64 서버(메모리 2GB 이상 권장: PostgreSQL + API + 워커)를 Public Subnet에 만들고 Init Script와 공인 IPv4를 지정한다.
   전용 ACG 인바운드는 TCP 80(ACME)과 443(HTTPS)만 허용한다.
4. Init Script가 Docker·Nginx·certbot을 설치하고 `/etc/naverwebtoon-backend/backend.env`(DB 비밀번호·JWT 키는 무작위 생성)를 만든다.
5. HTTPS가 열린 뒤 Actions 변수 `NCP_BACKEND_ORIGIN`을 설정하고 `main` 워크플로를 실행하면, 서버가 릴리스를 받아 `docker compose up`한다.

서버 구성: Nginx(443) → `127.0.0.1:8080`(api 컨테이너)로 `/healthz`, `/v1/*`, `/_fake-storage/*`만 전달한다. DB는 컨테이너 볼륨 `pgdata`에 저장된다.
서버에서 상태 확인: `journalctl -u naverwebtoon-backend-update.service`(배포), `journalctl -u naverwebtoon-backend-tls.service`(인증서),
`naverwebtoon-backend-compose ps` / `logs`(컨테이너).

키를 변경하면 GitHub Actions Secrets를 수정하고 main 워크플로를 실행한다. 운영 서버의 제한된 SSH 키는 연동 설정 적용 명령만 실행한다. 서버를 새로 만들면 이 명령과 공개 키를 다시 설치해야 한다.

남은 일: 실제 API 키를 넣은 뒤 CLOVA OCR·Studio·Voice 호출 확인 (요청 형식은 문서 기준으로 구현하고 가짜 HTTP로 테스트했다).
