# Preprocessing

하나의 컨테이너가 카메라 감지와 전역 인물 연결 작업을 실행합니다. 감지는
카메라별 스레드에서, 인물 연결은 별도 비동기 작업과 모델 실행 스레드에서
수행합니다. 인물 연결 초기화·분석 실패가 감지 작업을 중단시키지 않습니다.

## 담당 개발자가 구현할 부분

| 연결부 | 기본 구현 | 교체 방법 |
|---|---|---|
| 감지·추적 | `processors/detection/yolo.py`의 YOLO/ByteTrack | `DETECTION_PLUGIN=패키지.모듈:팩토리` |
| 전역 인물 연결 | `processors/identity/`의 `IdentityBlackBox` | `IDENTITY_PLUGIN=패키지.모듈:팩토리` |

감지는 기존 구현을 참고용 기본 처리기로 유지합니다. 모델을 읽지 못하면 상태를
표시하고 영상 연결 점검만 계속합니다. 인물 연결 기본 처리기는 항상
`unconfigured`를 반환하며 `global_person_id`를 만들지 않습니다. 알고리즘 교체에
Data, External, 모바일의 수정은 필요하지 않도록 아래 계약을 유지하십시오.

## 감지 계약 v1

`processors/detection/contracts.py`에 `DetectionFrame`, `DetectionResult`,
`DetectionProcessor`가 정의되어 있습니다. 팩토리는
`factory(model_path: Path, confidence: float, device: str)` 형태로 호출합니다.

```python
class MyDetector:
    def __init__(self, model_path, confidence, device):
        # 모델을 로딩하고 카메라별 추적 상태를 초기화합니다.
        ...

    def reset(self):
        # 새 영상 연결에서 이전 추적 상태를 폐기합니다.
        ...

    def process(self, frame: DetectionFrame) -> DetectionResult:
        # frame.image: 원본 H x W x 3 uint8 BGR 배열; 수정하지 않습니다.
        # 실제 감지 결과를 DetectionResult(objects=[...])로 반환합니다.
        ...
```

입력은 `schema_version=1`, `camera_id`, `tracking_session_id`, 시간대가 포함된
`observed_at`, `image`입니다. 이미지 배열은 프로세스 내부 입력이며 JSON으로
직렬화되지 않습니다. 출력은 `schema_version=1`, 최대 100개의 `objects`입니다.
각 객체는 문자열 `person_id`, 원본 영상 픽셀 기준 `[x1,y1,x2,y2]` 바운딩 박스,
`0..1` 범위 `confidence`를 가집니다. 경계 밖 좌표는 프레임에 맞춰 자르고 면적이
없는 박스와 중복 ID는 제거합니다. 신원 연결은 이 처리기에 포함하지 않습니다.

`person_id`는 카메라의 현재 추적 세션에서만 유효합니다. 영상 재연결 때
`tracking_session_id`를 새로 발급하고 `reset()`을 호출합니다. 영구 식별에는
`camera_id + tracking_session_id + person_id`를 사용해야 합니다.

실행 골격은 등장·사라짐 이벤트, 전체 스냅샷, 사람 크롭과 박스 이미지,
실시간 객체 좌표를 Data 내부 API에 전달합니다. 새 영상 프레임의 좌표는 최대
초당 2회 전송하며 오래된 대기 프레임은 버립니다. 모바일 HLS 재생과 좌표 사이에
지연 차이가 생길 수 있습니다. 이벤트 전송 실패는 상태로 기록하며 영상 소비를
중단하지 않습니다. 현재 감지 이벤트 자체에는 디스크 기반 송신 재시도가 없습니다.

## 인물 연결 계약

공통 스키마는 `ai_cctv_core.contracts.objects`, 작업 실행기는
`ai_cctv_core.processing.worker.ObjectWorker`에 있습니다. 팩토리는 인자 없이
생성되고 `process(job: dict, crop_path: Path) -> dict`를 구현합니다.

`job`에는 작업 `id`, `event_id`, `camera_id`, `person_id`, `lease_id`,
`object_observation`이 포함됩니다. 관측 스키마 v1에는 추적 세션, 박스, 원본
프레임 크기, 크롭 상대 경로가 포함됩니다. `crop_path`는 공유 스냅샷 저장소 안의
검증된 파일 경로이며 이미지를 변경하지 않아야 합니다.

반환값은 `outcome`과 선택적인 `global_person_id`, `metadata`입니다.
`outcome`은 `complete`, `retry`, `failed`, `unconfigured` 중 하나이며 전역 ID는
`complete`일 때만 가능합니다. `lease_id`는 실행기가 추가하므로 반환하지 않습니다.
metadata는 JSON으로 최대 64 KiB입니다. 미구현 기본 처리기는 다음을 반환합니다.

```json
{"outcome":"unconfigured","metadata":{"reason":"identity_backend_not_implemented"}}
```

Data는 등장 이벤트와 인물 연결·분석 작업을 같은 트랜잭션에 저장합니다. 작업을
`POST /object-jobs/identity/claim`으로 얻고
`POST /object-jobs/identity/{id}/complete`로 반환합니다. 경로는
`DATA_SERVICE_URL`에 대한 상대 경로입니다. 작업은 중복 실행될 수 있으므로
알고리즘의 외부 부수 효과에는 작업 ID를 중복 방지 키로 사용하십시오. 만료된
임대의 결과는 Data가 무시합니다. 미구현 작업은 알고리즘 배포 후
`POST /object-jobs/identity/requeue-unconfigured`로 재처리할 수 있습니다.

모델 실행 제한은 120초입니다. 시간 초과된 동기 모델은 강제로 종료할 수 없으므로
해당 인물 연결 작업기는 추가 호출을 멈추고 상태를 알립니다. 감지는 계속됩니다.
모델 구현에서 자체 시간 제한·종료 처리를 제공하고, 멈춘 작업기는 컨테이너를
재시작해 복구하십시오.

## 설정과 검증

- `DATA_INFERENCE_TOKEN`: 감지의 이벤트·상태·실시간 좌표용 제한 토큰
- `DATA_IDENTITY_TOKEN`: 인물 연결 작업만 처리하는 별도 제한 토큰
- `DATA_SERVICE_URL`, `SNAPSHOTS_ROOT`: Data 접근 주소·스냅샷 공유 경로
- `MEDIA_READ_USERNAME`, `MEDIA_READ_PASSWORD`: RTSP 읽기 전용 인증
- `INFERENCE_ENABLED`, `MODEL_PATH`, `INFERENCE_DEVICE`, `INFERENCE_CONFIDENCE`,
  `ANALYSIS_FPS`, `DISAPPEAR_SECONDS`: 기존 감지 설정을 유지
- 기본 플러그인 경로:
  `server.services.preprocessing.processors.detection.yolo:YoloTracker`,
  `server.services.preprocessing.processors.identity:IdentityBlackBox`

컨테이너는 SQLite를 직접 열지 않습니다. `/health/live`는 실행 여부,
`/health/ready`는 Data·감지·인물 연결 상태를 제공합니다. 인물 연결 장애는
`degraded`로 표시합니다. `identity.last_outcome=unconfigured`는 알고리즘이 아직
구현되지 않았음을 뜻합니다. 상태 조회는 내부 네트워크에서만 제공합니다.

저장소 루트에서 `python -m pytest server/services/preprocessing/tests`로 모델 없이
계약과 생명주기 분리를 검증합니다. 모델·RTSP·실기기 정확도는 별도 검증 대상입니다.
기본 실행은 Compose를 사용하며, 직접 실행 시 저장소 루트와 `src/`를
`PYTHONPATH`에 포함하고 `uvicorn server.services.preprocessing.app.main:app`을
실행합니다.
