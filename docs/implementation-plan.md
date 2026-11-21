# 구현 계획 (FastAPI 백엔드)

기준 문서: `docs/api.md` (API 명세 v0.3). 명세와 다르거나 불명확한 점은 `docs/open-questions.md`에 기록한다.

## 전제

- 기존 저장소는 Go `/healthz` 서버와 Go 바이너리 배포 파이프라인(`.github/workflows`, `ops/`)이다.
  이번 작업은 Python/FastAPI로 새로 작성하며, **기존 Go 코드와 배포 스크립트는 수정하지 않는다**.
  배포 파이프라인 전환(Docker 기반)은 별도 작업으로 남긴다 (open-questions 참고).
- 데이터 모델은 명세 §12 전체를 처음부터 하나의 Alembic 초기 마이그레이션으로 만든다
  (마일스톤마다 테이블을 쪼개 추가하면 `alembic check` 관리만 복잡해지기 때문).
- 외부 서비스(Object Storage, CLOVA OCR/Studio/Dubbing)는 인터페이스 뒤에 두고 Fake 구현으로 테스트한다.

## M0 기반

- [ ] 프로젝트 구조, `pyproject.toml`(ruff/pytest 설정), `requirements.in` / `requirements.lock`
- [ ] `app/config.py` (pydantic-settings), `app/logging.py` (JSON 로그 + request id), `app/errors.py` (§1 오류 형식)
- [ ] `/healthz` (`{"status":"ok","revision"}`, `Cache-Control: no-store`), `/readyz` (DB 불가 시 503)
- [ ] `APP_ENV=production`이면 `/docs`, `/openapi.json` 비활성화, CORS는 `CORS_ALLOWED_ORIGINS`만
- [ ] SQLAlchemy 2.x 모델 전체 + Alembic 초기 마이그레이션 (CHECK 제약, FK, 인덱스, 진행 중 job 부분 유니크 인덱스)
- [ ] 테스트 기반: 실제 PostgreSQL(`TEST_DATABASE_URL`), 테스트마다 TRUNCATE, Fake 어댑터 주입

## M1 인증·작품·회차·업로드 (§3, §4.1~4.4)

- [ ] 회원가입(초대 코드 → author), 로그인(argon2, 시도 제한), refresh 회전(해시 저장, 재사용 감지), 로그아웃, `GET /v1/me`
- [ ] 작품 CRUD + 표지 presign/complete, 장르·연재 상태 검증
- [ ] 회차 CRUD (삭제는 게시 전만), 커서 페이지네이션
- [ ] 이미지 presign(형식·크기·장수 검증) → complete(HEAD 존재·크기·형식 + 매직바이트 + 가로세로 추출)
- [ ] 이미지 목록·순서 변경·삭제(관련 대사·큐 삭제), 소유권 검사(없으면 404, 남의 것이면 403)
- [ ] `Idempotency-Key` (생성·재생성 계열 POST)
- [ ] 테스트: 엔드포인트별 성공·실패

## M2 파이프라인 (§5)

- [ ] `POST /process` (허용 상태 `uploaded`/`failed`, `from_step`, 진행 중이면 409 `JOB_IN_PROGRESS`)
- [ ] 워커: `SELECT ... FOR UPDATE SKIP LOCKED`로 job claim, heartbeat 갱신, heartbeat 만료 job 회수
- [ ] 단계: `ocr` → `structure` → `dubbing` → `build_cues` → `done`, 단계별 결과 저장 후 `failed` 재시도 시 다음 단계부터 재개
- [ ] 상태 전이: `uploaded → analyzing → synthesizing → needs_review`, 복구 불가 실패는 `failed`
- [ ] 일부 대사 음성 실패는 `audio_status=failed` 로 남기고 회차는 `needs_review`
- [ ] 음원 캐시: `sha256(text + voice_id + voice_params + emotion)`, `audio_assets.hash` UNIQUE
- [ ] `GET /v1/jobs/{id}` (진행률·ETA), `GET /episodes/{id}/jobs`, `POST /jobs/{id}/cancel`
- [ ] 테스트: 상태 전이 표, 재개, 부분 실패, 취소, heartbeat 회수

## M3 스튜디오·게시·독자 (§4.5, §4.6, §6, §8, §9.1~9.3)

- [ ] 이미지 검수 표시·alt (편집 가능 상태만)
- [ ] 스튜디오 일괄 조회(`?image_id=`), lines CRUD + 순서, characters CRUD + 병합, sfx-cues CRUD
- [ ] stale 규칙(텍스트·화자·감정·종류 변경, 인물 목소리 변경, 병합), `flags` 계산
- [ ] 집계 컬럼(검수 수, 문제 장면 수와 플래그별 수, 음성 길이)을 변경 시점에 갱신, ETA만 조회 시 계산
- [ ] 대시보드 (`month` Asia/Seoul 월 경계, `published_this_month`), 회차 목록 `?include=stats`
- [ ] voices / sfx-presets
- [ ] publish (stale·generating 검사, `REVIEW_INCOMPLETE`, `allow_unreviewed`, 예약 게시, 스냅샷 `playback.version`), unpublish
- [ ] 예약 게시 스케줄러(워커가 30초 주기로 폴링)
- [ ] 공개 API(비로그인): 작품 목록·상세·회차 목록, 회차 + playback, 음원 재서명 302, transcript
- [ ] 미리듣기 `GET /lines/{id}/audio` (302), `GET /episodes/{id}/preview`
- [ ] 테스트: stale/통계 규칙 표, 게시 조건, 스냅샷 격리

## M4 재생성·쿼터·설정·진행 (§7, §9.4~9.5)

- [ ] `POST /episodes/{id}/regenerate` (`stale`/`lines`/`all`), 대상 대사 `generating`
- [ ] `POST /lines/{id}/regenerate` (캐시 적중 200, 아니면 202 + job, `force`)
- [ ] 작업 중 수정 시 결과 폐기(입력 해시 비교 후 `stale` 유지)
- [ ] 쿼터: `usage_counters`(월 단위), `GET /v1/me/quota`, 초과 시 429 `QUOTA_EXCEEDED`
- [ ] `GET/PATCH /v1/me/settings`, `PUT/GET /v1/me/progress/{episode_id}`
- [ ] 구조화 로깅 (토큰·비밀번호·서명 URL 쿼리 미기록)

## 마무리

- [ ] `Dockerfile` (멀티스테이지, 비루트, `REVISION` 빌드 인자), `docker-compose.yml` (db / migrate / api / worker, `nw_owner`·`nw_app` 역할)
- [ ] `.env.example`, `README.md` (로컬 실행, 환경변수 표, 구현 현황)
- [ ] `scripts/export_openapi.py` → `docs/openapi.json`
- [ ] E2E 테스트: 가입 → 작품·회차 → 업로드 → process → 스튜디오 수정 → 재생성 → 게시 → 독자 조회
