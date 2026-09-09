# Preprocessing 컨테이너 교체 인수인계

이 문서는 새 `preprocessing` 컨테이너를 만들어 기존 서버에 연결하는 담당자를 위한 **임시 인수인계 문서**다. 언어와 모델은 자유이며, 아래 실행·HTTP·파일 규약을 유지한다. 교체 검증과 인수 후에는 실제 구현의 README와 테스트를 남기고 이 문서를 삭제한다. 지금은 삭제하지 않는다.

## 1. 처음 5분: 무엇을 만들 것인가

| 담당할 일 | 완료했을 때의 결과 |
|---|---|
| 사람 감지·카메라별 추적 | MediaMTX 영상을 읽어 사람별 로컬 ID와 원본 픽셀 좌표 생성 |
| 등장·사라짐과 대표 이미지 | 등장마다 이미지 파일을 먼저 저장하고 Data에 이벤트 전송 |
| 실시간 좌표 | Data에 최신 프레임의 박스·ID 전송. 앱이 영상 위에 표시 |
| 카메라 간 인물 연결(identity) | 크롭의 특징 벡터를 보고하고 Data가 영속 gallery에서 전역 ID 연결 |
| 실행·장애 처리 | 카메라·identity를 독립 실행하고 8000번 상태 API 제공 |

현재 감지·추적은 YOLO/ByteTrack, 기본 identity는 `OsNetIdentity`다. OSNet x0.25 MSMT17 combineall Re-ID 사전학습 가중치를 변환한 로컬 ONNX에서 512차원 특징을 추출하며, Data가 저장한 gallery와 비교해 전역 ID를 결정한다. 같은 사람임을 확정하는 신원 인증은 아니다. 이전 `LocalAppearanceIdentity`는 명시적 호환 플러그인으로 남고, `IdentityBlackBox`를 명시하면 `unconfigured`를 반환한다.

중앙 업무 SQLite·객체 작업 대기열·gallery·결과 병합은 Data, 영상 수신·녹화·HLS는 MediaMTX, 앱 API·푸시는 External, 옷 색상 등 metadata 분석은 별도 Analysis의 책임이다. Preprocessing은 Data DB·녹화 파일을 직접 열거나 Edge·앱·Firebase에 직접 연결하지 않는다. Data 전송 전 이벤트를 보존하는 로컬 SQLite outbox는 스냅샷 폴더에 별도로 둔다.

```text
Edge → MediaMTX → preprocessing 감지 → Data: 이벤트·최신 좌표
                         └→ /snapshots: 원본·크롭·박스 이미지
Data: identity 작업 → preprocessing 특징 추출 → Data: gallery 비교·ID 연결
Data: analysis 작업 → 별도 Analysis           → Data: 완료 결과
```

필요한 Data API는 다음 7개다. **경로는 모두 `DATA_SERVICE_URL` 뒤에 붙인다.** 본문이 있으면 `Content-Type: application/json`, 인증은 `X-Internal-Token` 헤더를 사용한다. 요청의 정의되지 않은 필드는 거부하므로 아래에 명시한 것만 보내고, 응답의 사용하지 않는 필드는 무시한다.

| 토큰 환경변수 | 메서드·경로 | 요청 → 성공 응답 |
|---|---|---|
| `DATA_INFERENCE_TOKEN` | `GET /cameras/enabled` | 본문·조회 조건 없음 → 200, `{"items":[...]}` |
| 위와 같음 | `PATCH /cameras/{camera_id}/status` | `{"status":"online"}` → 200, 갱신한 카메라 |
| 위와 같음 | `POST /events` | 4절 이벤트 → 201, 저장한 이벤트 |
| 위와 같음 | `PUT /cameras/{camera_id}/objects` | 5절 좌표 → 200, `{"accepted":true}` |
| `DATA_IDENTITY_TOKEN` | `POST /object-jobs/identity/claim` | 본문 없음 → 200, `{"job":null}` 또는 작업 1개 |
| 위와 같음 | `POST /object-jobs/identity/{job_id}/complete` | 6절 완료 결과 → 200, `{"accepted":true}` 또는 `false` |
| 위와 같음 | `POST /object-jobs/identity/requeue-unconfigured` | 본문 없음 → 200, `{"requeued":3}`; 한 번에 0~100건 |

감지·identity 토큰은 서로 바꿔 쓰지 않는다. identity 토큰에는 이벤트 조회·다른 단계 작업·관리 API 권한이 없다.

## 2. 컨테이너 실행 계약

### 네트워크·파일·설정

| 항목 | 유지할 값·조건 |
|---|---|
| 서비스·네트워크 | Compose `preprocessing`, `internal` 네트워크. 호스트 공개 포트 없음 |
| Data | `DATA_SERVICE_URL=http://nginx:8080/internal/data/v1`. 예: 목록 조회는 `http://nginx:8080/internal/data/v1/cameras/enabled` |
| 영상 | `MEDIAMTX_RTSP_BASE_URL=rtsp://mediamtx:8554` + `/` + 카메라의 `stream_path`, RTSP/TCP |
| 인증 | `DATA_INFERENCE_TOKEN`, `DATA_IDENTITY_TOKEN`, `MEDIA_READ_USERNAME`, `MEDIA_READ_PASSWORD`를 배포 비밀 파일에서 받음 |
| 설정 | `AI_CCTV_CONFIG_FILE=/app/config/config.yaml` ← 호스트 `CONFIG_FILE`, 읽기 전용 |
| 이미지 | `SNAPSHOTS_ROOT=/snapshots` ← 호스트 `SNAPSHOTS_DIR`, 읽기·쓰기. Data·Analysis와 같은 폴더 공유 |
| 모델 | `/models` ← 호스트 `MODELS_DIR`, 읽기 전용. 감지는 `MODEL_PATH=/models/${MODEL_FILE}`(`MODEL_FILE` 기본 `default.pt`), identity 기본 파일은 `/models/osnet_x0_25_msmt17.onnx` |
| 사용자 | Compose의 `AI_CCTV_UID:AI_CCTV_GID`, 기본 `1000:1000`. 이 권한으로 이미지 쓰기·다른 처리기의 읽기가 가능해야 함 |
| 상태 서버 | `0.0.0.0:8000`, 아래 3개 경로. 인증 없이 컨테이너 내부망에서 조회 |

기본 비밀 파일은 `server/secrets/preprocessing.env`이며 `PREPROCESSING_SECRETS_FILE`로 바꾼다. 기존 생성 파일을 사용하고 Data·MediaMTX 쪽 인증값과 맞춘다. 현재 실행기는 identity 토큰과 영상 읽기 비밀번호를 각각 최소 32자로 검사한다. 감지 토큰의 구형 이름 `INTERNAL_SERVICE_TOKEN`은 기존 Python 실행기의 호환용이며 새 배포에는 `DATA_INFERENCE_TOKEN`을 사용한다.

RTSP 기본 주소에는 인증정보·query·fragment를 넣지 않는다. 읽기 계정을 URL에 넣는 클라이언트는 사용자명·비밀번호를 각각 URL 인코딩하고 인증 URL을 로그에 남기지 않는다. 내부 HTTP·RTSP에는 기본 TLS가 없으며 HTTP 클라이언트는 외부 프록시 환경변수를 따르지 않는다. 비밀값은 이미지·metadata·로그에 포함하지 않는다.

기존 설정 우선순위는 **컨테이너 환경변수 → YAML의 `inference` → 기본값**이다. `inference`와 `ANALYSIS_FPS`는 여기서 수행하는 감지 설정 이름이다.

| 환경변수 | YAML 키 | 기본값·동작 |
|---|---|---|
| `INFERENCE_ENABLED` | `enabled` | `true`; `false`이면 감지를 생략하고 영상 연결 감시는 유지 |
| `MODEL_PATH` | `model_path` | Compose가 위 모델 경로를 지정하므로 YAML보다 우선 |
| `INFERENCE_DEVICE` | `device` | `auto`; 기존 구현은 `cpu`, `cuda`, `cuda:N` 지원 |
| `INFERENCE_CONFIDENCE` | `confidence_threshold` | `0.4`, 0~1 |
| `ANALYSIS_FPS` | `analysis_fps` | `5`, 양수; YAML 최대 30 |
| `DISAPPEAR_SECONDS` | `disappear_seconds` | `3`, 양수; 마지막 감지 후 사라짐 대기 시간 |
| `CAMERA_REFRESH_SECONDS` | 없음 | `15`, 활성 목록 재조회 간격. 양수 사용 |
| `IDENTITY_PLUGIN` | 없음 | `server.services.preprocessing.processors.identity:OsNetIdentity` |
| `IDENTITY_MODEL_PATH` | 없음 | 미설정·정확히 빈 문자열이면 `/models/osnet_x0_25_msmt17.onnx`. `/models` 안의 OSNet ONNX 사용 |
| `OBJECT_MODEL_TIMEOUT_SECONDS` | 없음 | `120`, `0 < 값 ≤ 240`; identity 1회 호출 제한 |
| `OBJECT_STARTUP_TIMEOUT_SECONDS` | 없음 | `30`, `0 < 값 ≤ 120`; identity 팩토리 초기화 제한 |
| `RTSP_TIMEOUT_SECONDS` | 없음 | `5`, 최대 30초; 열기·읽기 각각의 제한 |
| `MODEL_RETRY_SECONDS` | 없음 | `30`; 감지 모델 재준비 간격 |
| `DETECTION_STARTUP_TIMEOUT_SECONDS` | 없음 | `30`, 최대 120초; 감지 자식 초기화·통신 제한 |
| `DETECTION_MODEL_TIMEOUT_SECONDS` | 없음 | `10`, 최대 60초; 감지·reset 및 Pipe 전송·수신 전체 제한 |
| `OBSERVATION_WINDOW_SECONDS` | 없음 | `1`, 0~5초; 등장 뒤 대표 프레임 선택 창. 0이면 즉시 확정 |
| `OBSERVATION_BUFFER_MAX_BYTES` | 없음 | `33554432`; 카메라별 대표 프레임 후보 메모리 상한 |
| `DETECTION_SHUTDOWN_SECONDS` | 없음 | `15`, 최대 60초; 감독자의 공통 종료 제한. Compose 종료 유예는 75초 |
| `EVENT_OUTBOX_MAX_PENDING` | 없음 | `10000`; 거부 보관 항목을 포함한 큐 최대 건수 |
| `EVENT_OUTBOX_MAX_BYTES` | 없음 | `67108864`; 큐 JSON 바이트 합 상한. SQLite 실제 파일 크기와 다름 |

`server/.env`에 쓴 값이 모두 컨테이너에 전달되는 것은 아니다. `MODEL_FILE`은 Compose에 연결되어 있고, 감지 빈도 등은 기본적으로 YAML을 편집한다. 환경변수로 덮어쓰려면 해당 서비스의 `environment`에도 연결한다. GPU는 장치 문자열 외에 호스트 드라이버·컨테이너 GPU 접근 설정이 필요하다. 기존 YOLO는 클래스 0을 사람으로 해석하며, 다른 모델은 클래스·전처리·좌표 복원을 직접 맞춘다.

모델 준비는 [변환 도구 안내](../server/tools/README.md)를 따른다. 저장소 루트의 `python server/tools/prepare_osnet.py`는 공식 가중치를 받아 기본 `server/runtime/models/osnet_x0_25_msmt17.onnx`에 변환한다. `--output`으로 실제 `MODELS_DIR` 안의 파일을 지정할 수 있다. CPU PyTorch와 변환 의존성은 준비 도구에서만 필요하며 서비스는 모델을 내려받지 않는다. YOLO 파일과 OSNet 파일을 각각 준비한다. 아래 6절의 `IDENTITY_MATCH_THRESHOLD`·`IDENTITY_MATCH_MARGIN`은 preprocessing이 아닌 **Data 서비스 환경변수**다.

설치에 넘기는 탐지·identity 파일명은 대소문자를 무시해도 서로 달라야 한다. 예를 들어 `MODEL.onnx`·`model.onnx` 조합은 Windows에서 같은 설치 대상으로 겹치므로 초기화 전에 거부된다. 탐지 `.pt`·`.onnx`·TensorRT `.engine`과 identity ONNX는 각 파일의 SHA-256을 릴리스 기록에 남기며, 확장자 지원 자체가 해당 장비의 추론 호환성을 보증하지는 않는다. 설치 패키지는 실제 비밀 파일과 `.bak`를 제외하고 필요한 예제만 포함한다. 설치본의 [운영·복구 안내](operations.md)와 함께 모델 경로·해시를 확인한다.

### 상태 API

| 경로 | 상태 코드·본문 |
|---|---|
| `GET /health/live` | 200, `{"status":"alive","service":"preprocessing"}` |
| `GET /health/ready` | 감지용 카메라 목록 조회 실패 시 503, `{"detail":"Data Service is not ready"}`; 그 외 200과 아래 상태 |
| `GET /internal/v1/status` | 200, 아래 JSON에서 최상위 `status`만 제외 |

다음 예시는 감지와 기본 identity 처리 경로가 준비되고 특징 처리 한 건을 완료한 상태다.

```json
{
  "status": "ready",
  "data_ready": true,
  "last_error": null,
  "workers": {
    "cam-001": {
      "camera_id": "cam-001",
      "state": "online",
      "model_ready": true,
      "last_error": null,
      "last_frame_at": "2026-09-09T00:00:00.300Z"
    }
  },
  "identity": {
    "ready": true,
    "stalled": false,
    "last_error": null,
    "last_outcome": "complete",
    "backend": "server.services.preprocessing.processors.identity:OsNetIdentity",
    "model_ready": true
  }
}
```

`data_ready`는 최근 목록 조회 결과다. `workers`는 카메라별 상태이며 없으면 `{}`, `state`는 `starting|online|offline|stopped`, 프레임 시각·오류는 없으면 `null`이다. `identity.ready`는 claim 가능 여부, `stalled`는 안전한 재시작이 불가능해 추가 수신을 중단했는지 나타낸다. `last_outcome`은 `complete|retry|failed|unconfigured|rejected|null`이다. `rejected`는 완료 거부의 진단값이며 완료 요청에 보내는 값은 아니다.

`identity.backend`는 선택 플러그인, `model_ready`는 그 실행 경로의 준비 상태다. 외관 특징의 `complete`나 임계값 이상 cosine 점수만으로 같은 사람임이 증명되지는 않는다. outbox의 대기·거부·포화와 종료 시 미보존 이벤트도 카메라·감독자 상태에서 함께 확인한다.

감지가 켜진 카메라의 모델 미준비·offline·stopped·오래된 프레임, identity 미준비·정지·오류, 이벤트 보존·송신 오류와 거부 항목은 200 `degraded`로 표시한다. 카메라별 `frame_age_seconds`는 마지막 프레임 수신 후 단조 시계 경과 시간이며 미수신이면 `null`이다. 상태 조회는 `max(30초, RTSP_TIMEOUT_SECONDS×2)` 이상 경과를 `frame_stale:true`로 계산한다. 첫 프레임 이전은 작업자 생성 시점부터 같은 유예시간을 사용한다. **HTTP 200·Compose healthy만으로 모델이나 영상의 성공을 판정하지 말고 실제 출력을 확인한다.**

## 3. 카메라·추적·좌표 공통 규칙

활성 목록의 `items`에서 감지기가 필요한 값은 문자열 `camera_id`, `stream_path`다. 숫자 `id`는 DB 번호이므로 URL에 쓰지 않는다. `source_url`은 사용하지 않으며, 사용자별 목록을 만드는 `user_id` 조회 조건도 보내지 않는다. 카메라는 최대 4대이고 빈 목록은 `{"items":[]}`다.

추가된 카메라는 시작하고 빠진 카메라는 중지한다. **목록 조회 실패는 빈 목록과 다르다.** 기존 카메라를 유지하며 재조회하고 상태에 장애를 표시한다. 첫 프레임을 받아야 `online`, 영상 열기·읽기 실패는 `offline`이다. 상태 PATCH의 허용값은 `online|offline|degraded|disabled`이며 본문은 `status`만 보낸다. 감지기가 `enabled`나 사용자 설정을 바꾸지 않는다.

현재 Python 작업자는 실행 중 `stream_path` 변경을 즉시 적용하지 않는다. 이 경우 비활성화 → 목록 갱신으로 작업자 종료 확인 → 재활성화 또는 컨테이너 재시작으로 반영한다. 새 구현이 경로 변경을 자동 반영한다면 추적 세션도 새로 시작한다.

| 값 | 형식·한계 |
|---|---|
| `camera_id`, `stream_path` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `person_id` | 카메라 내부 추적 ID, 1~256자 문자열. 숫자도 `"7"`처럼 전송 |
| `tracking_session_id` | 소문자 16진수 32자 |
| `global_person_id` | 여러 관측을 같은 사람으로 묶는 1~256자 문자열 또는 `null`. 실명·앱 사용자 ID가 아님 |
| `bbox` | 정수 배열 `[x1,y1,x2,y2]`, 원본 프레임 왼쪽 위가 원점. `0 ≤ x1 < x2 ≤ frame_width`, `0 ≤ y1 < y2 ≤ frame_height` |
| 프레임 크기 | `frame_width`, `frame_height` 각각 정수 1~16384 |
| `confidence` | 감지 확신도 0~1, 재식별 확률과 구별 |
| 시각 | 시간대가 있는 UTC RFC 3339, 예: `2026-09-09T00:00:00.123Z` |

추적의 식별자는 **`(camera_id, tracking_session_id, person_id)`**다. 프로세스 재시작·RTSP 재접속·추적기 초기화마다 세션을 새로 만들고, 같은 세션의 같은 ID를 다른 사람에게 재사용하지 않는다. 프레임당 최대 100명, 같은 배열의 ID 중복은 금지한다. 모델 좌표는 원본 크기로 복원해 경계 안으로 자르고 면적 없는 박스는 버린다.

## 4. 등장·사라짐 이벤트와 파일

`POST /events`의 필수 필드는 `camera_id`, `event_type`, `occurred_at`이다. 사람 이벤트에는 추가로 비어 있지 않은 `person_id`와 `metadata.tracking_session_id`를 넣는다. 범용 Data API가 이를 모두 강제하지 않더라도 송신자가 지킬 규약이다.

| 이벤트 | 발생 시점·추가 값 |
|---|---|
| `person_appeared` | 새로 보인 추적마다 1회. 확신도와 같은 프레임의 원본·크롭·박스 이미지, `object_observation` 포함 |
| `person_disappeared` | 마지막 감지 후 대기 시간이 지나면 1회. 같은 ID·세션, 마지막 확신도, 사라짐 판단 시각. 이미지·관측은 생략 또는 `null` |
| `inference_stream_lost` | 영상 열기·읽기 실패 시 1회. 사람·이미지 없이 `metadata.reason`에 `rtsp_open_failed` 또는 `rtsp_read_failed` |
| `inference_stream_restored` | 위 장애 후 첫 프레임 수신 시 1회. `metadata.reason:"rtsp_stream_available"` |

계속 보이는 동안 등장 이벤트를 반복하지 않고, 사라짐 후 다시 보이면 새 등장 이벤트를 만든다. RTSP 단절을 퇴장으로 추정하지 않으며 같은 장애의 재접속 시도마다 이벤트를 쌓지 않는다. **`central_connection_*`는 Edge→중앙 장애·녹화 복구용이므로 여기서 만들지 않는다.**

등장 요청 예시다. 예시 날짜는 형식 확인용이며 실제 전송에는 현재 관측 시각과 **저장을 끝낸 실제 파일 경로**를 사용한다.

```json
{
  "camera_id": "cam-001",
  "event_type": "person_appeared",
  "source_event_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "occurred_at": "2026-09-09T00:00:00.123Z",
  "person_id": "7",
  "confidence": 0.92,
  "snapshot_path": "cam-001/a.jpg",
  "metadata": {
    "tracking_session_id": "0123456789abcdef0123456789abcdef"
  },
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

`confidence`, `snapshot_path`, `object_observation`의 API 기본값은 `null`, `metadata`는 `{}`다. 감지 단계의 `global_person_id`는 생략 또는 `null`로 두고 Data가 identity 완료를 반영할 때 결정한다. `source_event_id`는 이벤트 생성 시 한 번 만든 소문자 16진수 32자리 UUID이며 재전송에도 유지한다. 녹화 연결 필드 `recording_segment_id(s)`와 Edge 중복 식별용 `edge_event_id`는 보내지 않는다.

`object_observation`은 `person_appeared`이고 `person_id`가 있을 때만 허용한다. 필수값은 세션·bbox·원본 크기·`crop_path`이며, `schema_version`은 기본 1만, `object_class`는 기본 `person`만 허용한다. `annotated_snapshot_path` 기본값은 `null`이다. 세 경로의 길이는 최대 4096자이고 `crop_path`는 비어 있을 수 없다. 관측과 이벤트 metadata의 세션은 같아야 한다.

이미지 바이트는 HTTP 본문에 넣지 않고 파일 경로만 전달한다. 파일 계약은 다음과 같다.

1. `snapshot_path`는 원본 JPEG, `crop_path`는 **같은 원본에서 해당 bbox만 자른 JPEG**, `annotated_snapshot_path`는 복사본에 해당 박스·로컬 ID를 그린 JPEG다. 크롭에 다른 사람의 박스 표시를 섞지 않는다.
2. 경로는 `/snapshots` 기준 상대 경로다. 절대 경로·Windows 드라이브·`..`를 통한 이탈을 금지하고 심볼릭 링크 해석 후에도 저장소 안인지 확인한다.
3. 충돌하지 않는 파일명으로 쓰기·닫기를 마친 뒤 이벤트를 보낸다. 임시 파일 작성 후 같은 저장소에서 이름을 바꾸는 방식을 권장한다. 다른 작업자가 읽는 파일을 임의로 덮어쓰거나 지우지 않는다.
4. 저장 실패를 가짜 경로로 숨기지 않는다. 원본 실패 시 `snapshot_path:null`, 박스 이미지 실패 시 해당 경로만 `null`일 수 있다. 크롭 저장 실패는 현재 프레임을 유지하고 추가 추론을 중단한 채 0.5~5초 간격으로 재시도한다. 성공 전에 관측 없는 등장 이벤트로 대체하지 않는다. 원본 스냅샷은 처음 한 번만 만들며 `observation_error`·`observation_persistence_failures`로 실패를 드러낸다. 종료까지 저장하지 못하면 `event_shutdown_losses`에 기록한다. identity는 파일 존재·저장소 경계·이미지 디코딩을 다시 검사한다.

201 응답은 저장된 이벤트 객체다. 필요한 값은 양의 정수 `id`이며, 전송한 관측은 최상위가 아닌 `metadata.object`에 저장된다. Data가 `metadata.identity`, `metadata.analysis`를 각각 `status:pending`, `updated_at:UTC시각`, `result:{}`인 객체로 만들고 이벤트·녹화 연결·푸시 예약·두 작업을 같은 DB 트랜잭션에 저장한다. 이미지 저장은 이 트랜잭션에 포함되지 않는다.

crop 저장이나 이벤트 전송 재시도에 시간이 걸려도 등장·사라짐의 `occurred_at`은 원래 프레임 수신 시각을 유지한다. 완료 시각으로 바꾸어 영상·추적·gallery의 시간 관계를 왜곡하지 않는다.

현재 구현은 이미지 저장 후 이벤트 JSON을 `SNAPSHOTS_ROOT/.event-outbox.sqlite3`에 먼저 기록하고 독립 송신 흐름에서 전달한다. Data는 `(camera_id, source_event_id)`의 유일성으로 같은 이벤트의 재전송에 기존 결과를 돌려주며 작업·푸시를 중복 생성하지 않는다. `edge_event_id`와는 별도의 중복 제거 규칙이다. outbox는 재시작 후에도 남고, 응답 유실·일시 장애는 0.5~30초 간격으로 재시도한다. HTTP 400·404·413·422는 거부 항목으로 보관한 뒤 다음 항목을 처리한다.

큐는 거부 항목을 포함해 기본 10,000건·JSON 합 64 MiB로 제한된다. 페이지·WAL 오버헤드를 포함한 SQLite 파일 크기는 더 클 수 있다. 무제한 무손실 큐가 아니므로 디스크 오류·큐 포화·종료 시 보존하지 못한 이벤트를 상태에서 확인해야 한다. 재연결 때 최신 좌표는 이 대기열로 복원하지 않고 최신 상태만 다시 전송한다.

큐가 차거나 쓰기가 실패하면 카메라별 현재 이벤트 한 건·스냅샷을 유지한 채 추가 추론과 스냅샷 생성을 멈추고 0.5~5초 간격으로 같은 ID의 영속화를 재시도한다. `event_backpressure`·`event_delivery_error`·`event_persistence_failures`와 `event_delivery.pending/rejected/waiting/last_error`를 확인한다. `waiting`은 포화 때문에 아직 큐에 등록하지 못해 이미지 참조만 보호 중인 카메라 수다. SQLite를 조회할 수 없으면 건수는 `null`, 오류는 `EVENT_STORAGE`다. 종료는 이 대기를 깨며 아직 영속화하지 못한 건은 `event_shutdown_losses`와 ERROR 로그로 남긴다. 남은 스냅샷만으로 재시작 후 이벤트 복원을 보장하지 않는다. 큐 상한에는 JPEG 파일 총용량과 카메라별 대기 참조 행이 포함되지 않는다.

### 보존 정리와 공유하는 이미지 보호 목록

새 컨테이너도 `/snapshots/.pending-observations.json`을 같은 폴더의 임시 파일 작성·flush·원자 교체로 게시해야 한다. 큐의 미전송·격리 항목뿐 아니라 **포화로 아직 등록하지 못한 현재 관측의 경로**도 포함한다. 기존 구현은 이 대기 참조를 카메라별 한 행으로 outbox SQLite에 별도 보존한다. 공간이 생겨 먼저 전송한 항목이 삭제되거나 다른 인스턴스가 목록을 갱신해도 대기 참조는 남으며, 같은 카메라의 다음 큐 등록이 성공하면 해제한다. 재시작 후 대기 참조는 보호하지만 메모리에만 있던 이벤트 본문을 복원하는 것은 아니다.

활성 카메라 목록을 정상 조회한 뒤 실제 생산자도 종료된 카메라의 고아 대기 참조는 해제한다. 중지 요청 뒤에도 생산자가 살아 있거나 목록 조회가 실패하면 보호를 유지한다. 참조 삭제를 먼저 확정한 뒤 보호 목록을 갱신하며, 큐의 미전송·거부 이벤트나 JPEG 파일을 이 단계에서 직접 삭제하지 않는다.

```json
{
  "schema_version": 2,
  "complete": true,
  "generated_at": "2026-09-09T00:00:00.000Z",
  "paths": ["cam-001/crop.jpg"],
  "events": [{"camera_id": "cam-001", "source_event_id": "0123456789abcdef0123456789abcdef"}]
}
```

`paths`에는 원본·crop·박스 이미지의 비어 있지 않은 상대 경로를 모두 넣고, `events`에는 `(camera_id, source_event_id)` 중복 제거 키를 넣는다. 신규 참조는 이벤트 DB 확정 전에 보호하고, 완료 참조는 전달 및 큐 삭제 확정 후 뺀다. 30초마다 갱신한다. `generated_at`은 UTC이며 Data 기준 미래 5초 초과 또는 120초 초과 경과한 목록은 사용하지 않는다.

보호 목록은 UTF-8 64 MiB, 이벤트 100,000개, 경로 300,000개 이하로 유지한다. 대기 참조까지 포함하면 상한을 넘는 경우 **목록 일부를 자르고 정상이라고 표시하지 않는다.** `complete:false`, 빈 `paths/events`, 최신 `generated_at`인 작은 표식을 게시해 Data의 이벤트·이미지 정리를 보류한다. 누락·손상·노후 목록도 같은 보류 정책이며 녹화 정리는 별도다. 새 구현은 `schema_version:2`와 불리언 `complete`를 반드시 명시한다. Data의 호환 판독기는 `complete`가 없는 기존 v1도 읽는다. v1만 아는 구형 Data는 v2를 거절해 정리를 보류하므로, 업데이트 순서가 달라도 불완전한 빈 목록으로 삭제하지 않는다. 이미지 생성 직후 최소 24시간의 정리 유예만으로 장기간 큐 포화가 안전하다고 가정하지 않는다.

## 5. 최신 박스 전송

`PUT /cameras/{camera_id}/objects`는 영상과 별도로 앱에 표시할 최신 상태를 보낸다. **최대 초당 2회**, 느릴 때는 최신 값만 남기고 밀린 좌표를 계속 쌓지 않는다. 다음 필드는 모두 필수이고 `null`은 허용하지 않는다.

```json
{
  "tracking_session_id": "0123456789abcdef0123456789abcdef",
  "observed_at": "2026-09-09T00:00:00.123Z",
  "frame_width": 1280,
  "frame_height": 720,
  "objects": [
    {"person_id": "7", "bbox": [100, 50, 300, 600], "confidence": 0.92}
  ]
}
```

사람이 없으면 `objects:[]`를 보내며, 객체 항목에는 위 3개 필드만 넣는다. `global_person_id`는 Data가 연결표에서 조회해 앱 응답에 추가한다.

Data는 저장된 값보다 `observed_at`이 큰 요청만 반영한다. 과거·동일 시각 요청을 무시해도 응답은 200 `{"accepted":true}`다. 앱 조회 시 관측이 미래이거나 **3초 초과**, 카메라 비활성 또는 저장값 없음이면 `objects:[]`, `stale:true`가 된다. 시계를 맞추고 관측 시각을 처리 완료·전송 시각으로 바꾸지 않는다.

HLS 영상과 좌표는 프레임 단위로 동기화되지 않으며 영상 자체에 박스가 합성되는 것도 아니다. 같은 프레임의 결과 확인에는 저장한 박스 이미지를 사용한다.

## 6. identity: 작업 가져오기와 결과 보고

### 작업과 완료 본문

감지와 독립된 처리기에서 `claim → 크롭 검증·모델 실행 → complete`를 반복한다. 한 번에 처리 가능한 작업만 가져온다. `job:null`은 현재 작업 없음이며 잠시 기다렸다가 다시 조회한다.

claim의 `job`에는 다음 값이 들어온다. `id`·`event_id`는 양의 정수로 **완료 경로에는 이벤트 ID가 아닌 작업 `id`**를 넣는다. `attempts`는 이번 claim 직전 횟수(0~4)이고 `lease_id`는 이번 처리 권한이다.

```json
{
  "job": {
    "id": 21,
    "event_id": 10,
    "attempts": 0,
    "camera_id": "cam-001",
    "person_id": "7",
    "global_person_id": null,
    "occurred_at": "2026-09-09T00:00:00.123Z",
    "object_observation": {
      "schema_version": 1,
      "tracking_session_id": "0123456789abcdef0123456789abcdef",
      "object_class": "person",
      "bbox": [100, 50, 300, 600],
      "frame_width": 1280,
      "frame_height": 720,
      "crop_path": "cam-001/a_crop.jpg",
      "annotated_snapshot_path": "cam-001/a_boxed.jpg"
    },
    "lease_id": "abcdef0123456789abcdef0123456789"
  }
}
```

작업에는 전체 이벤트 metadata·원본 `snapshot_path`·다른 인물 검색 목록이 없다. `object_observation.crop_path`를 `/snapshots` 안에서 읽는다. claim 시 이미 전역 연결이 있으면 `global_person_id`가 채워질 수 있다. **gallery는 Data 내부에 영속 저장되며 플러그인에 검색 API나 벡터 목록을 노출하지 않는다.** 기본 플러그인은 상태를 저장하지 않고 새 특징만 완료 요청에 담는다.

위 작업의 완료 요청은 `POST /object-jobs/identity/21/complete`에 같은 lease를 넣는다. 다음은 공통 descriptor 형식을 설명하는 16차원 단위벡터 예시이며, 실제 기본 OSNet 추출기는 모델 해시·전처리 버전으로 구분한 공간의 512차원 측정값을 반환한다. 예시 벡터는 OSNet 추론 결과가 아니다.

```json
{
  "lease_id": "abcdef0123456789abcdef0123456789",
  "outcome": "complete",
  "identity_descriptor": {
    "schema_version": 1,
    "space_id": "example-descriptor-v1",
    "features": [0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25]
  },
  "metadata": {"backend": "example-reid", "version": "1", "method": "example", "quality": {}}
}
```

필수 필드는 `lease_id`(소문자 16진수 32자), `outcome`이며 `global_person_id`·`identity_descriptor`의 기본값은 `null`, `metadata`는 `{}`다. descriptor는 `schema_version:1`, 1~128자의 `space_id`, 유한 실수 16~2048개인 `features`를 가지며 L2 norm은 `1±0.001`이어야 한다. `complete`인 identity 결과에서만 사용할 수 있고 명시 `global_person_id`와 동시에 보낼 수 없다. 기존 직접 ID 플러그인의 완료 방식은 호환용으로 유지한다.

metadata는 모델 결과 자체를 담는 JSON 객체로, `identity`로 다시 감싸지 않는다. 기본 OSNet은 backend·version·architecture·model_sha256·method·quality를 넣고 벡터를 복제하지 않는다. 내부 키는 자유지만 NaN·Infinity는 금지한다. **Python `json.dumps(metadata, allow_nan=False).encode("utf-8")` 기준 65,536바이트 이하**다. 기본 직렬화는 한글을 이스케이프하고 공백을 포함하므로 압축한 전송 크기와 다르다.

| `outcome` | 사용할 때 | 전역 ID·후속 처리 |
|---|---|---|
| `complete` | 정상 처리 | 기본 descriptor를 Data가 비교하거나 교체 플러그인의 명시 ID를 반영 |
| `unconfigured` | 명시한 블랙박스·교체 플러그인의 미설정 상태 | ID 없음, 자동 재시도 없음. 기본 OSNet 모델 누락은 초기화 오류로 구분 |
| `retry` | 일시적인 모델·의존성 장애 | ID 없음, 한도 내 재시도 |
| `failed` | 크롭 누락·손상·정보 없는 상수 영상, 복구 불가능한 입력·결과 | ID 없음, 자동 재시도 없음 |

Data는 해당 이벤트의 `metadata.identity`만 `{"status":저장상태,"updated_at":UTC시각,"result":제출metadata}` 형태로 갱신하며 object·analysis 결과는 보존한다. identity·analysis 완료 순서는 상관없고 후속 완료로 새 이벤트·추가 푸시를 만들지 않는다.

### 특징 추출과 gallery 비교

기본 `OsNetIdentity`는 [공식 OSNet 저장소](https://huggingface.co/kaiyangzhou/osnet)의 x0.25 MSMT17 combineall Re-ID 가중치를 변환한 ONNX를 사용한다. 학습 데이터 범위는 [공식 Model Zoo](https://kaiyangzhou.github.io/deep-person-reid/MODEL_ZOO)를 참고한다. `/models` 안의 256 MiB 이하 파일만 OpenCV DNN CPU로 실행하며, 기본 파일은 `osnet_x0_25_msmt17.onnx`다. `IDENTITY_MODEL_PATH`가 없거나 정확히 빈 문자열이면 이 기본 파일을 사용한다. 공백만 있는 값·누락·손상 모델은 초기화 오류이며 다운로드나 HSV 자동 대체는 하지 않는다.

입력은 RGB `float32` `1×3×256×128`, `[0,1]` 변환 뒤 ImageNet 평균 `[0.485,0.456,0.406]`·표준편차 `[0.229,0.224,0.225]`를 적용한다. 출력은 **단일 `float32 1×512` 특징 벡터**여야 하며 유한값·비영벡터 확인 후 L2 정규화한다. 공간명은 `osnet:<파일 SHA-256>:rgb256x128-imagenet-v1`이다. 초기화 때 시험 추론을 수행해 준비 상태 전에 실행·출력을 확인하며 그 시험 결과는 gallery에 보내지 않는다. 손상·어느 한 변이 16픽셀 미만인 크롭과 모든 픽셀의 BGR 값이 같은 상수 영상은 벡터 없이 실패한다. 어두움·흐림·낮은 대비에 대한 임의 거부 기준은 없으며, 이 검사가 크롭에 사람이 있음을 증명하지도 않는다.

이전 `LocalAppearanceIdentity`를 명시하면 모델 미설정 시 `appearance-hsv-v1`의 392차원 HSV·밝기·질감 특징, 모델 지정 시 16~2048차원 범용 ONNX 추출을 유지한다. 이 호환 경로와 기본 OSNet 계약을 혼동하지 않는다.

Data는 유효 lease를 확인한 트랜잭션 안에서 특징·추적 연결·이벤트·작업 완료를 함께 확정한다. 같은 공간·차원만 비교하며 인물별 최고 cosine 점수 중 1위가 기본 0.97 이상이고 2위와 차이가 기본 0.05 이상이어야 기존 ID로 연결한다. Data 환경변수 `IDENTITY_MATCH_THRESHOLD`·`IDENTITY_MATCH_MARGIN`으로 조정할 수 있다. 이 기본값은 기존의 보수적인 운영값을 유지한 것으로, **OSNet 또는 설치 현장 데이터로 교정된 기준이 아니다.** 후보가 없거나 낮거나 모호하면 새 `person-<uuid>`를 만든다. 같은 카메라의 다른 추적과 관측 구간이 겹치거나 등장 시각이 ±30초 안인 후보 ID는 제외한다. 등장·퇴장 이벤트와 최신 객체 관측 시각을 저장하여 지연된 작업에도 적용한다.

gallery는 최대 관측 시각을 기준으로 최근 1,800초·최대 5,000개 표본을 유지하며 추적 연결은 DB에 보존한다. 재시작 후에도 연결이 유지되지만 오래 지난 새 추적은 새 ID가 될 수 있다. 관련 이벤트가 없고 관측도 보관 기한을 지난 연결은 정리한다. gallery 기간·건수·동일 카메라 제외 시간은 [Data identity 저장소](../server/services/data/app/database/repositories/identity.py)의 상수다. 비공개 벡터는 공개 이벤트에 포함하지 않는다. Data가 추가하는 `metadata.identity.result.match` 예시는 다음과 같다. `method`는 OSNet 공간이면 `osnet`, 이전 특징 공간이면 `appearance`다. cosine은 동일인 확률이 아니며 비슷한 의복·가림·조명 변화에 의한 오연결·분리가 가능하다.

```json
{
  "method": "osnet",
  "decision": "new",
  "similarity": null,
  "threshold": 0.97,
  "margin": 0.05
}
```

`decision`은 `new|matched|existing_track`이며 기존 추적 또는 후보 없음의 `similarity`는 `null`이다. `threshold`·`margin`은 해당 처리에 사용한 설정값으로 기록한다.

기존 `(camera_id, tracking_session_id, person_id)` 연결은 **특징 공간이 바뀌어도 우선**한다. 같은 추적을 OSNet으로 다시 처리하면 기존 ID를 유지하고 그 ID 아래 OSNet 표본을 저장한다. 서로 다른 공간의 벡터는 비교하지 않지만 이전 오연결을 자동 교정하지도 않는다. 완료된 작업은 모델 교체만으로 다시 처리되지 않으며 전역 ID 초기화·교정 기능은 제공하지 않는다.

입력은 등장 뒤 기본 1초의 관측 창에서 고른 crop 한 장이다. 정보가 있는 영역·선명도·크기로 대표 프레임을 선택하고 특징 평균은 하지 않는다. 후보는 카메라당 기본 32 MiB로 제한하며 퇴장·재접속·상한 초과 때 조기에 확정한다. 이벤트 시각은 최초 등장을 유지하고 선택 시각·품질은 `metadata.observation_selection`에 기록한다. 모두 부적합하면 `insufficient`를 남기며 이후 작업에 같은 파일이 전달된다. 교체 모델도 이 입력 범위에서 실제로 판단할 수 있는 결과만 제출한다.

전역 ID는 같은 `(camera_id, tracking_session_id, person_id)`의 기존 이벤트와 이후 이벤트·최신 좌표에 연결된다. 한 추적에 이미 연결한 전역 ID를 다른 ID로 바꾸면 유효한 임대의 완료에서도 409 `OBJECT_RESULT_CONFLICT`다. 다른 세션·카메라의 추적을 같은 전역 ID로 묶는 것은 가능하다.

### 임대·재시도·중복의 한계

- Data의 임대는 **5분**, 갱신 API와 만료 시각 응답은 없다. 처리 가능한 시점에 claim하고 제한 안에 완료한다.
- claim은 총 최대 5회다. `retry`는 30·60·120·240초 대기 후 다시 가능하며 5번째는 `failed`가 된다. 작업자 중단·완료 전송 실패 후 임대가 만료되면 같은 작업에 새 lease가 발급될 수 있다.
- 같은 완료의 재전송·만료 lease·없는 작업은 **200 `{"accepted":false}`**다. HTTP 성공만으로 반영을 판단하지 않는다. 완료 응답 유실 시 원래 job·lease·본문으로 재전송할 수 있지만, `false`만으로 첫 요청의 수락 여부를 구별할 수는 없다. `last_outcome:rejected` 진단은 유지하되 이후 정상 빈 claim에서 해당 오류를 지워 유휴 상태를 영구 장애로 표시하지 않는다. 작업 상태 조회 API는 없다.
- 이전 작업자의 늦은 결과는 거부된다. 모델의 외부 부작용도 반복될 수 있으므로 필요하면 `job.id`로 중복을 구별한다. 거부된 결과를 임의 새 lease로 제출하지 않는다.
- 모델 연결 후 `requeue-unconfigured`로 한 번에 최대 100건의 시도 횟수를 0으로 되돌린다. 크롭이 남아 있어야 하고, `failed` 일괄 재처리 API는 없다. Data는 claim·재등록·최종 실패 시 이벤트 상태를 동기화하고 기존 상태 불일치도 조회 때 현재 작업 상태로 보정한다. 장기 미처리 작업은 기본 30일 정책으로 최종 실패 처리하되 유효한 실행 lease는 만료까지 보호한다.

현재 Python 실행기는 팩토리와 모델 호출을 별도 `spawn` 프로세스에서 실행한다. 초기화 기본 30초·호출 기본 120초를 각각 설정된 상한 안에서 제한하며 시간 초과한 자식을 종료한 뒤 재생성한다. 호출 시간 초과는 `retry/MODEL_TIMEOUT`, 초기화 실패는 30초 후 재시도한다. 자식 종료 실패는 정지 상태로 드러내어 중첩 실행을 막는다. 이 제한은 5분 임대와 별개다. 완료 HTTP 실패 때는 같은 결과·lease를 메모리에 보관해 재추론 없이 전송하되, 전체 프로세스가 중단되면 임대 만료 후 재처리될 수 있다.

## 7. 장애 처리와 구현·연결 순서

카메라별 추적·영상 수신, identity, 상태 HTTP 응답을 독립적으로 유지한다. 감지 모델 로드·실행 실패 후 영상 연결 감시는 계속하고 기본 30초 뒤 모델을 재준비한다. RTSP 열기·읽기 각각 기본 5초 제한을 적용하며 실패 시 캡처를 해제하고 1~15초 뒤 재연결한다. identity 초기화 실패는 별도로 30초 후 재시도한다. 재접속·모델 재준비 시 추적 세션을 새로 만들어 이전 로컬 ID와 혼동하지 않는다.

감지 모델은 카메라별 별도 `spawn` 자식에서 실행한다. 초기화 기본 30초·감지 및 reset 호출 기본 10초를 넘으면 자식을 종료한다. 모델 재준비 시 추적 세션을 갱신하며 시간 초과 횟수·최근 처리시간을 상태에 제공한다. 종료 요청도 자식을 정리하여 감지 스레드의 네이티브 호출이 무기한 대기하지 않게 한다.

Data 오류는 보통 `{"error":{"code":"...","message":"...","details":{}}}` 형태다. 검증 오류의 `details`는 `location` 배열·`message`·`type`을 가진 항목 배열이다. 판단은 오류 코드로 한다.

| 응답 | 처리 |
|---|---|
| 401 `INVALID_INTERNAL_TOKEN` / 403 `INTERNAL_SCOPE_FORBIDDEN` | 비밀 파일·헤더·감지/identity 역할 확인 |
| 404 `CAMERA_NOT_FOUND` | 활성 목록 재조회·작업 정리 |
| 409 `OBJECT_RESULT_CONFLICT` | 세션·전역 ID 판정 확인, 같은 충돌 반복 제출 금지 |
| 422 `VALIDATION_ERROR`, `INVALID_OBJECT_EVENT`, `INVALID_STORAGE_PATH` | 잘못된 필드·좌표·경로 수정 |
| 503 `INTERNAL_TOKEN_NOT_CONFIGURED` | Data와 호출자의 인증 설정 확인 |
| 500·연결 실패·시간 초과 | 대기 후 복구하되 이벤트 POST 수락 여부 불확실성과 작업 임대를 고려 |

Nginx의 413·502·504는 JSON을 보장하지 않으며 전체 요청은 **2 MiB** 제한을 따른다. 모든 HTTP 요청에 시간 제한을 둔다. 현재 일반 요청 10초·좌표 2초, identity의 빈 대기열·통신 실패 후 대기는 1초다.

종료 신호에서는 새 claim·카메라 시작을 멈추고 연결을 닫는다. 미완료 작업은 임대 만료로 회수되며 저장된 파일·Data 상태는 보존한다. Compose의 `restart: unless-stopped`는 프로세스 종료에 대한 정책으로 **unhealthy만으로 자동 재시작하지 않는다.**

### 구현하고 새 이미지로 연결하기

1. 설정·토큰·UID·공유 경로 검증과 상태 서버를 만든다.
2. 활성 카메라 조회, RTSP 수신, 세션과 카메라별 상태를 구현한다.
3. 감지·추적 → 최신 좌표 → 등장 이미지 선저장·이벤트 → 독립 identity 순서로 연결한다.
4. 시간 초과·종료·재시도·인수 테스트를 구현한다.
5. `server/services/preprocessing/`에 실제 구현·의존성 파일·Dockerfile을 둔다. Compose의 빌드 context는 저장소 루트이므로 Dockerfile `COPY`도 그 기준이다.

서비스 이름·네트워크·인증·마운트·8000 상태 API를 유지하고 해당 서비스의 이미지 빌드·실행 명령을 새 구현에 맞춘다. **현재 Compose 공통 healthcheck는 Python으로 `/health/ready`를 호출하므로 Python 없는 이미지에서는 이 명령도 교체한다.**

아래 Dockerfile은 **대상 장비 아키텍처에 맞는 정적 링크 Linux 실행 파일을 사전 빌드한 경우**의 최소 예시다. `bin/preprocessing`과 `serve`·`healthcheck` 하위 명령은 담당자가 구현해야 한다. 동적 라이브러리·언어 런타임이 필요한 구현은 해당 의존성과 빌드 단계를 추가한다.

```dockerfile
FROM debian:12-slim
WORKDIR /app
COPY --chmod=0555 server/services/preprocessing/bin/preprocessing /app/preprocessing
USER 65532:65532
ENTRYPOINT ["/app/preprocessing"]
CMD ["serve"]
```

`serve`는 상태 서버·카메라 처리·identity 루프를 시작한다. `healthcheck`는 지정 URL을 제한 시간 안에 조회해 HTTP 200일 때만 종료 코드 0을 반환한다. `.dockerignore`가 `build/`·`dist/`를 제외하므로 예시는 `bin/`을 사용한다. 실제 산출물이 빌드 context에 포함되는지 확인한다.

기존 preprocessing의 `image`를 인수 버전 태그(예: `ai-cctv-preprocessing:handoff-v1`)로 지정하고 **해당 서비스의 healthcheck만** 다음처럼 재정의한다. 공통 healthcheck는 다른 서비스에 그대로 둔다.

```yaml
preprocessing:
  healthcheck:
    test: ["CMD", "/app/preprocessing", "healthcheck", "http://127.0.0.1:8000/health/ready"]
    interval: 10s
    timeout: 5s
    retries: 6
    start_period: 20s
```

이 YAML은 기존 서비스에서 바꿀 부분만 보여 준다. 기존 `build`·`env_file`·`environment`·`volumes`·`depends_on`·`networks`·`user`를 보존하고 필요한 값만 맞춘다. 모델 로딩이 길면 healthcheck의 시작 유예를 조정한다. Compose의 실행 UID가 위 Dockerfile의 USER보다 우선한다.

개발 Compose까지 사용할 경우 `server/compose.dev.yml`의 preprocessing에 지정된 `development` 단계·Python 실행 명령·코드 마운트도 맞춘다. 아래는 기본 Compose만 쓰는 교체 확인 절차다.

[소스 배포 안내](guide.md#소스-배포)로 **운영과 분리된 개발 배포**를 준비한다. 별도 소스 사본·env·비밀 파일·DB·영상·스냅샷 경로·공개 포트를 사용한다. 프로젝트 이름만 바꿔서는 경로·포트가 분리되지 않는다. 카메라와 모델을 준비한 뒤 저장소 루트의 PowerShell에서 실행하며, 실패하면 다음 단계로 넘어가지 않는다.

먼저 `nginx`를 기동하면 필요한 Data·External·MediaMTX도 준비된다. 이후 선택한 서비스만 빌드·재생성하여 새 이미지 반영을 확인한다.

```powershell
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --wait nginx
docker compose --env-file server/.env -f server/compose.yml stop preprocessing
docker compose --env-file server/.env -f server/compose.yml build preprocessing
docker compose --env-file server/.env -f server/compose.yml up -d --no-deps --force-recreate --wait preprocessing
$preprocessingContainer = docker compose --env-file server/.env -f server/compose.yml ps -q preprocessing
docker inspect --format '{{.Image}}' $preprocessingContainer
docker image inspect --format '{{.Id}}' ai-cctv-preprocessing:handoff-v1
docker inspect --format '{{json .Mounts}}' $preprocessingContainer
docker inspect --format '{{.Config.User}}' $preprocessingContainer
docker compose --env-file server/.env -f server/compose.yml logs --tail 100 preprocessing
```

컨테이너 ID가 비어 있지 않고 **두 이미지 ID가 같은지** 확인한다. Mounts는 의도한 호스트 경로이며 `/snapshots`는 `RW=true`, `/models`와 `/app/config/config.yaml`은 `RW=false`여야 한다. User는 배포의 `AI_CCTV_UID:AI_CCTV_GID`(기본 `1000:1000`)와 비교한다. 실제 태그가 다르면 비교 명령도 맞춘다. `restart`만으로는 새 이미지·환경이 적용되지 않는다.

아래 상태 조회의 Python은 **Data 컨테이너에 있는 도구**이므로 새 preprocessing 이미지에는 Python이 필요 없다.

```powershell
docker compose --env-file server/.env -f server/compose.yml exec -T data python -c "import urllib.request; opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); print(opener.open('http://preprocessing:8000/health/ready', timeout=3).read().decode())"
```

`--wait`와 상태 확인 이후에도 실제 프레임·이벤트·파일·identity 결과를 확인한다.

실제 카메라에서 사람 등장·퇴장을 만든 뒤 아래로 직전 5분의 이벤트(최대 200건)와 최신 좌표를 조회한다. `camera_id`를 실제 값으로 바꾸고, 좌표는 **영상에 사람이 들어오는 동안** 확인한다. 3초 초과 좌표는 빈 배열이 된다. 이 검사는 Data 컨테이너의 기존 External 토큰으로 읽기만 수행하며 preprocessing에 토큰을 추가하거나 claim·complete를 수동 호출하지 않는다.

```powershell
@'
import json, os, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
camera_id = "cam-001"
base = "http://nginx:8080/internal/data/v1"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
query = urllib.parse.urlencode({"camera_id": camera_id, "limit": 200,
    "from": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()})
for path in (f"/events?{query}", f"/cameras/{camera_id}/objects"):
    request = urllib.request.Request(base + path, headers={
        "X-Internal-Token": os.environ["DATA_EXTERNAL_TOKEN"]
    })
    with opener.open(request, timeout=10) as response:
        print(path, json.dumps(json.load(response), ensure_ascii=False, indent=2))
'@ | docker compose --env-file server/.env -f server/compose.yml exec -T data python -
```

이벤트의 `items`에서 등장·사라짐·세션·이미지 경로와 `metadata.identity`/`metadata.analysis` 결과를 확인하고, 좌표 응답에서 bbox·로컬/전역 ID를 확인한다. 고유 시험 시각과 비교하여 이전 실행의 이벤트를 새 구현 결과로 혼동하지 않는다.

기존 회귀 검사는 다음과 같다. 새 언어·새 이미지의 통과를 대신하지 않으므로 **교체 컨테이너를 실제 실행하여 Data와 주고받는 검사를 추가**한다.

```powershell
docker compose -f server/compose.test.yml build tests
docker compose -f server/compose.test.yml run --rm tests python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests tests/automated/test_object_processing.py tests/automated/test_person_identifiers.py -q
```

이 테스트 Compose는 단독 구성으로, 운영 Compose와 합치지 않는다.

로컬 회귀 검사는 OSNet 입력·512차원 출력과 오류 처리, 이전 특징 추출 호환, YOLO/ByteTrack 어댑터 대역, RTSP/HTTP 장애와 이벤트 대기열·Data gallery 계약을 포함한다. 공식 가중치 변환 후 PyTorch 2.8.0 CPU와 OpenCV 4.11의 세 입력 출력을 실제 비교했으며 `.onnx.json`에 버전·해시·검증 수치를 기록한다. 재현은 [모델 준비 도구 안내](../server/tools/README.md)를 따른다. Docker·실제 RTSP 카메라의 정확도·지연·전체 기동은 별도 검증 대상이다. 대역 검사나 모델 추론 성공을 실제 CCTV 인수 성능으로 제시하지 않는다.

실장비 재현 시 검증한 사람 클래스 0의 YOLO 가중치와 준비 도구로 변환한 OSNet ONNX를 `MODELS_DIR`에 두고 `MODEL_FILE`·`IDENTITY_MODEL_PATH`를 맞춘다. 모델 해시·두 매칭 설정값을 기록한다. 개발 Compose 기동 후 동일인·의복이 바뀐 동일인·비슷한 옷의 다른 사람을 포함한 시험 영상으로 crop·시각·로컬 추적·전역 ID·`match` 결정을 함께 수집한다. 기준 조정용 영상과 평가 영상을 분리하고 오연결·미연결을 측정한다. 시간차 1,800초, 같은 카메라의 동시 인물, 카메라 재연결과 Data 재시작을 나누어 확인하며 처리시간도 기록한다. 기존 HSV 기록이 있는 배포는 새 공간 분리와 기존 추적 ID 유지도 확인한다.

### 선택: 기존 Python 실행기를 재사용할 때만

전체 컨테이너를 새로 구현한다면 이 항목은 필요 없다.

- `DETECTION_PLUGIN=모듈:팩토리`: 팩토리 `(model_path: Path, confidence: float, device: str)`가 카메라마다 `reset()`·`process(frame)` 객체를 만든다. frame은 [DetectionFrame](../server/services/preprocessing/processors/detection/contracts.py)이며 `camera_id`, 세션, 시간대 있는 `datetime`, `uint8 H×W×3 BGR` 이미지 속성을 가진다. 입력을 수정하지 않고 `DetectionResult` 또는 `{"schema_version":1,"objects":[...]}`를 반환한다. objects는 5절의 3개 필드이며 최대 100개를 **반환 전에** 제한한다. 검증이 좌표 자르기보다 먼저다.
- `IDENTITY_PLUGIN=모듈:팩토리`: 인자 없는 팩토리가 `process(job, crop_path: Path)` 객체를 만든다. job은 6절의 작업 dict이며 결과는 **lease_id를 제외한** 완료 dict다. 실행기가 lease를 붙인다.

## 8. 인수와 이 문서의 삭제 조건

| 확인 | 통과 기준 |
|---|---|
| 등장·사라짐 | 등장 1회, 계속 보일 때 중복 없음, 대기 후 사라짐 1회. 같은 ID·세션 |
| 파일·좌표 | 크롭·박스·원본이 같은 프레임. UID 권한 정상. 빈 배열·3초 초과 좌표 제거 확인 |
| 재접속·격리 | 단절/복구 각 1회와 새 세션. 다른 카메라·identity·상태 서버 계속 응답 |
| Data 장애 | 영속 큐·재전송의 동일 source ID, 중복 작업·푸시 방지, 포화 시 생성 중단과 종료 손실 상태 확인 |
| identity 연결 | 실제 특징→Data gallery→이벤트·좌표 ID 반영. 모호함·동시 인물 제외·재시작·공개 응답의 벡터 제외 확인 |
| OSNet 전환 | 실제 512차원 출력·파일 해시, 상수 크롭 거부, 기존 추적 ID 유지·다른 공간 비교 제외 확인 |
| 검증·권한 | 토큰 교차 사용 403, 잘못된 경로·박스 422, 기존 전역 ID 변경 409 |
| 임대·실패 | 중복 완료 `accepted:false`, 중단 후 새 lease, 늦은 결과 거부. 시간 초과·누락 크롭 구분 |
| 미설정 호환 | 명시한 BlackBox는 `unconfigured`·전역 ID 없음. 기본 구현으로 교체 후 requeue와 완료 확인 |
| 실제 성능 | 장비·모델·동시 카메라 수·영상 FPS·실제 감지 FPS·지연·정확도 측정 조건과 결과 기록 |

영상 송출 30fps와 기본 감지 5fps는 서로 다른 설정이며, 4대 제한도 처리 성능 실측값이 아니다. 시험용 고정 ID는 연결 검증에만 사용한다.

인수 전에 새 `server/services/preprocessing/README.md`에 **실제 빌드·기동·상태 확인 명령, env·마운트·UID, 모델 준비·제약, 실제 입출력·실패 처리, 테스트 명령·결과와 실장비 검증 범위**를 남긴다. 새 구현 테스트와 영향받은 기존 회귀 검사가 통과하고 담당자의 교체 인수가 끝난 뒤 이 SRS를 삭제한다. 삭제 시 문서 색인·상호 링크·패키징 포함 목록도 함께 정리한다.

구현 대조용 원문: [Compose](../server/compose.yml), [감지 실행기](../server/services/preprocessing/app/pipeline.py), [HTTP 객체 형식](../lib/ai_cctv_core/contracts/objects.py), [이벤트 API](../server/services/data/app/api/events.py), [작업 저장·임대](../server/services/data/app/database/repositories/objects.py). 별도 SRS나 구조 문서를 먼저 읽을 필요는 없다.
