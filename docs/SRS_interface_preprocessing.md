# Preprocessing 컨테이너 교체 규약

이 문서는 현재 서버의 다른 컨테이너를 변경하지 않고 `preprocessing`을 교체하기 위한 규약이다. 감지·카메라별 추적과 카메라 간 인물 연결(identity)이 모두 이 컨테이너의 책임이다. 구현 언어·모델·내부 스레드 구조는 자유다. 전체 흐름은 [구조 설명](architecture.md), 별도 metadata 분석은 [Analysis 규약](SRS_interface_analysis.md)을 따른다.

현재 감지는 YOLO/ByteTrack 구현이 있으며, 인물 연결은 `unconfigured`를 반환하는 블랙박스다. 연결되지 않은 사람에게 가짜 `global_person_id`를 만들지 않는다.

## 1. 실행 환경과 연결

| 항목 | 교체 시 유지할 계약 |
|---|---|
| 배포 단위 | Compose의 `preprocessing` 서비스, `internal` 네트워크. 호스트 공개 포트 없음 |
| Data API | `DATA_SERVICE_URL=http://nginx:8080/internal/data/v1`; 아래 HTTP 경로는 이 주소 뒤에 붙임 |
| 영상 입력 | `MEDIAMTX_RTSP_BASE_URL=rtsp://mediamtx:8554` + `/` + 카메라의 `stream_path`. Nginx를 거치지 않는 RTSP/TCP |
| 감지용 인증 | `X-Internal-Token: <DATA_INFERENCE_TOKEN>` |
| 인물 연결용 인증 | `X-Internal-Token: <DATA_IDENTITY_TOKEN>`; 감지 토큰과 서로 다른 값 |
| 영상 읽기 인증 | `MEDIA_READ_USERNAME`, `MEDIA_READ_PASSWORD`를 RTSP Basic 인증에 사용. 읽기 계정이며 게시 권한 없음 |
| 설정 | `AI_CCTV_CONFIG_FILE=/app/config/config.yaml`, 읽기 전용 |
| 이미지 | `SNAPSHOTS_ROOT=/snapshots`, 읽기·쓰기. Data와 같은 호스트 저장소 |
| 모델 | `/models`, 읽기 전용. 기본 `MODEL_PATH=/models/default.pt`; Compose는 `MODEL_FILE`로 파일명 지정 |
| 상태 확인 | 컨테이너의 `0.0.0.0:8000`에서 7절의 HTTP 경로 제공 |
| 파일 권한 | Compose의 UID/GID로 `/snapshots`에 쓸 수 있어야 함. SQLite·녹화 저장소 직접 접근 없음 |

토큰·비밀번호는 `preprocessing.env`로 전달하고 로그·결과 metadata에 넣지 않는다. RTSP URL에 인증 정보를 결합하는 구현은 사용자명·비밀번호를 URL 인코딩해야 한다. 기본 내부 HTTP와 RTSP에는 TLS가 없다. HTTP 클라이언트가 호스트의 외부 프록시 설정으로 내부 토큰을 보내지 않도록 한다.

현재 배포 설정과 호환하려면 `config.yaml`의 `inference` 설정을 읽고 환경변수를 우선 적용한다. 카메라 운영 목록은 설정 파일 대신 Data API에서 받는다.

| 환경변수 | YAML의 `inference` 항목 | 기본값·의미 |
|---|---|---|
| `INFERENCE_ENABLED` | `enabled` | `true`; 감지 실행 여부 |
| `MODEL_PATH` | `model_path` | `/models/default.pt`; 모델 위치 |
| `INFERENCE_DEVICE` | `device` | `auto`; 현재 구현은 `cpu`, `cuda`, `cuda:N`도 지원 |
| `INFERENCE_CONFIDENCE` | `confidence_threshold` | `0.4`; 감지 채택 기준, 0~1 |
| `ANALYSIS_FPS` | `analysis_fps` | `5`; **감지** 처리 빈도, 양수 |
| `DISAPPEAR_SECONDS` | `disappear_seconds` | `3`; 마지막 감지 후 사라짐으로 판단할 시간, 양수 |
| `CAMERA_REFRESH_SECONDS` | 없음 | `15`; 활성 카메라 재조회 주기 |

기존 이미지의 `DETECTION_PLUGIN`, `IDENTITY_PLUGIN`은 Python 구현의 선택 장치다. 컨테이너 전체 교체에서는 그 클래스나 함수 모양을 구현할 필요가 없다. 이미지·의존성·자체 healthcheck 명령은 교체할 수 있으며, Compose의 기존 healthcheck가 Python 명령인 점도 함께 조정한다.

## 2. 카메라 조회와 상태 보고

모든 JSON 요청은 `Content-Type: application/json`을 사용한다. 아래 네 경로에는 **감지용 토큰**을 사용하며, identity 토큰으로 호출하면 403이다.

| 요청 | 입력 | 성공 응답 |
|---|---|---|
| `GET /cameras/enabled` | 본문 없음 | 200, `{"items":[카메라 객체,...]}` |
| `PATCH /cameras/{camera_id}/status` | `{"status":"online"}` 또는 `{"status":"offline"}` | 200, 갱신한 카메라 객체 |
| `POST /events` | 4절의 이벤트 | 201, 저장한 이벤트 객체 |
| `PUT /cameras/{camera_id}/objects` | 5절의 최신 좌표 | 200, `{"accepted":true}` |

카메라 객체의 전체 형식은 다음과 같다. 감지에는 `camera_id`, `stream_path`가 필요하고, 나머지 운영 필드는 소비자가 무시할 수 있다.

```json
{"id":1,"camera_id":"cam-001","name":"entrance","stream_path":"cam-001","edge_device_id":null,"source_url":null,"enabled":true,"status":"offline","created_at":"2026-09-07T00:00:00Z","updated_at":"2026-09-07T00:00:00Z"}
```

`camera_id`와 `stream_path`는 현재 `^[a-z0-9][a-z0-9_-]{0,63}$` 형식이다. 활성 카메라는 최대 4대이며 목록에서 빠지면 해당 영상 처리를 중지한다. `source_url`로 Edge에 직접 접속하지 않는다. 목록 조회 실패가 이미 실행 중인 카메라 수신을 중단시켜서는 안 된다.

RTSP에서 첫 프레임을 받으면 `online`, 열기·읽기 실패 시 `offline`을 보고한다. API 자체는 `degraded`, `disabled`도 받지만, 사용자의 활성화 설정을 감지기가 임의로 변경하지 않는다. 현재 구현은 연결 실패 시 1초부터 최대 15초까지 재접속 간격을 늘린다.

## 3. 객체와 식별자

| 필드 | 규칙 |
|---|---|
| `person_id` | 카메라 내부 추적 ID인 1~256자 문자열. 숫자 ID도 문자열로 전송 |
| `tracking_session_id` | 소문자 16진수 32자. 프로세스 재시작·RTSP 재접속·추적기 초기화 때 새 값 생성 |
| 로컬 추적의 키 | `(camera_id, tracking_session_id, person_id)`; 카메라나 세션이 다르면 같은 번호라도 별개 추적 |
| `global_person_id` | 카메라 간 동일 인물 연결 결과인 1~256자 문자열. 미연결은 `null`; 실명·앱 사용자 ID가 아님 |
| `bbox` | 원본 프레임 픽셀의 정수 `[x1,y1,x2,y2]`, 왼쪽 위 기준. `0 ≤ x1 < x2 ≤ width`, `0 ≤ y1 < y2 ≤ height` |
| 프레임 크기 | `frame_width`, `frame_height` 각각 1~16384 |
| `confidence` | 감지 모델의 확신 정도, 0~1. 동일 인물 판정 확률과 구별 |
| 시각 | UTC를 나타내는 RFC 3339 문자열. 예: `2026-09-07T00:00:00.123Z`; 최신 좌표에는 시간대 필수 |

화면 밖 좌표는 경계 안으로 잘라내고 면적 없는 박스는 제외한다. 한 프레임은 최대 100개 객체, 중복 `person_id` 없음이다. `track_id`는 현행 입력 필드가 아니다.

처음 보인 추적에는 `person_appeared`를 한 번 보낸다. 계속 보이는 동안 반복 발행하지 않으며, 설정 시간 동안 보이지 않으면 `person_disappeared`를 한 번 보낸다. 사라진 후 재등장은 새 등장 이벤트지만, 같은 세션의 같은 ID를 다른 사람에게 재사용하면 안 된다. RTSP 단절을 퇴장으로 추정하지 않고 새 연결에서 추적 세션을 초기화한다.

## 4. 이벤트와 이미지

등장 이벤트의 완전한 요청 예시다. 경로의 파일을 **먼저 저장 완료한 뒤** 요청한다.

```json
{
  "camera_id": "cam-001",
  "event_type": "person_appeared",
  "occurred_at": "2026-09-07T00:00:00Z",
  "person_id": "7",
  "global_person_id": null,
  "confidence": 0.92,
  "snapshot_path": "cam-001/2026/09/07/original.jpg",
  "metadata": {"tracking_session_id":"0123456789abcdef0123456789abcdef"},
  "object_observation": {
    "schema_version": 1,
    "tracking_session_id": "0123456789abcdef0123456789abcdef",
    "object_class": "person",
    "bbox": [100,50,300,600],
    "frame_width": 1280,
    "frame_height": 720,
    "crop_path": "cam-001/2026/09/07/example_crop.jpg",
    "annotated_snapshot_path": "cam-001/2026/09/07/example_boxed.jpg"
  }
}
```

- 필수 입력은 `camera_id`, `event_type`, `occurred_at`이다. 사람 이벤트에는 `person_id`와 `metadata.tracking_session_id`를 함께 보낸다. 감지 단계는 `global_person_id`를 `null`로 두고, 전역 ID 결정은 6절의 identity 완료로 수행한다.
- `snapshot_path`는 원본 JPEG, `crop_path`는 해당 박스의 사람 영역 JPEG, `annotated_snapshot_path`는 같은 프레임에 해당 박스와 로컬 ID를 그린 JPEG다. 파일명은 예시이며 충돌하지 않게 생성한다.
- 경로는 `/snapshots` 기준 상대 경로이고 최대 4096자다. 절대 경로·드라이브 경로·저장소 밖으로 벗어나는 경로를 보내지 않는다. 크롭을 읽을 때도 실제 경로가 저장소 내부 파일인지 확인한다. 분석 입력 파일을 덮어쓰지 않는다.
- `object_observation`은 `person_appeared`와 비어 있지 않은 `person_id`가 있을 때만 허용한다. `schema_version`은 1, `object_class`는 `person`이며 `crop_path`는 필수다. `annotated_snapshot_path`는 생략하거나 `null` 가능하다. 정의되지 않은 관측 필드는 422로 거부한다.
- 원본 저장 실패는 `snapshot_path:null`, 크롭 저장 실패는 `object_observation:null`로 보고할 수 있다. 이때 이벤트는 유지되지만 관측이 없으면 identity·analysis 작업은 생성되지 않는다. 현재 관측은 등장마다 한 번이며 매 프레임 저장하지 않는다.
- 사라짐 이벤트는 `event_type:"person_disappeared"`, 같은 `person_id`·세션, 마지막 확신도와 현재 발생 시각을 보내고 이미지·관측은 `null`로 둔다.

성공 응답에는 `id`(정수), `created_at`, 요청의 기본 이벤트 필드, `recording_segment_id`, `recording_segment_ids`, `edge_event_id`가 포함된다. 관측은 응답 최상위가 아닌 `metadata.object`에 들어가며, Data는 `metadata.tracking_session_id`를 관측과 일치시키고 `metadata.identity`, `metadata.analysis`를 각각 `{"status":"pending"}`으로 설정한다. 감지기는 녹화 ID와 `edge_event_id`를 생략한다. Data가 관련 녹화·알림·두 객체 작업을 같은 트랜잭션으로 저장한다.

영상 소비 구간 장애는 `inference_stream_lost`, 프레임 수신 재개는 `inference_stream_restored` 이벤트다. 사람 ID·이미지 없이 `metadata.reason`을 넣고, 같은 장애의 반복 연결 시도마다 이벤트를 만들지 않는다. Edge→중앙 구간의 `central_connection_*` 이벤트를 대신 생성하면 녹화 복구 구간이 잘못 정해질 수 있다.

현재 감지 이벤트에는 Data 도착 전 내구 송신 대기열이 없다. 응답이 유실된 POST를 무조건 재전송하면 이벤트·푸시가 중복될 수 있으며, HTTP 실패 중 감지 이벤트 무손실은 보장하지 않는다. 이미지 파일 저장과 HTTP 이벤트 저장도 하나의 트랜잭션이 아니다.

## 5. 실시간 박스

`PUT /cameras/cam-001/objects`에 최대 초당 2회 최신 상태를 보낸다.

```json
{
  "tracking_session_id": "0123456789abcdef0123456789abcdef",
  "observed_at": "2026-09-07T00:00:00.123Z",
  "frame_width": 1280,
  "frame_height": 720,
  "objects": [{"person_id":"7","bbox":[100,50,300,600],"confidence":0.92}]
}
```

모든 필드가 필수이며 정의되지 않은 필드는 거부한다. 사람이 없으면 `objects:[]`를 보낸다. 객체별 `global_person_id`는 **전송하지 않는다**. Data가 identity 연결표를 조회해 앱 응답에 추가한다.

전송이 느리면 오래된 대기 좌표를 버리고 최신 것만 남긴다. Data는 기존 값보다 `observed_at`이 큰 요청만 반영하지만, 무시한 과거·동일 시각 요청에도 `accepted:true`를 반환한다. 미래 시각 또는 3초를 초과한 관측은 앱 조회 시 빈 목록과 `stale:true`가 된다. 시스템 시계를 맞추고 순서가 뒤바뀐 좌표를 보내지 않는다.

이 API는 최신 상태 전달용이며 HLS 영상 프레임과의 정확한 동기화를 보장하지 않는다. 영상 자체에 박스가 합성되는 것도 아니다. 동일 프레임을 확인할 때는 박스 스냅샷을 사용한다.

## 6. 전역 인물 연결 작업

아래 요청에는 **identity 토큰**만 사용한다. identity는 analysis 완료를 기다리지 않으며, 다른 단계의 API·이벤트 조회·DB 접근 권한이 없다.

| 요청 | 입력 | 응답 |
|---|---|---|
| `POST /object-jobs/identity/claim` | 본문 없음 | 200, `{"job":null}` 또는 아래 작업 |
| `POST /object-jobs/identity/{id}/complete` | 아래 완료 JSON | 200, `{"accepted":true}` 또는 `false` |
| `POST /object-jobs/identity/requeue-unconfigured` | 본문 없음 | 200, `{"requeued":수량}`; 최대 100건 |

claim 응답 예시:

```json
{
  "job": {
    "id": 21,
    "event_id": 10,
    "attempts": 0,
    "camera_id": "cam-001",
    "person_id": "7",
    "global_person_id": null,
    "occurred_at": "2026-09-07T00:00:00Z",
    "lease_id": "fedcba9876543210fedcba9876543210",
    "object_observation": {
      "schema_version":1,
      "tracking_session_id":"0123456789abcdef0123456789abcdef",
      "object_class":"person",
      "bbox":[100,50,300,600],
      "frame_width":1280,
      "frame_height":720,
      "crop_path":"cam-001/2026/09/07/example_crop.jpg",
      "annotated_snapshot_path":"cam-001/2026/09/07/example_boxed.jpg"
    }
  }
}
```

`id`는 작업 ID, `event_id`는 원래 등장 이벤트 ID다. `attempts`는 **이번 claim 직전** 실행 횟수여서 첫 응답은 0이다. 작업에는 전체 이벤트 metadata나 별도의 인물 검색 목록이 포함되지 않는다.

완료 요청 예시:

```json
{
  "lease_id": "fedcba9876543210fedcba9876543210",
  "outcome": "complete",
  "global_person_id": "global-42",
  "metadata": {"backend":"example-reid","version":"1"}
}
```

`lease_id`는 claim에서 받은 소문자 16진수 32자를 그대로 전달한다. `outcome`은 필수이고 다음 네 값만 허용한다. `metadata`는 생략 가능한 JSON 객체(기본 `{}`), NaN·Infinity 금지, Data의 `json.dumps(metadata, allow_nan=False)`를 UTF-8로 바꾼 기준 65,536바이트 이하이다. 이 직렬화는 한글을 `\uXXXX`로 바꾸고 구분자 공백을 포함하므로 전송한 압축 JSON 크기와 다를 수 있다. 추가 최상위 필드는 금지한다.

| outcome | 의미와 Data 동작 |
|---|---|
| `complete` | 처리 완료. `global_person_id`는 생략·null도 가능하며, 유효한 판정이 있을 때만 지정 |
| `unconfigured` | 아직 모델을 연결하지 않음. 전역 ID 없음, 자동 재시도하지 않음 |
| `retry` | 일시 장애. 전역 ID 없음, 실행 한도 내에서 다시 대기 |
| `failed` | 잘못된 크롭·복구 불가능한 입력 등. 전역 ID 없음, 자동 재시도하지 않음 |

Data는 완료 시 해당 이벤트의 `metadata.identity`만 `{"status":"complete","updated_at":"...","result":{...}}` 형태로 바꾸며, `result`가 제출한 `metadata`다. 다른 분석 결과를 덮어쓰지 않는다. `retry`는 저장 상태 `pending` 또는 한도 초과 시 `failed`로 표시된다. 후속 완료로 새 이벤트·추가 푸시는 생성하지 않는다.

전역 ID는 로컬 추적 키에 연결되며 같은 추적의 기존 이벤트·이후 사라짐 이벤트·실시간 박스에 반영된다. 한 로컬 추적에 이미 연결된 전역 ID와 다른 ID를 제출하면 409 `OBJECT_RESULT_CONFLICT`다. 여러 카메라·추적을 같은 전역 ID로 묶는 것은 허용한다. 연결을 취소하거나 이미 확정된 ID를 바꾸는 API는 없다.

임대와 장애 처리는 다음을 지킨다.

1. claim은 작업을 5분간 임대한다. 만료 시각은 응답에 없고 갱신 API도 없다. 작업을 미리 많이 가져와 대기시키지 말고 임대 안에 완료한다.
2. 프로세스 중단·완료 전송 실패 시 임대 만료 후 다시 실행될 수 있다. 작업 ID로 외부 부수 효과의 중복을 막는다. 같은 작업이라도 재claim한 `lease_id`는 달라진다.
3. 만료·다른 lease·이미 완료·없는 작업은 완료 요청에 200 `accepted:false`를 반환한다. 성공 코드만 보고 결과 반영으로 판단하지 않는다. 409는 ID 충돌 등 별도 규약 위반이다.
4. claim은 총 최대 5회이며, `retry` 지연은 실행 횟수에 따라 30·60·120·240초다. 5번째 실패는 `failed`다. 임대 만료를 통해 한도 초과가 확정되는 경우 작업 상태와 이벤트 metadata의 마지막 상태가 다를 수 있다.
5. 현재 참고 실행기는 모델을 120초 기다린 뒤 `retry`를 보고하고 해당 identity 작업자의 추가 수신을 멈춘다. 이미 실행 중인 모델 스레드는 강제 종료하지 못하므로 운영자가 컨테이너를 재시작한다. 교체 구현도 무제한 대기·작업 누적을 막고 감지는 계속해야 한다.
6. 모델 구현 후 `requeue-unconfigured`를 호출하면 미설정 작업을 다시 대기시키고 실행 횟수를 0으로 만든다. 크롭이 남아 있어야 하며, 100건보다 많으면 나눠 호출한다. 이벤트 metadata는 새 완료 결과가 오기 전까지 이전 상태일 수 있다. `failed` 일괄 재처리 API는 없다.

현재 프로토콜은 관측 크롭과 결과 저장을 제공한다. 인물 특징 벡터·검색 인덱스·갤러리를 저장하거나 조회하는 전용 Data API는 없다. 재식별 알고리즘이 추가 영속 저장을 필요로 하면 별도 설계가 필요하며, 현재 DB를 직접 열어 해결해서는 안 된다.

## 7. 준비 상태·오류·교체 확인

| 경로 | 현재 응답 의미 |
|---|---|
| `GET /health/live` | 200, `{"status":"alive","service":"preprocessing"}` |
| `GET /health/ready` | Data 목록 조회 불가 시 503 `{"detail":"Data Service is not ready"}`; 가능 시 200과 아래 상태 |
| `GET /internal/v1/status` | 200, 아래 상태에서 최상위 `status`만 제외 |

```json
{"status":"ready","data_ready":true,"last_error":null,"workers":{"cam-001":{"camera_id":"cam-001","state":"online","model_ready":true,"last_error":null,"last_frame_at":"2026-09-07T00:00:00Z"}},"identity":{"ready":true,"stalled":false,"last_error":null,"last_outcome":"unconfigured"}}
```

감지가 켜져 있는데 모델이 준비되지 않았거나 identity가 준비되지 않음·정지·오류 상태이면 `status:"degraded"`로 보고한다. identity 장애만으로 감지·영상 수신을 중지하지 않는다. `unconfigured`는 미구현 상태이며 HTTP 준비 성공이나 `ready`가 모델 완성을 의미하지 않는다. 카메라의 `state`와 `last_frame_at`도 확인해야 한다. 이 경로들은 현재 인증 없이 내부에서 사용하며 외부에 공개하지 않는다.

인물 연결 완료 응답이 `accepted:true`가 아니면 identity 상태의 `last_outcome="rejected"`, `last_error="COMPLETION_NOT_ACCEPTED"`로 표시한다. 이 거절만으로 수신을 중단하지 않으며 이후 작업이 정상 완료되면 오류를 해제한다. `rejected`는 진단값이며 완료 요청의 `outcome`에는 사용하지 않는다.

Data 오류는 `{"error":{"code":"...","message":"...","details":{}}}` 구조다(`details`는 검증 오류에서 배열 가능). 401은 없는·잘못된 토큰, 403은 범위가 다른 토큰, 404는 없는 카메라, 422는 필드·좌표·경로 오류, 409는 결과 충돌이다. 토큰 미설정은 503이며, 서버·통신 장애에는 제한된 재시도를 적용한다. Nginx가 직접 반환한 오류는 이 JSON 형식을 보장하지 않으며 전체 요청 본문은 Nginx의 2 MiB 제한도 따른다. 비밀값이나 모델 예외 원문을 결과에 싣지 않는다.

교체 완료 전에는 다음을 실제 연결로 확인한다.

- 활성 카메라 추가·중지, RTSP 인증·재접속, 새 세션 생성, 감지 실패 중 영상 수신 유지.
- 등장·사라짐 각 1회, 동일 프레임 크롭·박스, 빈 좌표·화면 경계·오래된 관측 처리.
- 토큰 교차 사용 403, 경로 이탈·잘못된 bbox 422, identity ID 충돌 409.
- identity/analysis의 완료 순서를 바꿔도 결과 공존, 동일 인물 ID가 다른 카메라에 연결되고 같은 로컬 추적에 전파.
- 중복 완료·임대 만료의 `accepted:false`, 재시도 한도, 모델 미설정 재처리, 작업 정지 중 감지 지속.
- 교체 이미지의 권한·마운트·8000 상태 확인과 모바일 박스 표시. 모델 정확도는 통신 계약 검사와 별도로 평가.

구현 근거: [Compose](../server/compose.yml), [Data API](../server/services/data/app/api/objects.py), [객체 스키마](../lib/ai_cctv_core/contracts/objects.py), [작업 저장 규칙](../server/services/data/app/database/repositories/objects.py). 설치·공통 확인은 [README](../README.md)를 따른다.
