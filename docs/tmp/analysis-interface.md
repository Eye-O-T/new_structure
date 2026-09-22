# Analysis 인터페이스

Data에서 사람 크롭 작업을 가져와 분석 결과를 반환한다. 새 컨테이너가 유지할 실행·입출력 계약이다.

## 실행 환경

| 항목 | 값 |
|---|---|
| 서비스·네트워크 | Compose `analysis`, `internal` |
| Data 주소 | `DATA_SERVICE_URL=http://nginx:8080/internal/data/v1` |
| 인증 | `X-Internal-Token: <DATA_ANALYSIS_TOKEN>` |
| 이미지·모델 | `/snapshots`, `/models` 읽기 전용 |
| 실행 사용자 | `AI_CCTV_UID:AI_CCTV_GID`, 기본 `1000:1000` |
| 상태 API | 내부 `0.0.0.0:8000`의 `/health/live`, `/health/ready` |
| 시간 제한 | 초기화 기본 30초, 모델 호출 기본 120초 |

`/health/live`는 생존 시 200이다. `/health/ready`는 작업 수신 불가·정지 시 503이며, 최근 오류·모델 미준비는 200 `degraded`일 수 있다. `ready`, `stalled`, `backend`, `model_ready`, `last_error`, `last_outcome`을 제공한다.

## 작업 API

경로는 Data 주소 뒤에 붙인다. JSON 요청은 `Content-Type: application/json`을 사용한다.

| 요청 | 응답 |
|---|---|
| `POST /object-jobs/analysis/claim` (본문 없음) | `{"job":{...}}` 또는 `{"job":null}` |
| `POST /object-jobs/analysis/{job.id}/complete` | `{"accepted":true}` 또는 `false` |
| `POST /object-jobs/analysis/requeue-unconfigured` (본문 없음) | `{"requeued":건수}`, 최대 100건 |

### 작업 입력

```json
{
  "job": {
    "id": 42,
    "event_id": 120,
    "attempts": 0,
    "camera_id": "cam-001",
    "person_id": "7",
    "global_person_id": null,
    "occurred_at": "2026-09-07T01:00:00Z",
    "object_observation": {
      "schema_version": 1,
      "tracking_session_id": "0123456789abcdef0123456789abcdef",
      "object_class": "person",
      "bbox": [100, 50, 300, 600],
      "frame_width": 1280,
      "frame_height": 720,
      "crop_path": "cam-001/crop.jpg",
      "annotated_snapshot_path": null
    },
    "lease_id": "abcdef0123456789abcdef0123456789"
  }
}
```

`attempts`는 이번 claim 이전 횟수(0~4)다. 완료 URL에는 작업 `id`를 사용한다. `global_person_id`는 아직 없을 수 있으며 identity 완료를 기다리지 않는다. 입력은 크롭 한 장이며 전체 이벤트 metadata·연속 영상은 제공되지 않는다.

### 완료 출력

```json
{
  "lease_id": "abcdef0123456789abcdef0123456789",
  "outcome": "complete",
  "metadata": {"backend": "my-analysis", "attributes": {"coat_color": "blue"}}
}
```

| outcome | 처리 |
|---|---|
| `complete` | 성공 |
| `unconfigured` | 모델 미연결; 명시적 재등록 전까지 재시도 없음 |
| `retry` | 일시 오류; 남은 횟수 내 재시도 |
| `failed` | 영구 오류; 자동 재시도 없음 |

`metadata`는 JSON 객체이며 NaN·Infinity를 허용하지 않는다. Python `json.dumps(metadata, allow_nan=False).encode("utf-8")` 기준 65,536바이트 이하여야 한다. `global_person_id`·`identity_descriptor`는 생략하거나 null만 사용한다. 정의되지 않은 최상위 필드는 금지한다.

Data는 `metadata.analysis`에 `status`, `updated_at`, `result`를 저장한다. 다른 단계 결과를 보존하고 새 이벤트·푸시는 만들지 않는다.

## 작업 처리

- 임대는 5분이며 연장 API는 없다. 총 claim은 최대 5회, `retry` 대기는 30·60·120·240초다.
- 완료는 유효한 현재 `lease_id`로만 수락된다. 만료·중복·다른 단계의 완료는 HTTP 200 `accepted:false`다.
- 응답 유실 시 같은 lease·본문을 재전송한다. `false`만으로 이전 요청의 성공 여부는 알 수 없다.
- 중단된 작업은 임대 만료 후 다시 처리될 수 있다. 외부 쓰기가 있다면 작업 ID로 중복을 막는다.
- `requeue-unconfigured`는 시도 횟수를 초기화한다. 재시작만으로 재등록되지 않으며 `failed` 재등록 API는 없다.
- HTTP·모델 호출에 시간 제한을 두고 종료 시 새 claim을 멈춘다. 모델을 안전하게 종료할 수 없으면 준비 실패로 전환한다.

401·403은 토큰·역할, 422는 입력, 409는 결과 충돌을 확인한다. 연결 오류·5xx는 임대 기한 안에서 재시도한다.

## 이미지 계약

`crop_path`는 `/snapshots` 기준 상대 경로다. 심볼릭 링크 해석 후에도 저장소 내부의 일반 파일인지 검사한다. 경로 이탈·누락·손상은 `failed`로 보고한다.

크롭은 박스 없는 JPEG이며 8 MiB·1,600만 픽셀 이하다. 크기는 `(x2-x1)×(y2-y1)`과 일치해야 하며 원본 bbox로 다시 자르지 않는다. [공용 JPEG 로더](../../lib/ai_cctv_core/processing/images.py) 또는 동등한 검증을 사용한다. 입력 파일과 outbox·보호 목록은 수정하지 않는다.

## 교체와 인수

1. `server/services/analysis/`의 구현·Dockerfile과 Compose의 빌드·실행·healthcheck를 맞춘다. Python 없는 이미지라면 기존 Python healthcheck도 바꾼다.
2. [배포 안내](../deployment-guide.md)에서 이미지를 빌드·재생성한다.
3. 실제 공유 크롭을 처리하여 기존 이벤트의 `metadata.analysis.result`와 기대 결과를 대조한다.
4. 입력 오류, 단계 독립성, Data 장애, 임대 만료·중복 완료, 모델 시간 초과·재시작을 확인한다.

이미지·모델 버전과 실제 결과를 기록한다. 기본 분석기의 출력·실행 방법은 [서비스 README](../../server/services/analysis/README.md), 정확한 필드와 검증 규칙은 [객체 계약](../../lib/ai_cctv_core/contracts/objects.py)과 [작업 API](../../server/services/data/app/api/objects.py)를 따른다.
