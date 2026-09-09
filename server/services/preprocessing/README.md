# Preprocessing 서비스

MediaMTX 영상에서 사람을 감지·추적한다. 사람 영역 이미지는 공유 저장소에 저장하고 바운딩 박스·`person_id`·이미지 경로를 Data에 전달한다. `person_id`는 카메라와 추적 세션 안에서만 유효하므로 사람을 구분할 때 `camera_id`·`tracking_session_id`와 함께 사용한다. 카메라 간 `global_person_id`를 연결하는 작업도 같은 컨테이너에서 별도로 실행한다. 기동 후 인물 연결 플러그인의 실패가 감지를 중단시키지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 영상 수신·카메라별 실행·이벤트와 좌표 전달·상태 |
| `processors/detection/contracts.py` | 감지기 입력·출력 계약 |
| `processors/detection/yolo.py` | YOLO/ByteTrack 기본 감지기 |
| `processors/identity/` | 기본 OSNet Re-ID 특징 추출기, 이전 외관 특징·범용 ONNX·블랙박스 호환 |
| [`lib/ai_cctv_core/processing/`](../../../lib/ai_cctv_core/processing/) | 인물 연결과 분석이 공유하는 작업 임대·검증·재시도 결과 보고 |

기본 감지기는 YOLO/ByteTrack이며 사람 클래스 번호가 `0`인 Ultralytics 호환 모델 파일이 필요하다. 기본 `OsNetIdentity`는 사람 재식별용으로 학습된 OSNet x0.25의 ONNX 모델로 크롭의 특징을 추출하고, Data의 영속 gallery가 카메라·세션 간 연결을 결정한다. `LocalAppearanceIdentity`와 `IdentityBlackBox`는 명시적으로 선택하는 이전 구현이며, 블랙박스는 `unconfigured`를 반환한다. 바운딩 박스는 영상에 직접 합성하지 않고 모바일이 전달받은 좌표로 화면 위에 그린다.

`DETECTION_PLUGIN`과 `IDENTITY_PLUGIN`에 `패키지.모듈:팩토리`를 지정해 현재 Python 구현의 플러그인을 교체한다. 감지 팩토리는 `model_path`·`confidence`·`device` 순서의 위치 인자를 받고, 인물 연결 팩토리는 인자 없이 호출된다. Data의 SQLite는 직접 열지 않으며, 이벤트 송신 대기열만 공유 스냅샷 폴더의 별도 SQLite 파일로 보관한다.

## 현행 입출력과 구현 기준

감지기는 `reset()`과 `process(DetectionFrame) -> DetectionResult`를 제공한다. 입력 영상은 H×W×3 형태의 `uint8` BGR 배열이며 입력 프레임을 변경하지 않는다. 결과에는 카메라·추적 세션 안의 인물 ID, 픽셀 단위 `(x1, y1, x2, y2)` 박스와 신뢰도를 담는다. 프레임 형식·좌표 범위·최대 객체 수는 [감지 계약 코드](processors/detection/contracts.py), 동작 예시는 [감지기 계약 테스트](tests/test_processor_contract.py)에서 확인한다.

인물 연결 플러그인은 `process(job: dict, crop_path: Path) -> dict`를 제공한다. `job`은 Data에서 임대한 객체 작업이며 `crop_path`는 검증된 로컬 크롭 경로다. 반환값은 `lease_id`를 제외한 `ObjectJobCompletion` 필드이고 공통 실행기가 임대 ID를 붙여 Data에 완료 결과를 보고한다. `outcome`은 `complete`·`retry`·`failed`·`unconfigured` 중 하나다. 기본 구현은 `complete`와 `identity_descriptor`를 반환하며 `global_person_id`를 만들지 않는다. 교체 플러그인은 완료 때 명시 ID 또는 descriptor 중 하나만 제출할 수 있다. 기본 구현은 공용 JPEG 로더로 8 MiB·1,600만 픽셀 상한과 bbox 치수 일치를 검사하며, 교체 구현도 같은 검증을 유지해야 한다.

### 기본 OSNet 모델과 Data의 인물 연결

`IDENTITY_PLUGIN=server.services.preprocessing.processors.identity:OsNetIdentity`가 기본이다. `IDENTITY_MODEL_PATH`가 없거나 정확히 빈 문자열이면 `/models/osnet_x0_25_msmt17.onnx`를 읽는다. 모델은 [공식 OSNet 저장소](https://huggingface.co/kaiyangzhou/osnet)의 **OSNet x0.25 MSMT17 combineall Re-ID 사전학습 가중치**를 변환해 준비한다. 학습 데이터 범위는 [공식 Model Zoo](https://kaiyangzhou.github.io/deep-person-reid/MODEL_ZOO)를 참고한다. 이 프로젝트의 CCTV 영상으로 재학습하거나 임계값을 교정한 모델은 아니다.

OpenCV DNN CPU로 실행하며, 입력은 RGB `float32` NCHW `1×3×256×128`이다. `[0,1]` 변환 뒤 ImageNet 평균 `[0.485,0.456,0.406]`·표준편차 `[0.229,0.224,0.225]`를 적용한다. 단일 `float32 1×512` 특징 출력을 검사하고 유한 비영벡터를 L2 단위 길이로 정규화한다. 특징 공간은 `osnet:<파일 SHA-256>:rgb256x128-imagenet-v1`이며 다른 모델의 벡터와 섞어 비교하지 않는다. `/models` 안의 256 MiB 이하 ONNX만 허용하고 초기화 때 시험 추론으로 실행·출력을 확인한다. 누락·손상·출력 불일치는 오류로 드러낸다. **서비스는 모델을 자동 다운로드하거나 HSV 특징으로 대체하지 않는다.**

`LocalAppearanceIdentity`를 명시적으로 선택하면 기존 동작을 유지한다. 모델 경로가 없거나 빈 문자열일 때는 `appearance-hsv-v1`의 HSV·밝기·질감 392차원 특징을 계산하고, 경로를 지정하면 16~2048차원을 허용하는 기존 범용 ONNX 경로를 사용한다. 이 호환 플러그인의 입력·출력 범위는 기본 OSNet의 정확한 512차원 계약과 다르다.

공통 descriptor 계약은 `{schema_version:1, space_id, features}`이며 `space_id`는 1~128자, features는 유한 실수 16~2048개, L2 norm 허용 오차는 ±0.001이다. 기본 OSNet은 그중 512차원을 사용한다. 벡터는 Data의 비공개 gallery에만 저장되고 공개 이벤트 metadata에 복사되지 않는다. Data는 같은 공간·차원의 관측 중 인물별 최고 cosine 점수를 비교한다. 기본 0.97 이상이고 두 번째 후보와 기본 0.05 이상 차이 날 때만 기존 ID를 사용하며, 후보 없음·낮은 점수·모호함은 새 `person-<uuid>` ID가 된다. 같은 카메라의 다른 추적과 관측 구간이 겹치거나 등장 시각이 ±30초 안인 ID는 후보에서 제외한다. 등장·퇴장·최신 객체 관측으로 구간을 저장하여 지연된 작업에도 적용한다.

비교 기준은 **Data 서비스**의 `IDENTITY_MATCH_THRESHOLD`·`IDENTITY_MATCH_MARGIN`으로 조정한다. 기본 0.97·0.05는 기존의 보수적인 값을 유지한 것이며 OSNet이나 설치 현장 데이터로 교정된 기준이 아니다. 임계값을 바꾸기 전에 동일인·유사 복장 타인의 검증 영상을 분리하고 오연결·미연결을 측정한다. gallery의 관측 시각 기준 1,800초·최대 5,000표본 제한은 [Data identity 저장소](../data/app/database/repositories/identity.py)의 상수다. 오래 지난 새 추적은 새로운 ID를 받을 수 있다.

보관 중인 같은 `(camera_id, tracking_session_id, person_id)`의 연결은 모델이 달라져도 기존 ID를 유지한다. 같은 추적을 새 특징 공간으로 처리하면 기존 ID 아래 새 공간의 표본을 저장한다. 따라서 벡터 비교 공간은 분리되지만 이전 모델의 오연결을 OSNet 전환으로 자동 교정하지 않는다. 완료된 작업도 모델 교체만으로 다시 추론하지 않는다. 관련 이벤트가 없고 관측도 보관 기한을 지난 연결은 정리한다. `metadata.identity.result.match`에는 OSNet의 `method:osnet`, `new|matched|existing_track` 결정, cosine 점수, 적용한 `threshold`·`margin`을 남긴다. 점수는 동일인 확률이 아니다.

현재 입력은 **등장 뒤 짧은 창에서 고른 crop 한 장**이다. 배포 기본 `OBSERVATION_WINDOW_SECONDS=1` 동안 정보가 있는 영역·선명도·크기를 비교하며 특징 평균은 하지 않는다. 카메라별 후보 메모리는 `OBSERVATION_BUFFER_MAX_BYTES=33554432`로 제한하며 상한 초과·퇴장·재접속·정상 종료 때 남은 후보를 확정한다. 0초로 설정하면 즉시 저장한다. 최초 등장 시각을 유지하고 선택 시각·`usable|insufficient` 품질은 이벤트의 `observation_selection`에 기록한다. 확정 이후 입력은 교체하지 않는다. 누락·손상·16픽셀 미만·상수 영상은 OSNet이 실패 처리한다. 유사한 옷·조명·자세·가림으로 오연결·분리가 가능하며, 실명이나 확정 신원 확인에 사용하지 않는다.

유지할 기준은 [객체 스키마](../../../lib/ai_cctv_core/contracts/objects.py), [플러그인 프로토콜](../../../lib/ai_cctv_core/processing/plugins.py), [공통 실행기](../../../lib/ai_cctv_core/processing/worker.py), [Data 작업 API](../data/app/api/objects.py)와 [객체 처리 통합 테스트](../../../tests/automated/test_object_processing.py)다. [인물 식별자 테스트](../../../tests/automated/test_person_identifiers.py)는 카메라·세션별 ID 구분을 검증한다.

## 실행과 검증

중앙 Compose로 실행한다. 감지 설정은 `server/config/config.yaml`의 `inference`, 모델·플러그인 선택은 `server/.env`, 인증키는 `server/secrets/preprocessing.env`가 기본 위치다. 설정 준비는 [소스 배포](../../../docs/guide.md#소스-배포)를 따른다.

Compose는 `MODELS_DIR`를 `/models`에 읽기 전용으로 연결하고, `MODEL_FILE`로 감지기의 `/models/<파일명>` 경로를 설정한다. 환경변수가 YAML보다 우선하므로 이 구성에서는 `inference.model_path`만 바꿔도 감지 모델 파일이 바뀌지 않는다. 인물 연결은 별도의 `IDENTITY_MODEL_PATH`를 사용한다. `inference.enabled: false`는 감지만 끄며 OSNet 모델 준비를 대신하지 않는다.

배포 담당자는 저장소 루트에서 [모델 준비 도구 안내](../../tools/README.md)에 따라 CPU PyTorch와 [변환 의존성](../../tools/requirements-osnet.txt)을 준비한 뒤 실행한다.

```powershell
python server/tools/prepare_osnet.py
```

기본 결과는 `server/runtime/models/osnet_x0_25_msmt17.onnx`다. `MODELS_DIR`를 바꾼 배포는 `--output C:/path/to/models/osnet_x0_25_msmt17.onnx`로 그 폴더에 생성하고 컨테이너의 모델 경로와 맞춘다. 이 준비 단계에서 공식 가중치를 내려받고 ONNX로 변환한다. 변환용 PyTorch는 서비스 실행 의존성이 아니다. 설치 파일을 사용하는 운영자는 배포 담당자가 준비한 모델을 받으면 된다.

`DATA_INFERENCE_TOKEN`·`DATA_IDENTITY_TOKEN`은 Data의 해당 권한 토큰과, `MEDIA_READ_USERNAME`·`MEDIA_READ_PASSWORD`는 External의 영상 읽기 인증값과 일치해야 한다. 감지를 꺼도 인물 연결 작업기는 실행되며, `DATA_IDENTITY_TOKEN`이 32자 미만이면 컨테이너 시작이 실패한다.

`unconfigured`로 종료된 인물 연결 작업은 모델 교체·재시작만으로 다시 처리하지 않는다. 모델을 연결하고 크롭 파일이 남아 있는지 확인한 뒤 내부 네트워크에서 `POST http://nginx:8080/internal/data/v1/object-jobs/identity/requeue-unconfigured`를 호출한다. 본문은 없고 `X-Internal-Token`에는 `DATA_IDENTITY_TOKEN`을 사용한다. 응답 `{"requeued": 수량}`만큼 최대 100건씩 다시 대기하므로 남은 작업이 있으면 반복한다. 만료된 임대·재시도·재등록 규칙은 [Data 작업 저장소](../data/app/database/repositories/objects.py)와 [권한 검사](../data/app/security.py)를 따른다.

[개발 환경](../../../docs/guide.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec preprocessing python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests -q
```

`/health/ready`는 Data 연결에 성공하면 HTTP 200이면서 본문은 `degraded`일 수 있다. 실제 감지 상태는 카메라별 `workers`, 인물 연결 상태는 `identity`의 `ready`·`stalled`·`last_error`·`last_outcome`을 함께 확인한다. 모델·영상 오류와 블랙박스 상태의 해석은 [상태 확인](../../../docs/architecture.md#상태-확인)을 따른다.

감지가 켜진 카메라의 offline·stopped·모델 미준비, 이벤트 보존 오류·거부 큐, 오래된 프레임은 `degraded`로 표시한다. `frame_age_seconds`는 마지막 수신 후 단조 시계 경과 시간이며 아직 수신하지 않았으면 `null`이다. 상태 조회에서 `max(30초, RTSP_TIMEOUT_SECONDS×2)` 이상 프레임이 없으면 `frame_stale=true`가 된다. 첫 프레임 전에는 카메라 작업자 생성부터 같은 유예시간을 적용한다.

identity 초기화·호출은 별도 `spawn` 프로세스에서 실행한다. `OBJECT_MODEL_TIMEOUT_SECONDS` 기본 120초(`0 < 값 ≤ 240`), `OBJECT_STARTUP_TIMEOUT_SECONDS` 기본 30초(`0 < 값 ≤ 120`)다. 제한을 넘은 자식은 종료·재생성하고 초기화 실패는 30초 후 재시도한다. 완료 전송 실패 시 같은 lease·결과를 보관해 재추론 없이 다시 전송한다. 감지 카메라와 identity 실행은 독립적으로 복구한다.

RTSP 열기·읽기에는 각각 `RTSP_TIMEOUT_SECONDS` 기본 5초, 최대 30초를 적용하고 실패 시 캡처를 해제해 1~15초 간격으로 재연결한다. 감지 모델은 카메라별 별도 `spawn` 자식에서 실행한다. `DETECTION_STARTUP_TIMEOUT_SECONDS=30`(최대 120초), `DETECTION_MODEL_TIMEOUT_SECONDS=10`(최대 60초)을 넘기면 자식을 종료하고 `MODEL_RETRY_SECONDS` 기본 30초 후 새 추적 세션으로 재준비한다. 모델 오류에도 영상 연결 감시는 계속한다. `model_timeouts`·`last_inference_seconds`로 시간 초과와 처리시간을 확인한다. 감독자의 종료 대기는 `DETECTION_SHUTDOWN_SECONDS` 기본 15초·최대 60초이며 Compose 종료 유예는 identity 정리 여유를 포함한 75초다. `PREPROCESSING_MEMORY_LIMIT` 기본 4g는 선택한 모델·카메라 수에 맞게 조정한다.

등장 이미지 저장을 끝낸 후 이벤트를 `SNAPSHOTS_ROOT/.event-outbox.sqlite3`에 먼저 기록한다. `source_event_id`를 재전송에도 유지하고 Data가 `(camera_id, source_event_id)`로 이벤트·작업·푸시 중복을 막는다. 큐 한도는 `EVENT_OUTBOX_MAX_PENDING=10000`건과 `EVENT_OUTBOX_MAX_BYTES=67108864` JSON 바이트다. 실제 SQLite 파일은 페이지·WAL 때문에 더 클 수 있다. 400·404·413·422 응답은 거부 항목으로 보관하고 다음 항목을 진행하며, 그 밖의 전송 실패는 0.5~30초 간격으로 재시도한다. 큐 한도와 디스크 장애까지 포함한 무제한 무손실 보장은 제공하지 않는다.

큐 포화·쓰기 실패 시 현재 이벤트 한 건과 이미 만든 스냅샷을 유지하고 추가 추론·스냅샷 생성을 멈춘다. 0.5~5초 간격으로 같은 이벤트의 영속화를 재시도한다. 포화 대기 이미지의 경로·이벤트 키는 카메라별 한 행으로 `waiting_observations`에 별도 보존하여 Data 정리로 삭제되지 않게 한다. 큐 공간이 생기거나 다른 publisher/CLI가 목록을 갱신해도 참조는 유지되고 같은 카메라의 다음 큐 등록이 성공하면 해제된다. `event_backpressure`·`event_delivery_error`·`event_persistence_failures`, 큐의 `event_delivery.pending/rejected/waiting/last_error`를 확인한다. 종료까지 이벤트 본문을 영속화하지 못한 항목은 `event_shutdown_losses`와 ERROR 로그에 남기며 재시작 후 자동 복원을 보장하지 않는다. 대기 참조만 남은 카메라의 이미지는 다음 성공 등록까지 보수적으로 보호한다. 큐 상한은 JPEG 파일 총용량과 카메라별 대기 참조 행을 포함하지 않는다.

활성 카메라 목록을 정상 조회한 뒤 실제 생산자도 종료된 카메라의 고아 대기 참조는 해제한다. 중지 요청 뒤에도 생산자가 살아 있거나 목록 조회가 실패하면 보호를 유지한다. 참조 삭제를 먼저 확정한 뒤 보호 목록을 갱신하며, 큐의 미전송·거부 이벤트나 JPEG 파일을 이 단계에서 직접 삭제하지 않는다.

사람 crop 저장 실패도 해당 프레임을 유지한 채 추가 추론을 멈추고 0.5~5초 간격으로 재시도한다. 저장 성공 전에 관측 없는 등장 이벤트를 대신 보내지 않는다. `observation_error`·`observation_persistence_failures`에 실패가 드러나며, 지연 후 전송해도 등장·퇴장 시각은 재시도 완료 시각이 아닌 원래 프레임 수신 시각이다.

Data의 보존 정리와 조정하기 위해 `.pending-observations.json`에 미전송·거부·포화 대기 항목의 이미지 경로와 이벤트 중복제거 키를 원자적으로 기록한다. 송신 큐 변경 및 30초 heartbeat마다 갱신한다. 새 항목은 목록을 먼저 게시하지 못하면 DB에 확정하지 않고, 전달 완료 항목은 DB 삭제 확정 뒤 목록을 줄인다. 후속 목록 갱신이 실패하면 이전 목록이 더 오래 보호하며 재시도한다. 보호 목록은 `schema_version:2`와 명시적 `complete`를 사용한다. 새 Data는 기존 v1도 읽고 구형 Data는 v2 정리를 보류하므로 먼저 갱신된 Preprocessing의 불완전 목록을 정상으로 오해하지 않는다. Data는 목록이 없거나 손상·불완전하거나 120초 이상 오래되면 이미지·이벤트 정리를 보류한다. 목록도 공통 상한 64 MiB·이벤트 100,000개·경로 300,000개를 적용하며, 초과 시 경로를 잘라 정상 목록으로 표시하지 않고 `complete:false`인 작은 보류 표식을 게시한다. 범위 안으로 돌아오면 완전한 목록을 다시 게시한다. 생성 중 파일은 최소 24시간 보호하지만 장기 대기는 별도 참조로 보호한다. 대표 프레임 선택 중인 메모리 후보는 갑작스러운 프로세스 종료 때 복구되지 않으며, 영속 전달 보장은 outbox 기록 이후부터 적용된다.

영구 거부 원인을 해결한 뒤 아래 컨테이너 내부 CLI로 최대 100건씩 조회하고 선택 항목을 다시 보낼 수 있다. `discard`는 의도적으로 이벤트를 폐기하므로 sequence를 확인하고 `--confirm-discard`를 명시해야 한다. JPEG는 직접 지우지 않으며 이후 Data 보존 정리가 처리한다.

```powershell
docker compose --env-file server/.env -f server/compose.yml exec preprocessing python -m server.services.preprocessing.app.outbox_admin list
docker compose --env-file server/.env -f server/compose.yml exec preprocessing python -m server.services.preprocessing.app.outbox_admin retry --sequence 1
```

서비스 테스트는 [OSNet 입력·출력 계약](tests/test_osnet_identity.py), 손상·상수 크롭 거부, 작업 처리와 이전 [외관 특징 호환 테스트](tests/test_local_identity.py)를 포함한다. 모델 대역 테스트와 실제 가중치 추론은 구분한다. 로컬 개발 Python 환경에서는 다음으로 재현한다.

```powershell
python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests -q
```

공식 가중치로 ONNX를 실제 생성하고 PyTorch 2.8.0 CPU와 OpenCV 4.11의 세 입력 출력을 비교했다. 변환 기록 `.onnx.json`에는 해시·버전·검증 수치가 남는다. 자세한 재현 방법은 [모델 준비 도구 안내](../../tools/README.md)를 따른다. 이 확인은 변환·실행 호환성의 근거이며 CCTV 재식별 정확도를 보증하지 않는다. Docker·실제 카메라에서의 정확도·처리량·전체 기동은 별도 검증이 필요하다. 준비한 YOLO와 OSNet을 읽기 전용 모델 폴더에 두고 [Preprocessing 인수 문서](../../../docs/SRS_interface_preprocessing.md)의 실제 프레임·등장 이벤트·crop·전역 연결·장애 복구 절차를 수행한다.
