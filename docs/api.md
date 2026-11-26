# naverwebtoon 2.0 백엔드 API 명세 (초안)

> 몰입형 웹툰 서비스: 작가가 원고를 올리면 AI가 인물 음성과 효과음을 입혀 주고, 독자는 웹툰을 보며 듣는다.
> 기준: 서비스 시나리오 슬라이드(p.9), `backend-design.md` (FastAPI / PostgreSQL / 네이버클라우드 AI)
> 상태: 초안 v0.3. 요청·응답 필드는 구현하며 조정한다.

## 0. 서비스 시나리오와 API 매핑

```
Object Storage → CLOVA OCR → CLOVA Studio → CLOVA Dubbing → 작가 스튜디오 → 게시 → 독자 뷰어 → (확장) Papago
 (업로드)         (글자 인식)   (인물·대사 정리)  (음성+효과음)    (검수·수정·재생성)  (공개)   (보기·듣기)     (다국어)
```

| 시나리오 단계 | 구현 주체 | 관련 API 그룹 |
|---|---|---|
| 업로드된 원고 저장 | 직접 구현 (Object Storage) | §4 이미지 업로드 |
| 말풍선 속 글자 인식 | CLOVA OCR | §5 분석 파이프라인 (내부 단계) |
| 인물·대사 정리 | CLOVA Studio (HyperCLOVA) | §5 분석 파이프라인 (내부 단계) |
| 나레이션·인물 음성 + 효과음 프리셋 | CLOVA Dubbing | §5 파이프라인, §7 재생성 |
| 작가 스튜디오 (검수·수정·재생성) | 직접 구현 | §4.5 이미지 검수 표시, §4.6 대시보드, §6 대사·인물, §7 재생성 |
| 게시 | 직접 구현 | §8 게시 |
| 독자 뷰어 (보기·듣기·키보드·스크린 리더) | 직접 구현 | §9 독자용 API |
| Papago 번역 (확장 계획) | 확장 | §10 번역 (확장) |

> 설계 변경 메모: 기존 설계 초안은 TTS를 CLOVA Voice, 효과음을 자체 SFX 라이브러리로 두었으나, 시나리오는 **CLOVA Dubbing이 음성과 효과음 프리셋을 함께 담당**하는 구성이다. 이 명세는 시나리오를 따르며, 작가 검수가 음성 생성 **뒤**에 오므로 에피소드 상태 순서도 바뀌었다(§2). Dubbing의 실제 API 사양·요금은 콘솔에서 확인이 필요하다.

---

## 1. 공통 규약

- Base URL: `https://{host}/v1` (프런트 프록시가 `/api` 접두어를 붙이는지는 미정 → 경로는 `/v1/...`로 고정하고 프록시 규칙만 맞춘다)
- 형식: `application/json; charset=utf-8`, 시간은 ISO 8601 UTC (`2026-11-21T05:00:00Z`)
- ID: 문자열 (UUID v7 권장)
- 인증: `Authorization: Bearer <access_token>`
  - 역할: `author`(작가), `reader`(독자). 독자용 조회 API 중 공개된 회차는 비로그인 허용
- CORS: 프런트(Vercel) 도메인만 허용하고(허용 origin은 환경변수), 인증은 Bearer 헤더만 사용한다(쿠키 미사용)
- 페이지네이션: `?limit=20&cursor=<opaque>` → `{ "items": [...], "next_cursor": "..." | null }`
- 멱등성: 생성·재생성 계열 `POST`는 `Idempotency-Key` 헤더 지원
- 좌표: 큐·말풍선 위치는 픽셀이 아닌 **상대 좌표(0~1)** 로 주고받는다
- 이름 규칙: 작가용 API는 `image_id`, 독자용 API는 `image_index`를 쓴다 (DB 컬럼명은 구현에서 맞춘다)
- 오류 형식

```json
{
  "error": {
    "code": "EPISODE_STATE_INVALID",
    "message": "현재 상태에서는 게시할 수 없습니다.",
    "details": { "state": "analyzing" }
  }
}
```

| HTTP | code 예시 | 설명 |
|---|---|---|
| 400 | `VALIDATION_ERROR` | 요청 값 오류 |
| 401 | `UNAUTHORIZED` | 토큰 없음/만료 |
| 403 | `FORBIDDEN` | 권한 없음 (남의 작품 등) |
| 404 | `NOT_FOUND` | 리소스 없음 |
| 409 | `EPISODE_STATE_INVALID` | 상태 전이 불가 |
| 409 | `JOB_IN_PROGRESS` | 같은 회차에 이미 진행 중인 작업이 있음 |
| 409 | `REVIEW_INCOMPLETE` | 검수 완료되지 않은 이미지가 있어 게시할 수 없음 |
| 413 | `FILE_TOO_LARGE` | 이미지 크기 초과 |
| 415 | `UNSUPPORTED_MEDIA` | 지원하지 않는 이미지 형식 |
| 429 | `QUOTA_EXCEEDED` | AI 사용 쿼터 초과 |
| 502 | `AI_UPSTREAM_ERROR` | CLOVA 호출 실패 |
| 502 | `STORAGE_UPSTREAM_ERROR` | 백엔드의 Object Storage 호출 실패 |

백엔드 오류의 `details`에는 원인과 요청 추적 ID가 포함된다. 외부 서비스 실패에는 `service`와 `upstream`도 함께 들어간다. `upstream.status`는 외부 서비스의 HTTP 상태이며, HTTP 200 응답 본문에서 실패를 보고하면 `upstream.code`와 `upstream.message`에 그 원인이 기록된다. 오류 코드나 추적 ID를 제공하지 않은 서비스의 해당 필드는 `null`이다. 비공개 URL과 인증 값은 가린다.

비동기 분석은 `GET /v1/jobs/{job_id}`의 `error.details`에서 확인한다. 일부 대사나 효과음만 실패한 경우 `warnings`에 남고, 대사 실패는 스튜디오 및 대사 조회의 `audio.error`에도 표시된다.

---

## 2. 에피소드 상태

```
draft → uploaded → analyzing → synthesizing → needs_review ⇄ scheduled → published
                                                    ↑                        │
                                                    └──── unpublish ─────────┘
 - needs_review → published (즉시 게시), published → published (수정 후 재게시)
 - 이어서 진행할 수 없는 실패는 failed (재시도하면 마지막 성공 단계부터 이어서 실행)
```

| 상태 | 의미 |
|---|---|
| `draft` | 회차만 만들어 둔 상태 (이미지 없음) |
| `uploaded` | 이미지 업로드 완료, 분석 전 |
| `analyzing` | OCR + CLOVA Studio로 대사·인물·감정 정리 중. 긴 원고는 OCR 요청에서 나눠 처리하고 원본 좌표로 합친다 |
| `synthesizing` | CLOVA Dubbing으로 음성·효과음 생성 중 |
| `needs_review` | 분석·음성 생성 완료. 작가 스튜디오에서 검수·수정 (일부 대사 재생성은 이 상태 안에서 진행) |
| `scheduled` | 예약 게시 대기 (`scheduled_at`에 스케줄러가 게시) |
| `published` | 독자에게 공개. 독자는 마지막 게시 스냅샷을 본다 |
| `failed` | OCR 전체 실패처럼 이어서 진행할 수 없는 파이프라인 실패 (`jobs`에 원인 기록). 일부 대사의 음성만 실패한 경우는 `failed`가 아니라 `needs_review` + 대사의 `audio_failed` 플래그 |

**편집 가능 상태**: `needs_review`, `scheduled`, `published`. 이 상태에서 스튜디오(§6)의 수정·재생성·검수 표시(§4.5)를 할 수 있다. `analyzing`·`synthesizing` 중에는 조회만 되고 수정은 `409 EPISODE_STATE_INVALID`.

---

## 3. 인증·계정

### POST `/v1/auth/signup`
```json
{ "email": "a@example.com", "password": "...", "nickname": "작가A", "invite_code": "..." }
```
→ `201` `{ "user": { "id", "email", "nickname", "role" } }`

`invite_code`가 유효하면 `author`, 없으면 `reader`로 가입된다 (`role`은 클라이언트가 지정하지 않는다).

### POST `/v1/auth/login`
```json
{ "email": "a@example.com", "password": "..." }
```
→ `200` `{ "access_token", "refresh_token", "expires_in": 3600 }`

### POST `/v1/auth/refresh`
`{ "refresh_token" }` → `{ "access_token", "refresh_token", "expires_in" }` (refresh 토큰은 사용할 때마다 새 값으로 교체되고 이전 값은 폐기된다)

### POST `/v1/auth/logout`
`{ "refresh_token" }` → `204`. 해당 refresh 토큰을 폐기한다

### GET `/v1/me`
→ `{ "id", "email", "nickname", "role", "settings": { "reader": {...} } }`

설정 조회·저장은 §9.4 `GET/PATCH /v1/me/settings`

---

## 4. 작품·회차·원고 업로드 (Object Storage)

### 4.1 작품 (series)

| 메서드 | 경로 | 설명 | 권한 |
|---|---|---|---|
| POST | `/v1/series` | 작품 생성 | author |
| GET | `/v1/series` | 내 작품 목록 | author |
| GET | `/v1/series/{series_id}` | 작품 상세 | author |
| PATCH | `/v1/series/{series_id}` | 제목·소개·장르·연재 상태 수정 | author(본인) |
| POST | `/v1/series/{series_id}/cover/presign` | 표지 업로드 URL 발급 (§4.3과 같은 방식, 파일 1개) | author(본인) |
| POST | `/v1/series/{series_id}/cover/complete` | 표지 업로드 확정 | author(본인) |
| DELETE | `/v1/series/{series_id}` | 삭제 | author(본인) |

```json
// POST /v1/series
{ "title": "제목", "description": "소개", "language": "ko", "genre": "fantasy", "serial_status": "ongoing" }
```

작품 응답 예시

```json
{
  "id": "sr_01",
  "title": "제목",
  "description": "소개",
  "language": "ko",
  "genre": "fantasy",
  "serial_status": "ongoing",
  "cover_url": null,
  "episode_count": 12,
  "created_at": "2026-11-01T05:00:00Z",
  "updated_at": "2026-11-21T05:00:00Z"
}
```

- `genre` (생성 시 필수, 문자열 코드): 아래 표 중 하나. 장르를 여러 개 지정해야 하면 `genres: []` 배열로 확장 (현재는 단일 값)
- `serial_status` (기본 `ongoing`): 연재 상태. `ongoing`(연재 중) / `completed`(완결) / `hiatus`(휴재)
- 두 필드 모두 `PATCH /v1/series/{series_id}`로 수정 가능하며, 공개 작품 목록에서 필터로 쓸 수 있다 (§9.1)

| genre 코드 | 표시 |
|---|---|
| `romance` | 로맨스 |
| `fantasy` | 판타지 |
| `action` | 액션 |
| `drama` | 드라마 |
| `comedy` | 코미디 |
| `thriller` | 스릴러 |
| `daily` | 일상 |
| `sports` | 스포츠 |
| `historical` | 시대극 |
| `etc` | 기타 |

> 장르 목록은 초안이며 팀에서 확정한다. 코드값은 서버가 검증하고, 표시 이름은 프런트가 매핑한다.

### 4.2 회차 (episodes)

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/series/{series_id}/episodes` | 회차 생성 (`state: "uploaded"` 이전의 빈 회차, `draft`) |
| GET | `/v1/series/{series_id}/episodes` | 회차 목록 (작가용, 상태 포함). `?include=stats`로 검수·처리 통계 포함 (§4.6) |
| GET | `/v1/episodes/{episode_id}` | 회차 상세 |
| PATCH | `/v1/episodes/{episode_id}` | 제목·순서 수정 |
| DELETE | `/v1/episodes/{episode_id}` | 삭제 (게시 전만) |

회차 응답 예시

```json
{
  "id": "ep_01",
  "series_id": "sr_01",
  "number": 1,
  "title": "1화",
  "state": "needs_review",
  "image_count": 12,
  "published_at": null,
  "scheduled_at": null,
  "has_unpublished_changes": false,
  "updated_at": "2026-11-21T05:00:00Z",
  "stats": {
    "reviewed_image_count": 5,
    "review_progress": 0.42,
    "issue_count": 3,
    "audio_duration_ms": 482000,
    "processing": null
  }
}
```

- `published_at`: **최초 게시 시각**(UTC). 게시 전에는 `null`. 게시 취소·재게시를 해도 값은 유지된다 (§8). "이번 달 게시 수" 계산에 사용하며, 더 이상 `updated_at`으로 추정하지 않는다
- `scheduled_at`: 예약 게시 시각. 예약이 없으면 `null`
- `has_unpublished_changes`: 게시된 회차를 게시 후에 수정했으면 `true`. 독자에게는 반영되지 않았고, `publish`를 다시 호출하면 `false`가 된다 (§8)
- `stats`: 작가용 응답에서 `?include=stats`를 붙이거나 대시보드(§4.6)로 조회할 때만 포함된다. 필드 정의는 §4.6 참고

### 4.3 원고 이미지 업로드 (presigned URL)

업로드는 서버를 거치지 않고 Object Storage(`https://kr.object.ncloudstorage.com`, S3 호환)로 직접 올린다.

**① 업로드 URL 발급** — `POST /v1/episodes/{episode_id}/images/presign`
```json
{
  "files": [
    { "filename": "001.png", "content_type": "image/png", "size": 2481923 },
    { "filename": "002.png", "content_type": "image/png", "size": 2390012 }
  ]
}
```
→ `200`
```json
{
  "uploads": [
    {
      "image_id": "img_01",
      "index": 0,
      "method": "PUT",
      "url": "https://kr.object.ncloudstorage.com/...signed...",
      "headers": { "Content-Type": "image/png" },
      "expires_in": 600
    }
  ]
}
```

**② 클라이언트가 `url`로 파일 PUT**

**③ 업로드 완료 확정** — `POST /v1/episodes/{episode_id}/images/complete`
```json
{ "image_ids": ["img_01", "img_02"] }
```
→ `200` `{ "episode": { "state": "uploaded", "image_count": 2 } }` (서버가 객체 존재·크기·형식을 검증)

### 4.4 이미지 관리

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/episodes/{episode_id}/images` | 이미지 목록 (`index`, `width`, `height`, `url`, `alt`, `reviewed`, `reviewed_at`) |
| PUT | `/v1/episodes/{episode_id}/images/order` | 순서 변경 `{ "image_ids": [...] }` |
| PATCH | `/v1/episodes/{episode_id}/images/{image_id}` | 검수 완료 표시·대체 텍스트(`alt`) 수정 (§4.5) |
| DELETE | `/v1/episodes/{episode_id}/images/{image_id}` | 이미지 삭제 (분석 전/검수 중, 삭제 시 해당 큐·대사도 삭제) |

제한(초안): 파일당 20MB, PNG/JPEG/WebP, 회차당 최대 100장. 수치는 CLOVA OCR 제한 확인 후 확정.

### 4.5 이미지 검수 표시·대체 텍스트

작가가 이미지(컷 묶음) 단위로 검수를 마쳤는지 표시하고, 독자용 대체 텍스트(`alt`)를 입력한다.

`PATCH /v1/episodes/{episode_id}/images/{image_id}` — 권한: author(본인), 허용 상태: 편집 가능 상태(§2)

```json
{ "reviewed": true, "alt": "주인공이 어두운 방에서 눈을 뜬다" }
```

→ `200` 갱신된 이미지

```json
{
  "id": "img_01",
  "index": 0,
  "url": "...",
  "width": 800,
  "height": 12000,
  "alt": "주인공이 어두운 방에서 눈을 뜬다",
  "reviewed": true,
  "reviewed_at": "2026-11-21T06:10:00Z"
}
```

- `reviewed: false`를 보내면 검수 완료를 취소한다 (`reviewed_at`은 `null`)
- 모든 이미지 응답(§4.4 목록, §6.1 스튜디오)에 `reviewed`, `reviewed_at`이 포함된다. 기본값은 `false`
- `reviewed`, `alt`는 둘 다 선택 항목이며 하나 이상 보내야 한다. `alt`는 최대 500자이고 빈 문자열이면 지운다
- 편집 가능 상태가 아니면 `409 EPISODE_STATE_INVALID`
- 검수 완료 후 그 이미지의 대사·효과음을 수정해도 `reviewed`는 자동으로 해제되지 않는다 (작가가 직접 되돌린다). 단 `stale`·`audio_failed`는 계속 문제 장면으로 집계된다
- 회차 검수 진행률은 `reviewed` 이미지 수 ÷ 전체 이미지 수로 계산한다 (§4.6)

### 4.6 대시보드·회차 통계

작품 단위 화면(회차별 문제 장면 수, 처리 남은 시간, 검수 진행률, 재생 길이, 이번 달 게시 수)을 위한 API. 같은 통계는 회차 목록 `?include=stats`로도 받을 수 있다.

`GET /v1/series/{series_id}/dashboard?month=2026-11&limit=50&cursor=` — 권한: author(본인). `month` 생략 시 현재 달(Asia/Seoul 기준). `episodes`는 최신 회차부터 `limit`개씩 나눠 주고(기본 50), `summary`는 전체 회차 기준이다

```json
{
  "series": { "id": "sr_01", "title": "제목", "genre": "fantasy", "serial_status": "ongoing" },
  "summary": {
    "month": "2026-11",
    "timezone": "Asia/Seoul",
    "episode_count": 12,
    "published_count": 8,
    "published_this_month": 2,
    "issue_count": 5,
    "in_progress_count": 1
  },
  "episodes": [
    {
      "id": "ep_12",
      "number": 12,
      "title": "12화",
      "state": "synthesizing",
      "image_count": 12,
      "published_at": null,
      "updated_at": "2026-11-21T05:00:00Z",
      "stats": {
        "reviewed_image_count": 0,
        "review_progress": 0.0,
        "issue_count": 0,
        "issue_breakdown": {
          "low_ocr_confidence": 0,
          "speaker_unknown": 0,
          "audio_failed": 0,
          "audio_stale": 0
        },
        "audio_duration_ms": null,
        "processing": {
          "job_id": "job_07",
          "step": "dubbing",
          "progress": { "done": 34, "total": 58 },
          "eta_seconds": 95
        }
      }
    }
  ],
  "next_cursor": null
}
```

`stats` 필드 정의

| 필드 | 정의 |
|---|---|
| `review_progress` | `reviewed_image_count / image_count` (0~1, 이미지가 0장이면 0) |
| `issue_count` | 문제 장면 수 = 플래그(§6.2 `flags`)가 하나 이상인 대사의 수. 한 대사에 플래그가 여러 개여도 1로 센다. 효과음 큐는 제외 |
| `issue_breakdown` | `low_ocr_confidence`(OCR 신뢰도 낮음), `speaker_unknown`(화자 미확정), `audio_failed`(음성 생성 실패), `audio_stale`(수정 후 재생성 필요). 플래그별 개수이므로 합계가 `issue_count`보다 클 수 있다 |
| `audio_duration_ms` | 음성(`dialogue`·`narration`·`description`) 음원 길이의 합. 효과음은 음성과 겹쳐 재생되므로 제외. 음성이 아직 없으면 `null` |
| `processing` | 진행 중인 작업(`analyzing`·`synthesizing`·재생성)이 있으면 작업 정보, 없으면 `null` |
| `processing.eta_seconds` | 처리 남은 시간 **예상**(초). 진행량과 직전 단계 평균 처리 속도로 계산하며, 계산 불가(대기 중 등)면 `null` |

`summary` 필드 정의

| 필드 | 정의 |
|---|---|
| `published_this_month` | `published_at`이 `month`에 속하는 회차 수 (Asia/Seoul 기준 월 경계) |
| `issue_count` | 회차별 `issue_count` 합계 |
| `in_progress_count` | `analyzing`·`synthesizing` 상태 회차 수 |

문제 장면 집계 규칙(제안): `audio_failed`, `audio_stale`은 항상 집계하고, `low_ocr_confidence`, `speaker_unknown`은 해당 이미지가 `reviewed: true`가 되면 집계에서 제외한다. 각 대사의 `flags`(§6.1)로 같은 기준을 확인할 수 있다.

구현 메모: 통계는 대사·이미지·작업이 바뀔 때 회차의 집계 컬럼을 갱신해 두고, 조회 시 전체를 다시 계산하지 않는다. `eta_seconds`만 조회 시 계산한다.

---

## 5. AI 분석·음성 생성 파이프라인

원고 업로드 후 **OCR → 대사·인물 정리 → 음성·효과음 생성**을 하나의 비동기 작업으로 실행한다.

### POST `/v1/episodes/{episode_id}/process`
파이프라인 시작. 허용 상태: `uploaded`, `failed`. 회차당 동시에 하나의 작업(`process`·`regenerate`)만 실행되며, 진행 중이면 `409 JOB_IN_PROGRESS`.

- `failed`에서 다시 호출하면 마지막으로 성공한 단계 다음부터 이어서 실행한다 (OCR·Dubbing 결과와 비용 재사용). 처음부터 다시 하려면 `"from_step": "ocr"`
- 일부 대사의 음성 생성만 실패하면 회차는 `needs_review`가 되고, 해당 대사에 `audio.status = "failed"`와 `flags: ["audio_failed"]`가 표시된다. 작가가 재생성(§7)으로 다시 시도한다
```json
{
  "from_step": null,
  "options": {
    "ocr_language": "ko",
    "narration": true,
    "description": true,
    "sfx": true
  }
}
```
→ `202`
```json
{ "job": { "id": "job_01", "status": "queued", "step": "ocr" }, "episode": { "state": "analyzing" } }
```

작업 단계(`step`)

| step | 내부 호출 | 결과 |
|---|---|---|
| `ocr` | CLOVA OCR | 이미지별 텍스트 영역(`bbox`)과 글자 |
| `structure` | CLOVA Studio (HyperCLOVA) | 대사 분리, 화자(인물), 감정, 나레이션·설명문 구분(`kind`), 효과음 태그 |
| `dubbing` | CLOVA Dubbing | 음성 생성 대상(`dialogue`·`narration`·`description`)별 음성, 효과음 프리셋 매칭 |
| `build_cues` | 내부 | 재생 타임라인용 `cues` 생성 |
| `done` | - | `episode.state = needs_review` |

### GET `/v1/jobs/{job_id}`
```json
{
  "id": "job_01",
  "episode_id": "ep_01",
  "kind": "process",
  "status": "running",
  "step": "dubbing",
  "progress": { "done": 34, "total": 58 },
  "eta_seconds": 95,
  "error": null,
  "started_at": "...",
  "finished_at": null
}
```
`status`: `queued | running | succeeded | failed | canceled`

### GET `/v1/episodes/{episode_id}/jobs`
해당 회차의 작업 이력

### POST `/v1/jobs/{job_id}/cancel`
진행 중 작업 취소 (이미 생성된 결과는 유지)

> 진행 상황 전달은 1차로 **폴링**(2~3초)을 쓴다. 필요하면 `GET /v1/jobs/{job_id}/events`(SSE)를 추가한다.

---

## 6. 작가 스튜디오: 검수·수정

분석 결과는 편집 가능 상태(`needs_review`, `scheduled`, `published`, §2)에서 조회·수정한다. `analyzing`·`synthesizing` 중에는 조회만 가능하다.

### 6.1 스튜디오 한 번에 불러오기

`GET /v1/episodes/{episode_id}/studio`

`?image_id=`를 주면 해당 이미지의 대사·효과음만 돌려준다 (이미지가 많을 때 이미지 단위로 나눠 불러온다). 음원 URL은 서명 URL이며 1시간 유효하고, 만료되면 다시 조회한다.

```json
{
  "episode": { "id": "ep_01", "state": "needs_review" },
  "images": [
    { "id": "img_01", "index": 0, "url": "...", "width": 800, "height": 12000, "alt": null, "reviewed": false, "reviewed_at": null }
  ],
  "characters": [
    { "id": "ch_01", "name": "민준", "voice_id": "vdain", "voice_params": { "speed": 0, "pitch": 0 } },
    { "id": "ch_narr", "name": "나레이션", "kind": "narration", "voice_id": "..." }
  ],
  "lines": [
    {
      "id": "ln_01",
      "image_id": "img_01",
      "order": 1,
      "text": "여기가 어디지?",
      "bbox": { "x": 0.12, "y": 0.231, "w": 0.30, "h": 0.04 },
      "y_ratio": 0.231,
      "kind": "dialogue",
      "character_id": "ch_01",
      "emotion": "confused",
      "audio": { "asset_id": "au_01", "url": "...", "duration_ms": 1800, "status": "ready" },
      "flags": []
    },
    {
      "id": "ln_02",
      "image_id": "img_01",
      "order": 2,
      "text": "어둠 속에서 눈을 떴다.",
      "bbox": { "x": 0.10, "y": 0.05, "w": 0.45, "h": 0.05 },
      "y_ratio": 0.05,
      "kind": "description",
      "character_id": "ch_narr",
      "emotion": "neutral",
      "audio": { "asset_id": "au_02", "url": "...", "duration_ms": 2400, "status": "ready" },
      "flags": ["low_ocr_confidence"]
    }
  ],
  "sfx_cues": [
    {
      "id": "cu_09",
      "image_id": "img_01",
      "y_ratio": 0.245,
      "sfx_tag": "door_open",
      "asset_id": "au_sfx_12",
      "url": "...",
      "volume": 0.8,
      "duration_ms": 900
    }
  ]
}
```

### 6.2 대사(lines)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/episodes/{episode_id}/lines` | 대사 목록 (`?image_id=`로 필터) |
| POST | `/v1/episodes/{episode_id}/lines` | 대사 수동 추가 (OCR 누락 보정) |
| PATCH | `/v1/lines/{line_id}` | 텍스트·화자·감정·종류·말풍선 위치(`bbox`)·큐 위치(`y_ratio`) 수정 |
| DELETE | `/v1/lines/{line_id}` | 대사 삭제 (효과음 등 불필요 항목 제거) |
| PUT | `/v1/episodes/{episode_id}/lines/order` | `y_ratio`가 같은 대사끼리의 동순위 재생 순서 변경 `{ "line_ids": [...] }` |

```json
// PATCH /v1/lines/{line_id}
{
  "text": "여기가 어디야?",
  "character_id": "ch_01",
  "emotion": "scared",
  "kind": "dialogue"
}
```
→ `200` 수정된 대사. 텍스트·화자·감정이 바뀌면 `audio.status = "stale"`로 표시된다 (재생성 필요, §7).

`kind`

| kind | 의미 | 음성 생성 |
|---|---|---|
| `dialogue` | 인물 대사 | 대상 |
| `narration` | 서술자 나레이션 | 대상 |
| `description` | 장면·상황을 설명하는 글(설명문) | 대상 |
| `sfx_text` | 의성어·의태어 등 효과음 글자 | 제외 (효과음은 `sfx_cues`로 처리) |
| `ignore` | 낙서·로고 등 음성화하지 않을 글자 | 제외 |

- 음성 생성 대상은 `dialogue`, `narration`, `description` 세 종류이며 파이프라인(§5)과 재생성(§7)에서 동일하게 적용된다
- `narration`·`description`은 `character_id`를 생략하면 작품의 나레이션 인물(`kind: "narration"` 인물)을 기본 화자로 쓰고, 다른 목소리를 지정할 수도 있다
- `kind`를 대상 종류로 바꾸면 `audio.status = "stale"`(재생성 필요), 제외 종류로 바꾸면 기존 음원 연결을 해제한다
- `flags`(읽기 전용, 자동 판정): `low_ocr_confidence`, `speaker_unknown`, `audio_failed`. `audio.status`가 `stale`이면 `audio_stale`로 집계한다. 대시보드의 문제 장면 수(§4.6)에 사용한다
- `audio.status`: `ready | stale | generating | failed`
- 재생 순서: 큐는 `(image_index, y_ratio, order)` 순으로 정렬한다. 대사 큐의 `y_ratio`는 기본값이 `bbox.y`이고 PATCH로 바꿀 수 있으며, `order`는 `y_ratio`가 같은 대사끼리의 순서만 정한다

`emotion`(초안): `neutral | happy | sad | angry | scared | surprised | confused | whisper | shout`

### 6.3 인물(characters)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/series/{series_id}/characters` | 작품의 인물 목록 (회차 간 공유) |
| POST | `/v1/series/{series_id}/characters` | 인물 추가 |
| PATCH | `/v1/characters/{character_id}` | 이름·목소리·파라미터 변경 |
| DELETE | `/v1/characters/{character_id}` | 삭제 (대사가 없을 때만) |
| POST | `/v1/characters/merge` | 같은 인물을 중복 인식한 경우 합치기 `{ "from": "...", "into": "..." }` |

```json
// PATCH /v1/characters/{character_id}
{ "name": "민준", "voice_id": "vdain", "voice_params": { "speed": 0, "pitch": -1, "emotion_default": "neutral" } }
```
인물의 목소리를 바꾸면 그 인물의 모든 대사가 `stale`이 된다.

### 6.4 사용 가능한 목소리·효과음 목록

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/voices` | CLOVA Dubbing에서 선택 가능한 목소리 (`id`, `name`, `gender`, `age`, `sample_url`) |
| GET | `/v1/sfx-presets` | 효과음 프리셋 (`tag`, `name`, `category`, `sample_url`) |

### 6.5 효과음 큐(sfx_cues)

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/episodes/{episode_id}/sfx-cues` | 효과음 추가 `{ "image_id", "y_ratio", "sfx_tag", "volume": 0.8 }` (`volume` 생략 시 1.0) |
| PATCH | `/v1/sfx-cues/{cue_id}` | 위치·프리셋·볼륨 수정 |
| DELETE | `/v1/sfx-cues/{cue_id}` | 삭제 |

효과음 큐 응답 필드

| 필드 | 설명 |
|---|---|
| `volume` | 0~1 실수, 기본 1.0. 작가가 정한 효과음 기본 음량. 독자 설정의 `sfx_volume`(§9.4)이 여기에 곱해진다 |
| `duration_ms` | 효과음 길이(ms). 프리셋 음원에서 가져오는 읽기 전용 값이며 요청으로 바꿀 수 없다 |

`volume`만 바꾸는 수정은 음원을 다시 만들지 않으며(재생 시 적용) `stale` 처리도 하지 않는다.

### 6.6 미리듣기

`GET /v1/lines/{line_id}/audio` → `302` 음원 URL (만료형 서명 URL)
`GET /v1/episodes/{episode_id}/preview` → 독자 뷰어와 동일 형식의 타임라인 (§9.2 `playback`), 게시 전 작가용

---

## 7. 재생성

수정한 부분만 다시 만든다. 음원은 `hash(text + voice_id + voice_params + emotion)`으로 캐싱하므로 동일 조건이면 비용 없이 재사용된다.

동시 작업 규칙
- 회차당 진행 중인 작업은 하나뿐이다. 재생성 중에 `process`·`regenerate`를 또 호출하면 `409 JOB_IN_PROGRESS`
- 재생성 대상 대사는 작업 동안 `audio.status = "generating"`이 된다. 그 사이 대사를 수정하는 것은 허용하되, 작업이 끝났을 때 입력(해시)이 달라졌으면 결과를 버리고 `stale`로 남긴다

### POST `/v1/episodes/{episode_id}/regenerate`
```json
{ "scope": "stale" }
```
`scope`
- `stale`: `stale` 상태인 대사만
- `lines`: `"line_ids": [...]` 지정한 대사만
- `all`: 전체 (쿼터 주의)

→ `202` `{ "job": { "id": "job_02", "kind": "regenerate", "status": "queued" } }`

### POST `/v1/lines/{line_id}/regenerate`
한 대사만 즉시 재생성 (같은 입력이어도 다른 결과가 필요한 경우 `"force": true`)
```json
{ "force": false }
```
→ `202` `{ "job": {...} }` 또는 캐시 적중 시 `200` `{ "audio": {...} }`

### 쿼터
`GET /v1/me/quota` → `{ "dubbing_chars": { "used": 12000, "limit": 100000 }, "ocr_images": { "used": 40, "limit": 500 } }`
초과 시 `429 QUOTA_EXCEEDED`.

---

## 8. 게시

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/episodes/{episode_id}/publish` | 게시 또는 재게시. 허용 상태: `needs_review`, `published`(수정 후 재게시). 조건: `stale`·`generating` 대사 없음, 모든 이미지 `reviewed` |
| POST | `/v1/episodes/{episode_id}/unpublish` | 게시·예약 취소 → `needs_review`로 복귀 (`published_at`은 유지) |

```json
// POST /v1/episodes/{episode_id}/publish
{ "publish_at": null, "allow_unreviewed": false }
```
- `publish_at`이 미래 시각이면 예약 게시: 회차는 `scheduled`가 되고 워커의 스케줄러가 그 시각에 게시한다 (실패 시 재시도, 로그 기록). `null`이면 즉시 게시
- `stale`·`generating` 대사가 남아 있으면 `409 EPISODE_STATE_INVALID` + `details.stale_line_ids`
- `reviewed: false`인 이미지가 있으면 `409 REVIEW_INCOMPLETE` + `details.unreviewed_image_ids`. 작가가 확인하고 넘어가려면 `"allow_unreviewed": true`를 보낸다
- 게시 시 재생 타임라인을 확정 저장(스냅샷)한다. 게시된 회차도 스튜디오에서 수정할 수 있으며(§6), 수정하면 `has_unpublished_changes = true`가 되지만 독자는 마지막 스냅샷을 계속 본다. `publish`를 다시 호출하면 스냅샷(`playback.version`)이 갱신되고 플래그가 `false`로 돌아간다
- 최초 게시 때 `published_at`을 기록한다(예약 게시는 실제 게시되는 시각). 게시 취소나 재게시로 바뀌지 않으므로 "이번 달 게시 수"가 중복·이동 없이 계산된다

---

## 9. 독자용 API (보기·듣기)

공개된 회차만 대상으로 하며 비로그인 조회를 허용한다.

### 9.1 작품·회차 탐색

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/public/series` | 공개 작품 목록 (`?q=`, `?genre=`, `?serial_status=`, `?cursor=`). 응답에 `genre`, `serial_status` 포함 |
| GET | `/v1/public/series/{series_id}` | 작품 상세 |
| GET | `/v1/public/series/{series_id}/episodes` | 게시된 회차 목록 |

### 9.2 회차 보기 + 재생 타임라인

`GET /v1/public/episodes/{episode_id}`

```json
{
  "id": "ep_01",
  "title": "1화",
  "series_id": "sr_01",
  "language": "ko",
  "images": [
    { "index": 0, "url": "...", "width": 800, "height": 12000, "alt": "주인공이 어두운 방에서 눈을 뜬다" }
  ],
  "playback": {
    "version": 3,
    "urls_expire_at": "2026-11-21T11:00:00Z",
    "cues": [
      {
        "id": "cu_01",
        "image_index": 0,
        "y_ratio": 0.231,
        "kind": "dialogue",
        "text": "여기가 어디지?",
        "speaker": "민준",
        "audio_url": "https://...signed...",
        "duration_ms": 1800
      },
      {
        "id": "cu_02",
        "image_index": 0,
        "y_ratio": 0.245,
        "kind": "sfx",
        "label": "문 열리는 소리",
        "audio_url": "https://...signed...",
        "volume": 0.8,
        "duration_ms": 900
      }
    ]
  },
  "prev_episode_id": null,
  "next_episode_id": "ep_02"
}
```

- 큐는 `(image_index, y_ratio)` 오름차순. 뷰어는 스크롤 위치가 `y_ratio`를 지날 때 재생한다
- `kind`: `dialogue | narration | description | sfx`. 음성 큐(앞의 세 종류)는 `speaker`, `text`를, `sfx` 큐는 `label`, `volume`을 가진다
- `text`, `label`, `alt`는 **스크린 리더**와 자막 표시에 사용한다. 효과음도 `label`로 텍스트 대체를 제공한다
- `audio_url`은 만료형 서명 URL이며 기본 6시간 유효하다 (`playback.urls_expire_at`). 독자가 오래 열어 두었다가 이어 들을 때를 위해 `GET /v1/public/episodes/{episode_id}/cues/{cue_id}/audio`가 새 서명 URL로 `302` 리다이렉트한다. 뷰어는 만료 시각이 지났거나 403이면 이 경로로 다시 받는다
- 게시 전 원고 이미지는 비공개이며 서명 URL(1시간)로만 접근한다. 게시된 회차의 이미지 접근 방식은 CDN 도입 여부와 함께 정한다 (§14)
- `alt`는 접근성용 이미지 설명 (작가가 §4.5의 PATCH로 입력, 없으면 빈 문자열)

### 9.3 키보드·스크린 리더 지원을 위한 보조 API

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/v1/public/episodes/{episode_id}/transcript` | 회차 전체 텍스트 대본 (화자·효과음 라벨 포함, 스크린 리더용 선형 읽기) |

```json
{
  "episode_id": "ep_01",
  "items": [
    { "index": 1, "image_index": 0, "kind": "narration", "speaker": null, "text": "어둠 속에서 눈을 떴다." },
    { "index": 2, "image_index": 0, "kind": "description", "speaker": null, "text": "낡은 창고 안, 먼지가 가라앉아 있다." },
    { "index": 3, "image_index": 0, "kind": "dialogue", "speaker": "민준", "text": "여기가 어디지?" },
    { "index": 4, "image_index": 0, "kind": "sfx", "text": "[문 열리는 소리]" }
  ]
}
```

키보드 조작(예: 다음/이전 컷, 재생/일시정지, 효과음 끄기)은 프런트 구현이며, 서버는 설정 저장만 담당한다.

### 9.4 독자 설정 (로그인 시)

`GET/PATCH /v1/me/settings`

```json
{
  "reader": {
    "autoplay": true,
    "dialogue_volume": 0.9,
    "sfx_volume": 0.6,
    "narration": true,
    "show_subtitles": true,
    "playback_rate": 1.0,
    "high_contrast": false
  }
}
```

### 9.5 진행 상황

| 메서드 | 경로 | 설명 |
|---|---|---|
| PUT | `/v1/me/progress/{episode_id}` | 읽은 위치 저장 `{ "image_index": 3, "y_ratio": 0.42 }` |
| GET | `/v1/me/progress/{episode_id}` | 이어보기 위치 조회 |

---

## 10. 번역 (확장 계획, Papago)

1차 릴리스 범위 밖. 확장 시 아래 형태로 추가한다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/episodes/{episode_id}/translations` | 추출된 대사를 Papago로 번역 `{ "target_lang": "en" }` → `202` + job |
| GET | `/v1/episodes/{episode_id}/translations` | 번역된 언어 목록과 상태 |
| PATCH | `/v1/translations/{translation_id}/lines/{line_id}` | 작가가 번역문 수정 |
| GET | `/v1/public/episodes/{episode_id}?lang=en` | 번역 자막이 포함된 타임라인 (`text`가 번역문으로 대체, 원문은 `original_text`) |

- 번역은 **자막**으로 제공하는 것이 기본이며, 번역 음성 생성은 별도 결정이 필요하다 (Dubbing의 외국어 지원 여부 확인)
- 번역 결과도 작가 검수 후 게시하는 흐름을 유지한다

---

## 11. 운영·상태 확인

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/healthz` | 프로세스 상태, `revision` 반환 (배포 확인용) |
| GET | `/readyz` | DB 연결 확인 |

> 운영 Swagger는 `/v1/docs`, 개발 Swagger는 `/docs`에서 열린다. 인증 API를 호출하려면 로그인 응답의 액세스 토큰을 Swagger의 **Authorize**에 입력한다.

---

## 12. 데이터 모델과의 대응

| API 리소스 | 테이블 |
|---|---|
| user / me | `users` |
| series | `series` (genre, serial_status, cover 추가) |
| episodes | `episodes` (published_at, scheduled_at, has_unpublished_changes, 검수·문제 집계 컬럼 추가) |
| images | `episode_images` (reviewed, reviewed_at 추가) |
| characters | `characters` (voice_id, voice_params) |
| lines | `dialogue_lines` (text, bbox, y_ratio, order, speaker, emotion, kind, audio_status) — `flags`는 계산값 |
| audio / sfx | `audio_assets` (hash, url, kind) |
| cues / playback | `cues` (episode_id, image_idx, y_ratio, asset_id, kind, volume, duration_ms) — `duration_ms`는 `audio_assets`에서 복사 |
| jobs | `jobs` (status, step, progress_done, progress_total, error, started_at) — `eta_seconds`는 조회 시 계산 |
| 게시 스냅샷 | `episode_playbacks` (episode_id, version, json) — 신규 |
| 독자 설정·진행 | `reader_settings`, `reading_progress` — 신규 |
| 번역 (확장) | `translations`, `translation_lines` — 신규 |

---

## 13. 마일스톤별 구현 범위

| 마일스톤 | 포함 API |
|---|---|
| M0 | §11 (`/healthz`, `/readyz`) |
| M1 | §3 인증, §4 작품·회차·이미지 업로드(presigned) |
| M2 | §5 파이프라인 (`process`, `jobs`), OCR → 대사 구조화 → Dubbing 1회차 end-to-end |
| M3 | §4.5 검수 표시, §4.6 대시보드, §6 스튜디오·검수 API, §8 게시(예약 게시 스케줄러 포함), §9 독자용 타임라인·대본 |
| M4 | §7 부분 재생성·캐싱·쿼터, §9.4~9.5 설정·진행, 로깅 |
| 확장 | §10 Papago 번역 |

---

## 14. 미정 사항

- Dubbing이 감정·속도 파라미터와 효과음 삽입을 어떻게 지원하는지 (콘솔·문서 확인 후 `voice_params`, `sfx_tag` 확정)
- 효과음을 Dubbing 결과에 포함할지, 별도 음원으로 받아 `cues`로 독립 재생할지 (독자 뷰어에서 볼륨을 따로 조절하려면 **별도 음원**이 유리)
- `/api` 접두어를 프런트 프록시가 제거하는지
- 독자 로그인 필수 여부, 작가·독자 계정 통합 여부
- 이미지 제한(크기·장수)과 쿼터 수치
- 번역 음성 생성 여부
- `description`의 정확한 의미 확정 (이 문서는 "장면·상황 설명 글"로 정의, `narration`과 목소리를 분리할지 여부)
- OCR 신뢰도 임계값 등 문제 장면 판정 기준 (검수 완료 후 수정해도 `reviewed`는 자동 해제하지 않는 것으로 정했으며, 필요하면 재검토)
- `audio_duration_ms`는 음원 길이의 합이며, 스크롤 속도에 따라 달라지는 실제 감상 시간과는 다를 수 있음
- 재게시 시각이 필요하면 `republished_at`(또는 `last_published_at`) 추가 여부
- `genre`를 단일 값으로 둘지 다중(`genres`)으로 둘지, 장르 목록 확정
- 작가 가입 방식 (현재는 초대 코드 가정), 비밀번호 정책
- 서명 URL 만료 시간(현재 음원 6시간·원고 1시간 가정), 게시된 회차의 이미지·음원 접근 방식과 CDN 도입 여부
- 예약 게시 스케줄러 구현 방식 (워커 주기 폴링 vs 별도 스케줄러)
