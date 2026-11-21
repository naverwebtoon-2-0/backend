# 열린 질문과 구현 결정

`docs/api.md`(v0.3)와 다르거나 명세에 없어 구현에서 정한 내용이다. 원칙적으로 **가장 보수적인 쪽**으로 구현했다.
팀에서 확정하면 이 문서와 코드를 함께 고친다.

## A. 저장소·배포

| # | 내용 | 현재 구현 | 확인 필요 |
|---|---|---|---|
| A1 | 원격 저장소는 Go `/healthz` 서버이고, CI/CD(`.github/workflows/backend.yml`, `ops/`)는 서명한 Go 바이너리를 systemd로 배포한다 | Python/FastAPI로 새로 작성했다. Go 코드·워크플로·`ops/`는 **수정하지 않았다**. 그래서 현재 CI는 여전히 Go 빌드를 기준으로 동작한다 | Docker 기반 배포로 바꾸려면 워크플로와 `ops/`(특히 `backend.service`의 `IPAddressAllow=localhost`, `MemoryMax=256M`는 외부 CLOVA·DB 호출을 막는다)를 새로 설계해야 한다. Go 코드 삭제 여부도 정해야 한다 |
| A2 | 프런트 프록시의 `/api` 접두어 | 서버 경로는 `/v1/...` 고정. 프런트 `api.js`가 `/api/v1/*`를 `/v1/*`로 넘긴다고 가정 | 프록시 규칙 확정 |

## B. 응답 형식 (프런트 mock과의 차이 포함)

| # | 내용 | 현재 구현 |
|---|---|---|
| B1 | `GET /v1/voices`, `/v1/sfx-presets`의 응답 형태가 명세에 없다 | mock과 같이 `{ "items": [...], "next_cursor": null }` |
| B2 | 이미지 목록, 대사 목록, 인물 목록의 응답 형태 | 페이지 형식 `{items, next_cursor}`. 회차 단위 목록은 한 번에 모두 준다(`next_cursor: null`) |
| B3 | `POST /publish`, `/unpublish` 응답이 명세에 없다 | mock처럼 회차 객체를 돌려준다. 회차 응답에 `playback_version`(현재 독자 스냅샷 버전)을 추가했다 |
| B4 | `POST /images/complete` 응답 | `{ "episode": { "id", "state", "image_count" } }` (명세 예시에 `id` 추가) |
| B5 | 회차 응답에 `created_at`, 대사 응답에 `original_text`(OCR 원문), `ocr_confidence`를 추가했다 | mock의 "원문으로 되돌리기" 제안 필드(`original_text`)를 채운다 |
| B6 | `POST /process`의 `job` | `{id, kind, status, step}` (`kind` 추가) |
| B7 | 공개 회차 응답에 `number`, 공개 작품 응답에 `author.nickname`, 공개 회차 목록에 `image_count`·`audio_duration_ms`를 추가했다 | |
| B8 | 시각 형식 | 모든 응답을 ISO 8601 UTC `...Z`로 통일했다. DB 세션 시간대도 UTC로 고정했다 |
| B9 | `GET /studio?image_id=` | 대사·효과음 큐만 그 이미지로 거르고, `images`와 `characters`는 전체를 준다 (장면 목록을 그리는 데 필요) |

## C. 오류 코드 (명세 표에 없는 코드)

| HTTP | code | 상황 |
|---|---|---|
| 409 | `EMAIL_ALREADY_EXISTS` | 같은 이메일로 가입 |
| 429 | `TOO_MANY_ATTEMPTS` | 로그인 시도 제한 (기본 5분에 10회, `Retry-After` 헤더). 명세의 429는 `QUOTA_EXCEEDED`만 있다 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 같은 `Idempotency-Key`로 다른 요청 본문 |
| 409 | `JOB_NOT_CANCELABLE` | 이미 끝난 작업 취소 |
| 409 | `CHARACTER_IN_USE` | 대사가 있는 인물 삭제 |
| 500 | `INTERNAL_ERROR` | 처리하지 못한 서버 오류 |
| (job.error) | `CANCELED`, `WORKER_LOST`, `QUOTA_EXCEEDED`, `AI_UPSTREAM_ERROR`, `INTERNAL_ERROR` | 작업 실패 원인 (`GET /jobs/{id}`의 `error.code`) |

## D. 인증·계정

| # | 내용 | 현재 구현 |
|---|---|---|
| D1 | 틀린 `invite_code`를 보냈을 때 | **400 VALIDATION_ERROR** (조용히 독자로 가입시키지 않는다). 코드를 보내지 않으면 독자 |
| D2 | 비밀번호 정책 | 8~128자 (`PASSWORD_MIN_LENGTH`) |
| D3 | refresh 토큰 | 30일 만료. 이미 교체된 토큰이 다시 쓰이면 탈취로 보고 그 사용자의 refresh 토큰을 모두 폐기한다 |
| D4 | 남의 리소스 | 없으면 404, 있지만 남의 것이면 403 (명세 §1). 독자 계정이 작가 API를 부르면 403 |
| D5 | 로그인 시도 제한은 프로세스 메모리 기반이다 | API 인스턴스가 여러 개면 인스턴스별로 적용된다 |

## E. 상태·게시 규칙

| # | 내용 | 현재 구현 |
|---|---|---|
| E1 | 예약 게시의 스냅샷 시점 | **예약 요청 시점**에 검사하고 스냅샷을 저장한다. 예약 시각에 스케줄러는 그 스냅샷을 공개만 한다. 예약 후 수정하면 `has_unpublished_changes=true`가 되며, 반영하려면 게시 취소 후 다시 게시한다 (`publish` 허용 상태에 `scheduled`가 없으므로) |
| E2 | 게시 중(`published`)인 회차에 미래 `publish_at`을 보내면 | 409로 거부한다. 예약 상태로 바꾸면 독자가 그 회차를 볼 수 없게 되기 때문이다 |
| E3 | `audio_failed` 대사가 남아 있을 때 게시 | 명세 조건(`stale`·`generating` 없음)만 검사하므로 게시는 된다. 해당 큐는 `audio_url: null`이고 텍스트는 남는다 |
| E4 | 게시 취소 후 공개 범위 | `needs_review`로 돌아가 독자 API에서 404. 스냅샷 기록은 남는다 |
| E5 | 회차 삭제 "게시 전만" | `published_at`이 한 번이라도 기록된 회차는 게시 취소 후에도 삭제할 수 없다 |
| E6 | 진행 중인 `process` 취소 | 작업은 `canceled`, 회차는 진행분이 있으면 `failed`, 없으면 `uploaded`(이어서 실행 가능). 생성 중이던 대사는 `stale` |
| E7 | 이미지 추가 업로드 가능 상태 | `draft`, `uploaded`, `failed`. 이미지가 바뀌면 이전 파이프라인 진행분은 이어 쓰지 않는다(이미 OCR한 이미지 결과는 재사용) |
| E8 | 이미지 순서 변경 가능 상태 | 처리 중(`analyzing`·`synthesizing`)이 아니고 진행 중 작업이 없을 때 |
| E9 | 효과음 큐·`alt`·이미지 순서 변경도 게시 후 수정으로 보고 `has_unpublished_changes=true` | 검수 표시(`reviewed`)만 바꾸는 것은 제외 |
| E10 | 회차 번호(`number`) 중복 | 유니크 제약을 두지 않았다 (순서 바꾸기 편의). 필요하면 추가 |
| E11 | 작품 삭제 | 회차·이미지 등을 함께 삭제한다(진행 중 작업이 있으면 409). Object Storage 객체는 지우지 않는다 |

## F. 스튜디오·재생성

| # | 내용 | 현재 구현 |
|---|---|---|
| F1 | `flags`에 `audio_stale` 포함 여부 | 명세는 `flags`를 `low_ocr_confidence, speaker_unknown, audio_failed`로 적었지만, 문제 장면 수(`issue_count`)가 `audio_stale`을 항상 세야 하므로 `flags`에도 `audio_stale`을 넣었다 |
| F2 | `low_ocr_confidence` 판정 | OCR 신뢰도 < 0.8 (`LOW_OCR_CONFIDENCE_THRESHOLD`). `ignore` 종류는 판정하지 않는다. 수동 추가 대사는 신뢰도가 없어 판정하지 않는다 |
| F3 | `speaker_unknown` 판정 | `dialogue`이면서 `character_id`가 없을 때 |
| F4 | `regenerate scope=stale` 대상 | `stale`과 함께 `failed` 대사도 포함한다 (명세 §5: 음성 실패는 작가가 재생성으로 다시 시도) |
| F5 | 작업 중 수정 | 수정 즉시 `stale`로 바꾸고, 작업이 끝날 때 입력 해시가 다르면 결과를 버린다 |
| F6 | `force: true` 재생성 | 같은 해시의 캐시 항목을 새 음원으로 교체한다. 같은 입력을 공유하는 다른 대사도 새 음원을 쓰게 된다. 게시 스냅샷은 object key를 따로 저장하므로 영향이 없다 |
| F7 | 인물 목소리 변경 시 stale 대상 | 그 인물의 `ready`·`failed` 대사. 나레이션 인물이면 화자 미지정 `narration`·`description`도 포함한다 |
| F8 | 화자가 없는 `dialogue`의 목소리 | 기본 인물 목소리(`dain`)로 합성한다 |
| F9 | 대사 순서(`PUT lines/order`) | 보낸 순서대로 `order`를 1부터 매긴다. 같은 `y_ratio`끼리인지는 검사하지 않는다 (정렬은 `(image_index, y_ratio, order)`) |
| F10 | 같은 위치의 대사와 효과음 | 효과음 큐의 `order`를 0으로 두어 같은 `y_ratio`면 효과음이 먼저 온다 |
| F11 | 공개 큐의 `speaker` | 화자가 지정된 대사만 인물 이름. 대본(transcript)에서는 명세 예시처럼 `dialogue`만 `speaker`를 채운다 |
| F12 | 작품 생성 시 기본 나레이션 인물(`나레이션`, 목소리 `nara`)을 자동으로 만든다 | narration·description 기본 화자 |

## G. 외부 연동·운영

| # | 내용 | 현재 구현 |
|---|---|---|
| G1 | CLOVA Dubbing 실제 사양 | `ClovaDubbingClient`는 스켈레톤이다(호출 시 `AI_UPSTREAM_ERROR`). `AI_BACKEND=clova`로 운영하려면 사양 확인 후 구현해야 한다 |
| G2 | CLOVA OCR / Studio | OCR General V2, Studio Chat Completions v3(HCX-005) 형식으로 구현했지만 **실제 키로 검증하지 않았다** |
| G3 | 서명 URL 만료 | 원고 이미지·작가용 음원(스튜디오 §6.1 "1시간") 1시간, 독자 음원 6시간. 게시된 회차의 이미지도 1시간 서명 URL (CDN 미정) |
| G4 | 쿼터 | 월(UTC) 단위, 작품 소유 작가에게 집계. OCR은 이미지 1장당 1, Dubbing은 캐시 미적중 글자 수. 파이프라인 중 Dubbing 쿼터가 바닥나면 해당 대사를 `audio_failed`로 남긴다 |
| G5 | 개발용 가짜 저장소 | `STORAGE_BACKEND=fake`이고 운영이 아닐 때만 `/_fake-storage/*` 경로를 연다 (HMAC 서명 검사) |
| G6 | 예약 게시 스케줄러 | 워커 안에서 30초 주기로 폴링 (`SCHEDULER_INTERVAL_SECONDS`). 실패하면 다음 주기에 다시 시도하고 로그를 남긴다 |
| G7 | 워커 장애 | heartbeat가 120초 끊긴 작업은 다시 대기열에 넣고, 3회를 넘으면 `WORKER_LOST`로 실패 처리 |
| G8 | Docker 실행은 이 환경에 Docker가 없어 **검증하지 못했다** | `Dockerfile`, `docker-compose.yml`은 작성만 했다. 역할 초기화 SQL·DML 전용 권한·마이그레이션은 로컬 PostgreSQL 16에서 확인했다 |

## H. OpenAPI와 api.md의 차이

`docs/openapi.json`은 `scripts/export_openapi.py`로 생성한다(테스트가 최신 여부를 검사한다). api.md 대비 차이는 위 B·C 항목과 같고,
그 밖에 `/healthz`, `/readyz`, 개발용 `/_fake-storage/*`는 스키마에서 제외했다. §10 번역(확장)은 구현 범위 밖이다.
