# Preprocessing 서비스

MediaMTX 영상에서 사람을 감지·추적한다. 사람 영역 이미지는 공유 저장소에 저장하고 바운딩 박스·`person_id`·이미지 경로를 Data에 전달한다. `person_id`는 카메라와 추적 세션 안에서만 유효하므로 사람을 구분할 때 `camera_id`·`tracking_session_id`와 함께 사용한다. 카메라 간 `global_person_id`를 연결하는 작업도 같은 컨테이너에서 별도로 실행한다. 기동 후 인물 연결 플러그인의 실패가 감지를 중단시키지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 영상 수신·카메라별 실행·이벤트와 좌표 전달·상태 |
| `processors/detection/contracts.py` | 감지기 입력·출력 계약 |
| `processors/detection/yolo.py` | YOLO/ByteTrack 기본 감지기 |
| `processors/identity/` | 기본 외관 특징·선택 ONNX Re-ID 특징 추출기와 이전 블랙박스 호환 |
| [`lib/ai_cctv_core/processing/`](../../../lib/ai_cctv_core/processing/) | 인물 연결과 분석이 공유하는 작업 임대·검증·재시도 결과 보고 |

기본 감지기는 YOLO/ByteTrack이며 사람 클래스 번호가 `0`인 Ultralytics 호환 모델 파일이 필요하다. 기본 `LocalAppearanceIdentity`는 사람 크롭에서 외관 특징을 실제 추출하고 Data의 영속 gallery가 카메라·세션 간 연결을 결정한다. 명시적으로 `IdentityBlackBox`를 선택한 경우에만 종전의 `unconfigured` 동작을 사용한다. 바운딩 박스는 영상에 직접 합성하지 않고 모바일이 전달받은 좌표로 화면 위에 그린다.

`DETECTION_PLUGIN`과 `IDENTITY_PLUGIN`에 `패키지.모듈:팩토리`를 지정해 현재 Python 구현의 플러그인을 교체한다. 감지 팩토리는 `model_path`·`confidence`·`device` 순서의 위치 인자를 받고, 인물 연결 팩토리는 인자 없이 호출된다. Data의 SQLite는 직접 열지 않으며, 이벤트 송신 대기열만 공유 스냅샷 폴더의 별도 SQLite 파일로 보관한다.

## 현행 입출력과 구현 기준

감지기는 `reset()`과 `process(DetectionFrame) -> DetectionResult`를 제공한다. 입력 영상은 H×W×3 형태의 `uint8` BGR 배열이며 입력 프레임을 변경하지 않는다. 결과에는 카메라·추적 세션 안의 인물 ID, 픽셀 단위 `(x1, y1, x2, y2)` 박스와 신뢰도를 담는다. 프레임 형식·좌표 범위·최대 객체 수는 [감지 계약 코드](processors/detection/contracts.py), 동작 예시는 [감지기 계약 테스트](tests/test_processor_contract.py)에서 확인한다.

인물 연결 플러그인은 `process(job: dict, crop_path: Path) -> dict`를 제공한다. `job`은 Data에서 임대한 객체 작업이며 `crop_path`는 검증된 로컬 크롭 경로다. 반환값은 `lease_id`를 제외한 `ObjectJobCompletion` 필드이고 공통 실행기가 임대 ID를 붙여 Data에 완료 결과를 보고한다. `outcome`은 `complete`·`retry`·`failed`·`unconfigured` 중 하나다. 기본 구현은 `complete`와 `identity_descriptor`를 반환하며 `global_person_id`를 만들지 않는다. 교체 플러그인은 완료 때 명시 ID 또는 descriptor 중 하나만 제출할 수 있다. 기본 구현은 공용 JPEG 로더로 8 MiB·1,600만 픽셀 상한과 bbox 치수 일치를 검사하며, 교체 구현도 같은 검증을 유지해야 한다.

### 기본 특징과 Data의 인물 연결

`IDENTITY_PLUGIN=server.services.preprocessing.processors.identity:LocalAppearanceIdentity`가 기본이다. `IDENTITY_MODEL_PATH` 환경변수가 없거나 정확히 빈 문자열이면 상·하체의 네 수평 띠에서 HSV·무채색 밝기·경사 특징 392개를 추출해 L2 단위벡터로 만든다. 특징 공간은 `appearance-hsv-v1`이다. 학습 모델·다운로드 없이 CPU에서 실행하며 파일이나 gallery를 직접 쓰지 않는다. 공백만 있는 값이나 잘못된 파일 경로는 설정 오류다.

`IDENTITY_MODEL_PATH=/models/reid.onnx`를 지정하면 그 모델만 OpenCV DNN CPU로 실행한다. `/models` 안의 256 MiB 이하 ONNX 파일이어야 하며 입력은 RGB `float32` NCHW `1×3×256×128`, `[0,1]` 변환 뒤 ImageNet 평균 `[0.485,0.456,0.406]`·표준편차 `[0.229,0.224,0.225]`를 적용한다. 단일 `1×D` 출력에서 `16 ≤ D ≤ 2048`의 유한 비영벡터만 정규화한다. 특징 공간은 `onnx-reid:<파일 SHA-256>:rgb256x128-imagenet-v1`이다. 잘못된 파일·입출력은 실패하며 기본 특징으로 몰래 대체하거나 모델을 내려받지 않는다.

descriptor는 `{schema_version:1, space_id, features}`이며 `space_id`는 1~128자, features는 유한 실수 16~2048개, L2 norm 허용 오차는 ±0.001이다. 벡터는 Data의 비공개 gallery에만 저장되고 공개 이벤트 metadata에 복사되지 않는다. Data는 같은 공간·차원의 관측 중 인물별 최고 cosine 점수를 비교한다. 0.97 이상이고 두 번째 후보와 0.05 이상 차이 날 때만 기존 ID를 사용하며, 후보 없음·낮은 점수·모호함은 새 `person-<uuid>` ID가 된다. 같은 카메라의 다른 추적이 ±30초 안에 존재한 ID는 후보에서 제외한다. 이미 연결된 같은 `(camera_id, tracking_session_id, person_id)`는 항상 기존 ID를 유지한다.

gallery는 관측 시각 기준 최근 1,800초·최대 5,000표본을 유지하며 추적별 연결은 재시작 후에도 남는다. 이 값들은 Data의 [identity 저장소](../data/app/database/repositories/identity.py) 상수이며 환경변수는 아니다. 오래 지난 새 추적은 새로운 ID를 받을 수 있다. 이벤트의 `metadata.identity.result.match`에는 `new|matched|existing_track` 결정과 cosine 점수를 남긴다. 점수는 같은 사람일 확률이 아니다. 비슷한 옷·조명·자세·가림으로 잘못 연결되거나 같은 사람을 분리할 수 있으며 실명이나 확정 신원 확인에 사용하지 않는다.

유지할 기준은 [객체 스키마](../../../lib/ai_cctv_core/contracts/objects.py), [플러그인 프로토콜](../../../lib/ai_cctv_core/processing/plugins.py), [공통 실행기](../../../lib/ai_cctv_core/processing/worker.py), [Data 작업 API](../data/app/api/objects.py)와 [객체 처리 통합 테스트](../../../tests/automated/test_object_processing.py)다. [인물 식별자 테스트](../../../tests/automated/test_person_identifiers.py)는 카메라·세션별 ID 구분을 검증한다.

## 실행과 검증

중앙 Compose로 실행한다. 감지 설정은 `server/config/config.yaml`의 `inference`, 모델·플러그인 선택은 `server/.env`, 인증키는 `server/secrets/preprocessing.env`가 기본 위치다. 설정 준비는 [소스 배포](../../../docs/guide.md#소스-배포)를 따른다.

Compose는 `MODELS_DIR`를 `/models`에 읽기 전용으로 연결하고, `MODEL_FILE`로 `/models/<파일명>`의 `MODEL_PATH`를 설정한다. 환경변수가 YAML보다 우선하므로 이 구성에서는 `inference.model_path`만 바꿔도 모델 파일이 바뀌지 않는다. 모델 없이 연결을 시험하려면 `inference.enabled: false`로 감지를 끌 수 있다.

`DATA_INFERENCE_TOKEN`·`DATA_IDENTITY_TOKEN`은 Data의 해당 권한 토큰과, `MEDIA_READ_USERNAME`·`MEDIA_READ_PASSWORD`는 External의 영상 읽기 인증값과 일치해야 한다. 감지를 꺼도 인물 연결 작업기는 실행되며, `DATA_IDENTITY_TOKEN`이 32자 미만이면 컨테이너 시작이 실패한다.

`unconfigured`로 종료된 인물 연결 작업은 모델 교체·재시작만으로 다시 처리하지 않는다. 모델을 연결하고 크롭 파일이 남아 있는지 확인한 뒤 내부 네트워크에서 `POST http://nginx:8080/internal/data/v1/object-jobs/identity/requeue-unconfigured`를 호출한다. 본문은 없고 `X-Internal-Token`에는 `DATA_IDENTITY_TOKEN`을 사용한다. 응답 `{"requeued": 수량}`만큼 최대 100건씩 다시 대기하므로 남은 작업이 있으면 반복한다. 만료된 임대·재시도·재등록 규칙은 [Data 작업 저장소](../data/app/database/repositories/objects.py)와 [권한 검사](../data/app/security.py)를 따른다.

[개발 환경](../../../docs/guide.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec preprocessing python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests -q
```

`/health/ready`는 Data 연결에 성공하면 HTTP 200이면서 본문은 `degraded`일 수 있다. 실제 감지 상태는 카메라별 `workers`, 인물 연결 상태는 `identity`의 `ready`·`stalled`·`last_error`·`last_outcome`을 함께 확인한다. 모델·영상 오류와 블랙박스 상태의 해석은 [상태 확인](../../../docs/architecture.md#상태-확인)을 따른다.

감지가 켜진 카메라의 offline·stopped·모델 미준비, 이벤트 보존 오류·거부 큐, 오래된 프레임은 `degraded`로 표시한다. `frame_age_seconds`는 마지막 수신 후 단조 시계 경과 시간이며 아직 수신하지 않았으면 `null`이다. 상태 조회에서 `max(30초, RTSP_TIMEOUT_SECONDS×2)` 이상 프레임이 없으면 `frame_stale=true`가 된다. 첫 프레임 전에는 카메라 작업자 생성부터 같은 유예시간을 적용한다.

identity 초기화·호출은 별도 `spawn` 프로세스에서 실행한다. `OBJECT_MODEL_TIMEOUT_SECONDS` 기본 120초(`0 < 값 ≤ 240`), `OBJECT_STARTUP_TIMEOUT_SECONDS` 기본 30초(`0 < 값 ≤ 120`)다. 제한을 넘은 자식은 종료·재생성하고 초기화 실패는 30초 후 재시도한다. 완료 전송 실패 시 같은 lease·결과를 보관해 재추론 없이 다시 전송한다. 감지 카메라와 identity 실행은 독립적으로 복구한다.

RTSP 열기·읽기에는 각각 `RTSP_TIMEOUT_SECONDS` 기본 5초, 최대 30초를 적용하고 실패 시 캡처를 해제해 1~15초 간격으로 재연결한다. 감지 모델 오류는 `MODEL_RETRY_SECONDS` 기본 30초 후 같은 카메라 실행 흐름에서 재준비한다. 영상 상태 감시는 계속한다. 감독자의 종료 대기는 `DETECTION_SHUTDOWN_SECONDS` 기본 15초·최대 60초이며 Compose 종료 유예는 identity 정리 여유를 포함한 75초다. 감지 네이티브 호출 자체가 교착하면 스레드를 강제 종료하지 못하며 기한 뒤 경고한다. 별도 프로세스의 identity 제한과 구분해야 한다.

등장 이미지 저장을 끝낸 후 이벤트를 `SNAPSHOTS_ROOT/.event-outbox.sqlite3`에 먼저 기록한다. `source_event_id`를 재전송에도 유지하고 Data가 `(camera_id, source_event_id)`로 이벤트·작업·푸시 중복을 막는다. 큐 한도는 `EVENT_OUTBOX_MAX_PENDING=10000`건과 `EVENT_OUTBOX_MAX_BYTES=67108864` JSON 바이트다. 실제 SQLite 파일은 페이지·WAL 때문에 더 클 수 있다. 400·404·413·422 응답은 거부 항목으로 보관하고 다음 항목을 진행하며, 그 밖의 전송 실패는 0.5~30초 간격으로 재시도한다. 큐 한도와 디스크 장애까지 포함한 무제한 무손실 보장은 제공하지 않는다.

큐 포화·쓰기 실패 시 현재 이벤트 한 건과 이미 만든 스냅샷을 유지하고 추가 추론·스냅샷 생성을 멈춘다. 0.5~5초 간격으로 같은 이벤트의 영속화를 재시도한다. `event_backpressure`·`event_delivery_error`·`event_persistence_failures`, 큐의 `event_delivery.pending/rejected/last_error`를 확인한다. 종료까지 영속화하지 못한 항목은 `event_shutdown_losses`와 ERROR 로그에 남기며 재시작 후 자동 복원을 보장하지 않는다. 큐 상한은 JPEG 파일 총용량을 포함하지 않는다.

사람 crop 저장 실패도 해당 프레임을 유지한 채 추가 추론을 멈추고 0.5~5초 간격으로 재시도한다. 저장 성공 전에 관측 없는 등장 이벤트를 대신 보내지 않는다. `observation_error`·`observation_persistence_failures`에 실패가 드러나며, 지연 후 전송해도 등장·퇴장 시각은 재시도 완료 시각이 아닌 원래 프레임 수신 시각이다.

[외관 특징 테스트](tests/test_local_identity.py)는 실제 합성 JPEG의 동일·다른 색 배치, 밝기·크기 변화, 손상 입력과 모델 대역의 ONNX 전처리·출력 검증을 수행한다. 로컬 개발 Python 환경에서는 다음으로 재현한다.

```powershell
python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests -q
```

실제 YOLO/ONNX 모델·Docker·카메라를 사용한 정확도·처리량·실기동은 이 검증에 포함하지 않았다. 검증된 YOLO 파일과 필요시 계약에 맞는 ONNX를 읽기 전용 모델 폴더에 준비한 뒤, [Preprocessing 인수 문서](../../../docs/SRS_interface_preprocessing.md)의 실제 프레임·등장 이벤트·crop·전역 연결·장애 복구 절차로 별도 확인한다.
