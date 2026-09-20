# Preprocessing 인터페이스

MediaMTX 영상에서 사람을 감지·추적하고, 이미지·이벤트·좌표·인물 특징을 Data에 전달한다. 새 컨테이너가 유지할 실행·입출력 계약이다.

## 실행 환경

| 항목 | 값 |
|---|---|
| 서비스·네트워크 | Compose `preprocessing`, `internal` |
| Data 주소 | `DATA_SERVICE_URL=http://nginx:8080/internal/data/v1` |
| 영상 | `rtsp://mediamtx:8554/{stream_path}`, RTSP/TCP |
| 인증 | 감지용 `DATA_INFERENCE_TOKEN`, identity용 `DATA_IDENTITY_TOKEN`, 영상용 `MEDIA_READ_USERNAME/PASSWORD` |
| 마운트 | `/snapshots` 읽기·쓰기, `/models`·`/app/config/config.yaml` 읽기 전용 |
| 실행 사용자 | `AI_CCTV_UID:AI_CCTV_GID`, 기본 `1000:1000` |
| 상태 API | 내부 `0.0.0.0:8000`의 `/health/live`, `/health/ready`, `/internal/v1/status` |

설정 우선순위는 컨테이너 환경변수 → YAML `inference` → 기본값이다. 모델·세부 설정은 [서비스 README](../../server/services/preprocessing/README.md)와 [Compose](../../server/compose.yml)를 따른다.

## Data API

경로는 Data 주소 뒤에 붙인다. 인증값은 `X-Internal-Token`, JSON 요청은 `Content-Type: application/json`으로 전달한다.

| 토큰 | 요청 | 성공 응답 |
|---|---|---|
| 감지 | `GET /cameras/enabled` | `{"items":[...]}` |
| 감지 | `PATCH /cameras/{camera_id}/status` | 갱신한 카메라 |
| 감지 | `POST /events` | 201, 저장한 이벤트 |
| 감지 | `PUT /cameras/{camera_id}/objects` | `{"accepted":true}` |
| identity | `POST /object-jobs/identity/claim` | `{"job":{...}}` 또는 `{"job":null}` |
| identity | `POST /object-jobs/identity/{job_id}/complete` | `{"accepted":true}` 또는 `false` |
| identity | `POST /object-jobs/identity/requeue-unconfigured` | `{"requeued":건수}`, 최대 100건 |

claim·requeue는 본문 없이 호출한다. 감지·identity 토큰은 서로 바꿔 쓰지 않는다. 중앙 DB·gallery는 Data가 관리한다.

## 카메라와 추적

활성 목록의 `camera_id`와 `stream_path`를 사용한다. 추가된 카메라는 시작하고 빠진 카메라는 중지한다. 목록 조회 실패 시 기존 카메라를 유지한다. 상태 PATCH는 `{"status":"online"}` 형식이며 `online|offline|degraded|disabled`를 허용한다.

| 값 | 규칙 |
|---|---|
| 카메라·스트림 ID | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| 로컬 추적 | `(camera_id, tracking_session_id, person_id)`; person ID는 1~256자 문자열 |
| 추적 세션 | 소문자 16진수 32자; 재시작·재접속·추적 초기화마다 새로 생성 |
| bbox | 원본 픽셀의 정수 `[x1,y1,x2,y2]`; 프레임 내부의 양수 면적 |
| 프레임 크기 | 각 변 정수 1~16384 |
| 객체 목록 | 최대 100개, 같은 배열의 person ID 중복 금지 |
| 시각 | 시간대가 있는 UTC RFC 3339 관측 시각 |

## 이벤트와 이미지

| 이벤트 | 발생 기준 |
|---|---|
| `person_appeared` | 새 등장마다 한 번; 대표 크롭과 관측 포함 |
| `person_disappeared` | 마지막 감지 후 대기 시간 경과; 같은 추적 ID·세션 |
| `inference_stream_lost` | RTSP 열기·읽기 실패마다 장애 구간당 한 번 |
| `inference_stream_restored` | 장애 후 첫 프레임 수신 |

RTSP 단절을 퇴장으로 처리하지 않는다. `central_connection_*`는 Edge의 녹화 복구 이벤트이므로 여기서 생성하지 않는다. 등장 요청 예시:

```json
{
  "camera_id": "cam-001",
  "event_type": "person_appeared",
  "source_event_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "occurred_at": "2026-09-09T00:00:00Z",
  "person_id": "7",
  "confidence": 0.92,
  "snapshot_path": "cam-001/a.jpg",
  "metadata": {"tracking_session_id": "0123456789abcdef0123456789abcdef"},
  "object_observation": {
    "schema_version": 1,
    "tracking_session_id": "0123456789abcdef0123456789abcdef",
    "object_class": "person",
    "bbox": [100, 50, 300, 600],
    "frame_width": 1280,
    "frame_height": 720,
    "crop_path": "cam-001/a_crop.jpg",
    "annotated_snapshot_path": "cam-001/a_boxed.jpg"
  }
}
```

- 경로는 `/snapshots` 기준 상대 경로이며 최대 4096자다. 경로·심볼릭 링크 해석 후에도 저장소 내부여야 한다.
- 원본·크롭·박스 이미지는 같은 프레임의 JPEG다. 크롭에는 박스를 그리지 않는다. 쓰기를 끝낸 후 이벤트를 전송한다.
- `object_observation`은 person ID가 있는 등장 이벤트에만 넣으며 metadata와 같은 세션을 사용한다. 전역 ID는 Data가 연결한다.
- 크롭 저장 실패 시 추가 추론을 멈추고 재시도한다. 관측 없는 등장 이벤트로 대체하지 않는다.
- `source_event_id`는 소문자 16진수 32자리로 생성하고 재전송 때 유지한다. Data는 카메라와 이 ID로 중복을 제거한다.
- Data는 관측을 `metadata.object`에 저장하고 identity·analysis 작업을 생성한다. 원래 관측 시각은 재시도 중에도 유지한다.

### 전송 대기와 이미지 보호

이미지 저장 후 이벤트를 `/snapshots/.event-outbox.sqlite3`에 영속 기록한다. 기본 상한은 격리 항목을 포함해 10,000건·JSON 64 MiB다. 포화·저장 실패 시 카메라별 현재 이벤트를 보존하며 추가 생성을 멈춘다. 영속화 전 종료 손실은 상태·로그에 남긴다.

통신 오류는 같은 이벤트 ID로 재시도하고 HTTP 400·404·413·422는 격리한다. 전송 대기·격리·포화 대기 이미지와 이벤트 키를 다음 보호 목록에 포함하여 30초마다 원자 교체한다.

```json
{
  "schema_version": 2,
  "complete": true,
  "generated_at": "2026-09-09T00:00:00Z",
  "paths": ["cam-001/a.jpg", "cam-001/a_crop.jpg", "cam-001/a_boxed.jpg"],
  "events": [{"camera_id": "cam-001", "source_event_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
}
```

파일은 `/snapshots/.pending-observations.json`이다. 신규 참조는 큐 등록 확정 전에 보호하고 전달·큐 삭제 확정 후 해제한다. 포화 대기 참조도 별도 영속 보존한다.

목록은 64 MiB·이벤트 100,000개·경로 300,000개 이하다. 초과 시 `complete:false`와 빈 목록을 게시한다. 누락·손상·120초 경과·미완성 목록은 Data의 이벤트·이미지 정리를 보류시킨다.

## 최신 좌표

최대 초당 2회 최신 값만 전송한다. 사람 없음은 `objects:[]`다.

```json
{
  "tracking_session_id": "0123456789abcdef0123456789abcdef",
  "observed_at": "2026-09-09T00:00:00Z",
  "frame_width": 1280,
  "frame_height": 720,
  "objects": [{"person_id": "7", "bbox": [100, 50, 300, 600], "confidence": 0.92}]
}
```

Data는 더 새로운 시각만 반영한다. 과거 요청도 `accepted:true`로 응답할 수 있다. 앱 조회 시 3초 초과·미래 관측은 빈 목록과 `stale:true`가 된다. 좌표와 HLS 프레임은 정확히 동기화되지 않는다.

## Identity 작업

감지와 독립적으로 claim → 크롭 검증·특징 추출 → complete를 수행한다. 입력 구조는 [작업 입력](analysis-interface.md#작업-입력)과 같고 완료 URL에는 작업 `id`를 사용한다.

| 완료 필드 | 규칙 |
|---|---|
| `lease_id` | claim에서 받은 소문자 16진수 32자리 |
| `outcome` | `complete|unconfigured|retry|failed` |
| `identity_descriptor` | schema_version 1, 1~128자 space_id, 유한 실수 16~2048개의 features; L2 norm `1±0.001` |
| `global_person_id` | 기본 생략·null; 직접 ID 방식은 descriptor와 동시 사용 금지 |
| `metadata` | JSON 객체; Python 기본 직렬화 기준 65,536바이트 이하, NaN·Infinity 금지 |

descriptor는 `complete`에서만 사용한다. Data는 같은 특징 공간·차원을 비교하고 `metadata.identity`와 추적의 전역 ID를 갱신한다. 이미 연결된 추적의 ID 변경은 409다.

기본 OSNet은 준비된 ONNX에서 512차원 단위벡터를 추출한다. 특징 공간은 `osnet:<모델 SHA-256>:rgb256x128-imagenet-v1`이며 모델 변경 시에도 기존 추적 ID는 유지된다. 세부 전처리·매칭 조건은 [Preprocessing README](../../server/services/preprocessing/README.md#기본-osnet-모델과-data의-인물-연결)를 따른다.

임대는 5분, claim은 최대 5회다. `retry` 대기는 30·60·120·240초이며 만료·중복 완료는 HTTP 200 `accepted:false`다. 응답 유실 시 같은 lease·결과를 재전송한다. `unconfigured`는 명시적 requeue가 필요하며 `failed`는 자동 재시도하지 않는다.

## 상태와 인수

Data 연결 실패는 readiness 503이다. 모델 미준비·영상 단절·프레임 노후·전송 오류는 200 `degraded`일 수 있다. 카메라별 상태·모델 준비·프레임 시각, identity의 ready·stalled·last_error, 큐의 pending·rejected·waiting을 제공한다.

카메라·identity·상태 서버를 독립 실행하고 HTTP·RTSP·모델 호출에 시간 제한을 둔다. 시간 초과한 작업을 정리한 뒤 재시도하고 추적 재시작 시 세션을 갱신한다.

1. 구현·Dockerfile·Compose의 실행 명령과 healthcheck를 맞추고 [개발 배포](../deployment-guide.md#서버-코드-개발)에서 새 이미지를 실행한다.
2. 실제 영상의 등장·퇴장, 같은 프레임의 파일·좌표, 전역 ID 연결을 확인한다.
3. RTSP·Data 단절, 큐 포화, 재시작, 임대 만료·중복 완료와 보호 목록의 이미지 보존을 확인한다.
4. 장비·이미지·모델 해시, 동시 카메라 수, 지연·정확도를 기록한다.

정확한 필드 검증은 [객체 계약](../../lib/ai_cctv_core/contracts/objects.py), [이벤트 API](../../server/services/data/app/api/events.py), [작업 API](../../server/services/data/app/api/objects.py)를 따른다.
