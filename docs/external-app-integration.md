# 외부 애플리케이션 REST/HLS 연동 계약

## 목차

- [1. 범위와 기준](#1-범위와-기준)
- [2. Base URL과 OpenAPI](#2-base-url과-openapi)
- [3. 인증 계약](#3-인증-계약)
- [4. Camera와 영상](#4-camera와-영상)
- [5. Event와 상태](#5-event와-상태)
- [6. 관리자 Edge/Profile 계약](#6-관리자-edgeprofile-계약)
- [7. 시간, 페이지네이션과 오류](#7-시간-페이지네이션과-오류)
- [8. 현재와 Future 경계](#8-현재와-future-경계)

## 1. 범위와 기준

이 문서는 별도 외부 사용자 애플리케이션이 AI_CCTV 중앙 서버에 연동하는 공개 계약의 정본이다. 모바일 네이티브 앱과 Web UI 자체는 이 저장소의 구현 범위가 아니며, 앱은 Edge에 직접 접속하지 않는다.

```text
External App
  └─ HTTPS → Nginx public origin
       ├─ /api/v1/* → External Service
       │    └─ recovered MPEG-TS content → Data Service protected stream
       ├─ /hls/* → auth_request → MediaMTX HLS
       └─ /playback/* → auth_request → MediaMTX Playback

MediaMTX ── internal RTSP ──> Inference Service
```

외부 앱에 제공하는 실시간 영상은 HLS다. RTSP는 Edge→중앙과 MediaMTX→Inference 내부 경로에만 사용한다.

## 2. Base URL과 OpenAPI

- 운영 Base URL: `https://cctv.example.com`
- API Prefix: `/api/v1`
- OpenAPI 3 JSON: `GET /api/v1/openapi.json`
- 대화형 문서: `GET /api/v1/docs`

External FastAPI가 위 Versioned 경로에 Schema와 문서를 생성하고 Nginx의 기존 `/api/` Proxy가 이를 공개한다. 내부 `/internal/` API, Data Service·Edge API와 `/health/*`는 공개 Schema가 아니며 Nginx 공개 Listener에서 접근할 수 없다. Client 생성 시 배포 서버에서 받은 `/api/v1/openapi.json`을 사용하고 Version Prefix를 생략하지 않는다.

서버에 `PUBLIC_BASE_URL=https://cctv.example.com`을 설정하면 Live와 Playback 응답은 Absolute HTTPS URL을 반환한다. 이 값이 없는 개발 환경에서만 동일 Origin 기준 상대 경로가 올 수 있으므로 Client는 다음처럼 처리한다.

```text
absolute URL → 그대로 사용
relative URL → API 요청에 사용한 HTTPS origin으로 resolve
```

운영 환경에서 `http://` 미디어 URL은 거부한다. URL의 Host나 Port를 조합하거나 Edge 주소를 추론하지 않는다.

## 3. 인증 계약

### 3.1 로그인

```http
POST /api/v1/auth/login
Content-Type: application/json
```

```json
{
  "username": "viewer01",
  "password": "user-supplied-password"
}
```

성공 응답은 Bearer Client용 JSON Token을 반환하는 동시에 다음 Cookie를 설정한다.

- Access Cookie: 기본 이름 `ai_cctv_access`, `HttpOnly`, `Secure`, `Path=/`
- Refresh Cookie: 기본 이름 `ai_cctv_refresh`, `HttpOnly`, `Secure`, `Path=/api/v1/auth`

```json
{
  "access_token": "<jwt>",
  "refresh_token": "<rotating-refresh-jwt>",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": 1,
    "username": "viewer01",
    "role": "viewer",
    "is_active": true
  }
}
```

Token, Cookie, 비밀번호는 로그·분석 이벤트·Crash Report·URL에 기록하지 않는다.

### 3.2 공식 사용 방식

| 요청 | 공식 인증 |
| --- | --- |
| REST API | `Authorization: Bearer <access_token>`; Access Cookie도 지원 |
| Browser/HLS 연속 재생 | 로그인에서 설정된 HttpOnly Secure Access Cookie |
| 헤더 주입 가능한 Native HLS Player | Playlist와 모든 Segment 요청에 Bearer도 가능 |
| Query String Token | 금지 |

HLS의 공식 방식은 Cookie다. Nginx는 Manifest와 모든 Media Segment 요청마다 `Authorization`과 `Cookie`를 내부 `auth_request`에 전달하고 JWT, 사용자 활성 상태와 Camera ACL을 다시 확인한다. 인증 경계는 `%` 인코딩, 역슬래시, 중복 Slash와 dot Segment가 있는 모호한 원본 HLS/Playback URI를 거부한다. Cookie를 쓰는 Browser Client는 API와 HLS를 같은 HTTPS Origin에서 사용하고 요청 Credential을 포함해야 한다.

### 3.3 갱신과 로그아웃

Browser는 Refresh Cookie로 빈 Body 요청을 보낼 수 있고 Native Client는 JSON Body에 Refresh Token을 보낼 수 있다.

```http
POST /api/v1/auth/refresh
Content-Type: application/json
```

```json
{
  "refresh_token": "<current-refresh-jwt>"
}
```

Refresh Token은 매 성공 시 회전한다. 이전 Token을 재사용하지 않는다. HLS 재생 중 Access Token 만료로 Manifest 또는 Segment가 `401`이면 재생을 잠시 멈추고 한 번 Refresh한 뒤 Playlist를 다시 로드한다. Refresh도 실패하면 Cookie/Token을 폐기하고 로그인 화면으로 이동한다. 여러 동시 요청이 각각 Refresh하지 않도록 Client에서 단일 갱신 작업으로 합친다.

```http
POST /api/v1/auth/logout
```

로그아웃은 서버 측 Token을 철회하고 인증 Cookie를 삭제한다.

## 4. Camera와 영상

### 4.1 카메라 목록

```http
GET /api/v1/cameras?limit=50&offset=0
Authorization: Bearer <access_token>
```

Viewer는 ACL이 허용된 카메라만 받는다. 일반 Camera 응답에는 `source_url`, `edge_management_url`, `edge_recovery_url`, `edge_auth_token`, RTSP 게시 자격증명 등 Edge 내부 접속정보가 절대 포함되지 않는다.

### 4.2 Live HLS

```http
GET /api/v1/cameras/cam-001/live
Authorization: Bearer <access_token>
```

```json
{
  "camera_id": "cam-001",
  "protocol": "hls",
  "url": "https://cctv.example.com/hls/cam-001/index.m3u8",
  "hls_url": "https://cctv.example.com/hls/cam-001/index.m3u8",
  "auth": {
    "method": "cookie",
    "cookie_name": "ai_cctv_access"
  }
}
```

`url`이 정식 필드이고 `hls_url`은 기존 Client 호환 Alias다. Player는 응답 URL만 사용하고 `/hls/{camera_id}/index.m3u8`를 직접 조합하지 않는다. Browser에서는 JavaScript로 HttpOnly Cookie 값을 읽으려 하지 말고 Cookie Jar가 Playlist와 Segment에 자동 첨부하도록 한다.

### 4.3 저장 영상 검색과 Playback

```http
GET /api/v1/recordings?camera_id=cam-001&from=2026-08-23T07%3A00%3A00Z&to=2026-08-23T08%3A00%3A00Z&limit=50&offset=0
Authorization: Bearer <access_token>
```

```http
GET /api/v1/recordings/{recording_id}/playback
Authorization: Bearer <access_token>
```

```json
{
  "recording_id": "recording-001",
  "playback_url": "https://cctv.example.com/playback/get?path=cam-001&start=2026-08-23T07%3A00%3A00Z&duration=60.000&format=fmp4"
}
```

`playback_url`은 불투명한 서버 선택값으로 보고 응답 그대로 사용한다. 중앙 MediaMTX가 만든 `central` fMP4는 위와 같은 보호된 `/playback/get` URL을 반환한다. Edge에서 복구한 MPEG-TS는 다음처럼 External Service가 ACL을 다시 확인하는 보호 URL을 반환할 수 있다.

```json
{
  "recording_id": "42",
  "playback_url": "https://cctv.example.com/api/v1/recordings/42/content"
}
```

복구 Content는 `GET /api/v1/recordings/{recording_id}/content`에서 `video/mp2t`로 제공한다. 전체 응답은 `200`, 유효한 Byte Range는 `206`과 `Content-Range`, 만족할 수 없는 Range는 `416`이다. 응답의 ETag를 `If-Range`로 보내면 일치할 때만 부분 응답을 받고, 오래된 Validator면 서버가 최신 전체 본문 `200`을 반환한다. REST와 같은 Bearer 또는 Access Cookie가 필요하며 Camera ACL을 적용한다. Client는 형식이나 URL Prefix를 추론하지 않고 `playback_url`의 Content-Type과 상태에 따라 재생한다. HLS VOD는 현재 필수 경로가 아니다.

## 5. Event와 상태

### 5.1 Event 검색

```http
GET /api/v1/events?camera_id=cam-001&event_type=person_appeared&from=2026-08-23T07%3A00%3A00Z&to=2026-08-23T08%3A00%3A00Z&limit=50&offset=0
```

상세 조회는 `GET /api/v1/events/{event_id}`다. Client는 알려지지 않은 신규 `event_type`이나 Metadata를 역직렬화 실패로 버리지 말고 표시 가능한 문자열/객체로 보존한다.

| Event Type | 의미 | 대표 Metadata |
| --- | --- | --- |
| `person_appeared`, `person_disappeared` | 사람 Track 전이 | `track_id`, `confidence` |
| `camera_input_lost`, `camera_input_restored` | Edge Camera Frame 중단·복구 | `reason`, `timeout_seconds` |
| `central_connection_lost`, `central_connection_restored` | Edge Publisher↔중앙 연결; 자동 복구 권위 Event | `reason` |
| `inference_stream_lost`, `inference_stream_restored` | 중앙 RTSP 추론 소비 중단·복구; 복구 비권위 | `reason` |
| `external_power_lost`, `external_power_restored` | 외부 전원 전환 | `battery_percent`, `power_source` |
| `battery_low`, `battery_critical` | Battery 임계치 | `battery_percent`, `power_source` |
| `storage_warning`, `storage_critical` | 저장공간 임계치 | `storage_percent` |
| `edge_offline`, `edge_online` | Edge HTTP 가용성 | `source` |
| `video_profile_changed`, `video_profile_change_failed` | Profile 적용 결과 | Profile 값, `reason_code` |

### 5.2 카메라 Runtime 상태

```http
GET /api/v1/cameras/cam-001/status
```

```json
{
  "camera_id": "cam-001",
  "online": true,
  "cpu_percent": 32.5,
  "memory_percent": 58.1,
  "storage_percent": 71.2,
  "battery_percent": 84,
  "power_source": "external",
  "camera_input": "online",
  "central_connection_status": "online",
  "current_video_profile": "hd",
  "last_seen_at": "2026-08-23T07:20:00Z",
  "last_error_code": null
}
```

센서 또는 Capability가 없는 필드는 `null`일 수 있다. `online=false`와 `camera_input=offline`은 각각 Edge 연결 장애와 카메라 입력 장애이므로 동일 상태로 합치지 않는다.

## 6. 관리자 Edge/Profile 계약

카메라 등록·수정과 Profile 변경은 Admin 전용이다. Profile·상태 조회는 해당 Camera ACL이 있는 Viewer도 사용할 수 있다. Configurator는 Admin 권한으로 이 계약을 사용한다.

```http
POST /api/v1/cameras
Authorization: Bearer <admin-access-token>
Content-Type: application/json
```

```json
{
  "camera_id": "cam-001",
  "name": "Entrance",
  "edge_device_id": "edge-001",
  "edge_management_url": "http://192.0.2.41:8003",
  "edge_recovery_url": "http://192.0.2.41:8002",
  "edge_auth_token": "<edge-bearer-token>",
  "enabled": true
}
```

- `edge_management_url`: 상태·제어·이벤트 API, 기본 Port 8003
- `edge_recovery_url`: `/v1/recovery` Manifest/File API, 기본 Port 8002
- `edge_auth_token`: 두 API가 공유하는 32자 이상 Bearer Token

두 URL은 독립 필드이며 한 URL에서 다른 Port를 추론하지 않는다. Configurator의 신규 Edge 등록은 네 Edge 필드를 모두 요구한다. 영상 Bootstrap만 필요한 일반 Camera 등록은 네 필드 전체를 생략할 수 있지만 일부만 포함한 신규 등록은 `422`로 거부된다. 업그레이드 전 DB의 불완전 Metadata는 조회할 수 있으나 제어·복구가 `CAPABILITY_UNKNOWN` 또는 `failed`이며 관리자는 `edge-update`로 완성해야 한다. Token은 어떠한 외부 응답에도 반환하지 않는다.

서버는 동시에 최대 4개 Camera만 활성화한다. Recording/Event/Recovery 이력을
보존한 비활성 Camera는 이 활성 한도에 포함하지 않지만, 다시 활성화할 때는 한도를
재검사한다. 신규 Camera는 동적 게시 자격증명 Hash를 저장하기 전까지 내부적으로
disabled 상태이므로 같은 ID의 오래된 Bootstrap 자격증명이 등록 중간에 Publisher로
남을 수 없다.

기존 Camera의 Edge Metadata 보완·교체는 다음 Admin 요청을 사용한다. 생략한 필드는 유지하며 Secret은 응답에서 제거된다.

```http
PATCH /api/v1/cameras/cam-001
Authorization: Bearer <admin-access-token>
Content-Type: application/json
```

```json
{
  "edge_device_id": "edge-001",
  "edge_management_url": "http://192.0.2.41:8003",
  "edge_recovery_url": "http://192.0.2.41:8002",
  "edge_auth_token": "<rotated-edge-bearer-token>"
}
```

등록 성공의 `publish_credentials`는 RTSP 게시 설정을 위해 한 번만 반환된다.

```json
{
  "camera_id": "cam-001",
  "publish_credentials": {
    "username": "cam-001",
    "password": "<one-time-generated-password>"
  }
}
```

Configurator CLI는 등록 전에 필수 `--publish-credentials-output` 경로를 검증하고, 응답에서 해당 Camera의 `camera_id`, `username`, `password`만 추출해 원자적으로 `0600` JSON 파일에 저장한다. GUI도 명시적인 저장 경로를 요구한다. 비밀번호는 결과 창·표준출력·로그에 표시하지 않는다. 관리자는 파일을 일치하는 Edge RTSP publish setup에 전달하고, 설치가 끝나면 정책에 따라 불필요한 복사본을 제거한다.

Edge에서는 `ai-cctv-edge setup --publish-credentials-file <file>`로 이 JSON을 가져온다. Edge는 `camera_id`와 `username`이 설정하려는 Camera ID와 모두 일치하고 Linux 파일 권한이 group/world에 열려 있지 않은 경우에만 비밀번호를 저장한다.

운영 중 게시 비밀번호가 노출됐거나 Edge를 교체할 때는 다음 Admin API를 사용한다.

```http
POST /api/v1/cameras/cam-001/publish-credentials/rotate
Authorization: Bearer <admin-access-token>
```

응답 형식은 등록 응답과 같은 일회성 `publish_credentials`이며
`Cache-Control: no-store`다. 서버는 Camera를 일시 차단하고 기존 Publisher를 종료한
뒤 새 Argon2 Hash를 저장하며, 성공하면 요청 전 활성 상태를 복원한다. Configurator
GUI의 자격증명 재발급 또는 다음 CLI를 사용하면 원문을 화면에 표시하지 않고 제한
권한 전달 파일로 바로 저장한다.

```bash
ai-cctv-server edge-rotate-credentials cam-001 \
  --server-url https://cctv.example.com \
  --publish-credentials-output /secure/cam-001-publish-rotated.json
```

카메라를 `PATCH /api/v1/cameras/{camera_id}`의 `enabled=false`로 바꾸면 서버는 먼저 DB와 Runtime을 disabled로 만들어 새 게시 인증과 Live/HLS를 차단한 뒤 MediaMTX v1.9의 해당 RTSP publisher session을 종료한다. Media 제어 실패 시 Camera는 차단 상태로 남고 `MEDIA_CONTROL_UNAVAILABLE`을 반환하므로 같은 요청을 재시도한다. `DELETE /api/v1/cameras/{camera_id}`는 Recording/Event/Recovery 이력을 먼저 검사하며, 이력이 있으면 `CAMERA_HAS_HISTORY` `409`로 거부하고 기존 활성 상태와 Publisher를 그대로 유지한다. 이력이 없을 때만 disable→publisher 종료→삭제를 수행한다.

```http
GET /api/v1/cameras/cam-001/video-profile
```

```json
{
  "camera_id": "cam-001",
  "current_profile": "hd",
  "desired_profile": "hd",
  "supported_profiles": ["hd", "fhd"],
  "edge_online": true,
  "last_error_code": null
}
```

```http
PATCH /api/v1/cameras/cam-001/video-profile
Content-Type: application/json
```

```json
{
  "profile": "fhd"
}
```

Profile은 `hd`(1280×720@30fps, 2Mbps) 또는 `fhd`(1920×1080@30fps, 4Mbps)다. 서버는 Edge가 Pipeline을 실제 적용했다는 성공 응답을 받은 뒤에만 `current_profile`을 바꾼다.

자동 복구 작업과 결과는 Admin이 조회한다.

```http
GET /api/v1/recovery-jobs?camera_id=cam-001&limit=50&offset=0
Authorization: Bearer <admin-access-token>
```

상태는 `detected`, `waiting_for_recovery`, `downloading`, `indexing`, `completed`, `failed` 중 하나다. `attempt_count`, `max_attempts`, `next_retry_at`, `last_error`, 완료 `recovery_summary`를 표시하되 내부 Token이나 Edge URL은 노출하지 않는다.

## 7. 시간, 페이지네이션과 오류

### 7.1 UTC

- Query의 `from`, `to`는 timezone이 포함된 RFC 3339여야 한다.
- 서버의 표준 응답은 UTC `Z` 형식이다. 예: `2026-08-23T07:20:00Z`.
- Offset 없는 Local datetime은 보내지 않는다.
- UI 표시 직전에만 장치 Local timezone으로 변환하고 API 재요청 시 다시 UTC로 직렬화한다.
- 시간 범위는 `from < to`여야 한다.

### 7.2 페이지네이션

목록 API는 `limit`(기본 50, 최대 100)과 `offset`을 사용한다. 응답이 빈 목록일 때까지 순차 조회하고 정렬 순서나 전체 개수를 암묵적으로 추론하지 않는다.

### 7.3 오류

일반 오류:

```json
{
  "detail": "Camera not found"
}
```

입력 검증 `422`의 `detail`은 `{type, loc, msg}` 항목 배열이다. Profile 거부처럼 Client가 분기해야 하는 오류는 Edge `reason_code`를 정규화한 안정된 `error.code`를 제공한다.

```json
{
  "error": {
    "code": "UNSUPPORTED_VIDEO_PROFILE",
    "message": "The Edge device does not support the requested video profile.",
    "details": {
      "requested_profile": "fhd",
      "supported_profiles": ["hd"]
    }
  }
}
```

| HTTP | 처리 |
| ---: | --- |
| 400 | 요청 형식·시간 범위·Camera ID 수정 |
| 401 | Access Refresh 한 번 시도, 실패하면 재로그인 |
| 403 | 역할 또는 Camera ACL 부족으로 표시; 재시도 금지 |
| 404 | 삭제·미존재 자원으로 표시 |
| 409 | 상태 충돌 또는 Profile 적용 거부; `reason_code` 표시 |
| 422 | 필드별 입력 검증 메시지 표시 |
| 429 | `Retry-After` 이후 로그인 재시도 |
| 502/503/504 | 일시 장애로 제한된 Backoff 재시도 |

주요 Profile/Edge 오류 코드는 `EDGE_OFFLINE`, `CAPABILITY_UNKNOWN`, `UNSUPPORTED_VIDEO_PROFILE`, `CAMERA_UNAVAILABLE`, `ENCODER_UNAVAILABLE`, `PIPELINE_START_FAILED`, `ROLLBACK_FAILED`, `CONTROL_TIMEOUT`이다. Camera 생명주기에서는 `CAMERA_HAS_HISTORY`, `CAMERA_LIMIT_REACHED`, `MEDIA_CONTROL_UNAVAILABLE`을 구분한다. 알 수 없는 코드는 원문을 보존하고 일반 오류로 표시한다. 오류 메시지나 요청 Body를 기록할 때 비밀번호·JWT·Edge Token은 반드시 제거한다.

## 8. 현재와 Future 경계

현재 상태 조회, Profile 제어와 복구는 인증된 HTTP다. MQTT Telemetry, Event, Availability(LWT/retained), Command/Result, QoS와 중복 처리는 Future 범위다. 외부 앱은 MQTT Broker에 직접 의존하지 않고 앞으로도 Versioned HTTPS API를 사용한다. 영상 경로는 MQTT 도입 여부와 무관하게 사용자 HLS/Playback, 내부 RTSP를 유지한다.
