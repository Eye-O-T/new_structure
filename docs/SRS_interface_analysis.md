# Analysis 컨테이너 교체 인수인계

이 문서는 **새 Analysis 컨테이너를 제작하는 담당자와 인수 검증 담당자**를 위한 임시 문서다. 교체 구현과 인수가 끝나면 마지막 절의 정보를 컴포넌트 README·테스트로 옮긴 뒤 삭제한다. 현재는 삭제하지 않는다.

## 처음 5분: 무엇을 만들면 되는가

**사람 영역 이미지 한 장과 관측 정보를 받아 측정 가능한 외관 metadata를 만드는 컨테이너**다. 현재 기본 `LocalAppearanceAnalyzer`는 OpenCV·NumPy CPU로 상·하의 후보 영역의 대표색과 영상 품질을 실제 측정한다. 교체 언어·모델은 선택할 수 있으며, 이전 `MetadataBlackBox`를 명시적으로 선택하면 종전처럼 `unconfigured`를 반환한다.

| 바로 확인할 것 | 이번 담당 범위 |
|---|---|
| 입력 | Data에서 claim한 작업 JSON과 공유 폴더의 사람 크롭 JPEG |
| 출력 | 작업 complete API에 보고할 `outcome`과 `metadata` JSON 객체 |
| 담당 범위 밖 | 사람 detection·추적, identity·전역 인물 ID, 앱, DB 직접 접근, 새 이벤트·푸시 생성 |
| 처리 제한 | 작업 임대 5분, 총 claim 5회. HTTP 200이어도 `accepted:false`이면 미반영 |
| 교체 위치 | `server/services/analysis/`의 프로그램·Dockerfile, `server/compose.yml`의 `analysis` 서비스 |
| 완료 기준 | 실제 새 이미지가 실행되고, 실제 크롭을 읽어 기존 이벤트의 `metadata.analysis`에 모델 결과가 저장됨 |

읽는 순서는 [배포 계약](#1-배포-계약) → [작업 API](#2-작업-api) → [실패·이미지 규칙](#3-작업-처리와-실패-규칙) → [제작·교체](#4-새-컨테이너-제작과-교체) → [인수](#5-인수-검증)다. 환경 파일·인증서·Docker 준비만 [소스 배포 안내](guide.md#소스-배포)를 참고한다.

### 이 컨테이너가 연결되는 시스템

Raspberry Pi가 보낸 영상을 Preprocessing이 감지·추적하여 사람 크롭과 등장 이벤트를 만든다. Data는 이벤트와 함께 identity·analysis 작업을 각각 저장한다. Analysis는 **Nginx를 통해 Data의 내부 HTTP API를 호출**하고, 크롭 파일은 공유 폴더에서 읽는다. Data가 Analysis로 작업을 밀어 넣거나 Preprocessing이 Analysis를 직접 호출하지 않는다.

identity는 Preprocessing 내부에서 별도로 실행된다. Analysis의 입력 `global_person_id`는 아직 `null`일 수 있으며, 분석 도중 바뀔 수도 있다. identity 완료를 기다리지 않는다. 현재 입력은 등장 뒤 대표 프레임 선택 창에서 확정한 정지 이미지다. 기본 선택 창은 1초이며 이벤트의 `occurred_at`은 최초 등장 시각을 유지한다. 연속 영상·감지 confidence·다른 이벤트 전체 metadata는 제공되지 않는다. 연속 동작이 필요한 분석은 이 입력만으로 가능하다고 가정하지 않는다.

### 현재 기본 분석기가 제공하는 결과

`metadata`에는 `backend:local_appearance`, `backend_version:1.0.0`, `schema_version:1`, `image`, `clothing_colors`, `evidence`를 담는다. `image`는 원본 크기와 긴 변 최대 256픽셀인 표본의 밝기·대비·포화 픽셀 비율·Laplacian 분산을 기록한다. `clothing_colors.upper/lower`는 중앙 상·하체 영역의 색명·중앙값 RGB·원래 픽셀 비율·가중 비율·색상 일관성 지표와 상위 세 색을 제공한다. HSV 밝기·채도로 무채색을 구분하고, 양옆과 중앙의 색이 다를 때 배경 후보색의 가중치를 낮춘다.

이 값은 의복을 분할한 결과가 아니라 위치로 정한 영역의 색 추정이다. 자세·가림·조명·JPEG 손실·배경색에 영향을 받는다. `confidence`는 정답 확률이 아니고 `low_edge_energy`는 흐림 확정값이 아니다. 단일 이미지로 행동·보이지 않는 속성·나이·성별·인종을 추정하지 않는다. 세부 필드와 실제 JPEG 테스트는 [Analysis README](../server/services/analysis/README.md#기본-분석-결과의-의미), [기본 분석기 테스트](../server/services/analysis/tests/test_local_appearance.py)를 따른다.

## 1. 배포 계약

| 항목 | 반드시 맞출 값·동작 |
|---|---|
| Compose 서비스 | 이름 `analysis`, 기존 `internal` 네트워크 유지. 호스트 공개 포트 추가 없음 |
| Data 주소 | `DATA_SERVICE_URL=http://nginx:8080/internal/data/v1` |
| 인증 | `DATA_ANALYSIS_TOKEN`을 `X-Internal-Token` 헤더로 전송. Data의 동명 토큰과 일치하는 32자 이상 값 |
| 비밀 파일 | `${ANALYSIS_SECRETS_FILE:-./secrets/analysis.env}`. 다른 서비스의 토큰은 추가하지 않음 |
| 이미지 | `SNAPSHOTS_ROOT=/snapshots`, `${SNAPSHOTS_DIR:-./runtime/snapshots}` → `/snapshots`, **읽기 전용** |
| 플러그인 | `ANALYSIS_PLUGIN=server.services.analysis.processors:LocalAppearanceAnalyzer`가 기본 |
| 모델 | `${MODELS_DIR:-./runtime/models}` → `/models`, **읽기 전용**. 기본 분석기는 모델 파일이 필요 없음 |
| 실행 제한 | `OBJECT_MODEL_TIMEOUT_SECONDS=120`, `0 < 값 ≤ 240`; `OBJECT_STARTUP_TIMEOUT_SECONDS=30`, `0 < 값 ≤ 120` |
| 실행 사용자 | Compose의 `${AI_CCTV_UID:-1000}:${AI_CCTV_GID:-1000}`. Dockerfile의 `USER`보다 Compose 설정이 우선함 |
| 상태 서버 | 컨테이너 내부 `0.0.0.0:8000`, 아래 health 경로. 인증 없이 HTTP JSON 제공 |
| 시간 | `TZ=UTC`. 이벤트 시각은 시간대가 있는 ISO 8601 문자열 |
| 필요 없는 마운트 | DB·녹화·`config.yaml`. 입력 폴더 수정 금지 |

배포 env는 소스의 `server/.env` 또는 설치본의 실제 `config/compose.env`다. 그 안의 상대 경로는 `server/` 기준이다. 호스트의 `SNAPSHOTS_DIR/cam-001/crop.jpg`는 컨테이너의 `/snapshots/cam-001/crop.jpg`가 된다. 폴더는 기동 전에 있어야 하며 실행 UID가 파일을 읽을 수 있어야 한다.

Analysis에는 `MODEL_PATH`가 자동 주입되지 않으며 기본 분석기는 다운로드를 수행하지 않는다. 학습 모델로 교체할 때는 검증한 모델 파일과 전처리·출력 계약을 배포 전에 준비하고 읽기 전용 `/models`에 쓰지 않는다. 내부 요청에 시스템 HTTP 프록시를 사용하지 말고 토큰을 소스·이미지·로그·결과에 넣지 않는다.

### 상태 확인 계약

| 요청 | 응답 |
|---|---|
| `GET /health/live` | 200, `{"status":"alive","service":"analysis"}` |
| `GET /health/ready` | 작업 수신이 가능하고 멈추지 않았으면 200, 아래 JSON |
| 준비 실패 | 503; `detail` 객체에 `status:unavailable`과 동일한 작업 상태 필드를 포함 |

```json
{"status":"ready","ready":true,"stalled":false,"last_error":null,"last_outcome":"complete","backend":"server.services.analysis.processors:LocalAppearanceAnalyzer","model_ready":true}
```

`status`는 `ready` 또는 마지막 오류·모델 미준비가 남은 `degraded`, `last_error`는 오류 코드 또는 `null`, `last_outcome`은 `complete`·`unconfigured`·`retry`·`failed`·`rejected` 또는 초기 `null`이다. 200이면 `ready=true`, `stalled=false`다. 초기화 실패는 `ANALYSIS_STARTUP_FAILED`로 드러나고 30초 뒤 재시도한다. `rejected`는 완료 거절의 진단값이며 API에 보내는 outcome이 아니다. **준비 상태 200은 모델 연결·분석 정확도까지 보증하지 않는다.**

`backend`는 선택한 플러그인 참조, `model_ready`는 실행 경로의 준비 상태다. 초기화 실패와 자식 재생성 중에는 준비 상태가 달라질 수 있다. 기본 CPU 구현의 성공은 실제 색·품질 측정 완료를 뜻하며 학습된 의복 인식 모델의 정확도 검증을 뜻하지 않는다.

현재 Compose 점검은 Python으로 `/health/ready`를 호출한다. **Python 없는 이미지로 교체하면 이 점검 명령도 교체해야 한다.** 기본 간격 10초, 명령 제한 5초, 실패 6회, 시작 유예 20초다. 모델 로딩 시간에 맞게 시작 유예를 조정한다. `unhealthy` 표시만으로 컨테이너가 자동 재시작되지는 않는다.

## 2. 작업 API

모든 경로는 `DATA_SERVICE_URL` 뒤에 붙인다. 작업 API에는 `X-Internal-Token: <DATA_ANALYSIS_TOKEN>`, JSON 본문에는 `Content-Type: application/json`을 사용한다. Analysis 토큰으로 다른 단계·일반 관리 API를 호출할 수 없다.

| 호출 | 요청 본문 | HTTP 200 응답 |
|---|---|---|
| `POST /object-jobs/analysis/claim` | 없음 | `{"job": {...}}` 또는 `{"job":null}` |
| `POST /object-jobs/analysis/{job.id}/complete` | 완료 JSON | `{"accepted":true}` 또는 `{"accepted":false}` |
| `POST /object-jobs/analysis/requeue-unconfigured` | 없음 | `{"requeued":100}`처럼 재등록 건수 |

### claim 입력: 대표 유효 응답

```json
{
  "job": {
    "id": 42,
    "event_id": 120,
    "attempts": 0,
    "camera_id": "cam-001",
    "person_id": "7",
    "global_person_id": null,
    "occurred_at": "2026-09-07T01:00:00.000Z",
    "object_observation": {
      "schema_version": 1,
      "tracking_session_id": "0123456789abcdef0123456789abcdef",
      "object_class": "person",
      "bbox": [100, 50, 300, 600],
      "frame_width": 1280,
      "frame_height": 720,
      "crop_path": "cam-001/2026/09/07/example_crop.jpg",
      "annotated_snapshot_path": null
    },
    "lease_id": "abcdef0123456789abcdef0123456789"
  }
}
```

| 필드 | 의미·제약 |
|---|---|
| `id`, `event_id` | 정수 작업 ID와 이벤트 ID. **완료 URL에는 `id` 사용** |
| `attempts` | 이번 claim **이전** 횟수 0~4. 첫 응답은 0이며 Data 내부 횟수는 이미 1 증가함 |
| `camera_id` | 1~64자, 소문자·숫자로 시작하고 이후 소문자·숫자·`_`·`-` 허용 |
| `person_id` | 1~256자 문자열. 카메라·추적 세션 안에서만 유효 |
| `global_person_id` | 문자열 또는 `null`. claim 시점의 값이며 Analysis가 지정·변경하지 않음 |
| `occurred_at` | 원본 이벤트의 UTC 시각. 작업 수신 시각이 아님 |
| `lease_id` | 소문자 16진수 32자리. 받은 값을 그대로 완료 요청에 사용 |
| `object_observation` | 아래 관측 객체. 이벤트 전체 metadata가 아님 |

위 job 필드는 모두 반환된다. `stage`, `lease_until`, 영상 바이트는 반환되지 않는다. 로컬 추적을 구분할 때는 `(camera_id, tracking_session_id, person_id)`를 함께 사용한다.

| 관측 필드 | 의미·제약 |
|---|---|
| `schema_version`, `object_class` | 현재 `1`, `"person"`만 지원. 새 버전을 임의로 기존 버전으로 해석하지 않음 |
| `tracking_session_id` | 소문자 16진수 32자리. 재접속·추적 초기화 때 변경됨 |
| `frame_width`, `frame_height` | 원본 프레임 크기, 각각 정수 1~16384 |
| `bbox` | 정수 4개 `[x1,y1,x2,y2]`. `0 ≤ x1 < x2 ≤ width`, `0 ≤ y1 < y2 ≤ height` |
| `crop_path` | 1~4096자, 스냅샷 저장소의 상대 경로 |
| `annotated_snapshot_path` | 최대 4096자의 상대 경로 또는 `null`. 박스가 그려진 전체 이미지이며 분석 필수 입력 아님 |

좌표 원점은 왼쪽 위이고 x는 오른쪽, y는 아래로 증가한다. 위 예제의 크롭 크기는 **200×550**이다. Data는 생산자가 생략한 `schema_version=1`, `object_class=person`, `annotated_snapshot_path=null`을 채워 전달한다. 관측에 정의되지 않은 추가 필드는 허용하지 않는다.

### complete 출력: 작업 42의 유효 요청

`POST /object-jobs/analysis/42/complete`에 다음 본문을 보낸다.

```json
{
  "lease_id": "abcdef0123456789abcdef0123456789",
  "outcome": "complete",
  "metadata": {
    "backend": "my-analysis",
    "version": "handoff-v1",
    "attributes": {"coat_color": "blue"}
  }
}
```

| 필드 | 규칙 |
|---|---|
| `lease_id` | 필수. claim과 동일한 값 |
| `outcome` | 필수. 아래 4개 중 하나 |
| `metadata` | JSON 객체. 생략하면 `{}`. 배열·`null` 자체는 불가 |
| `global_person_id` | **생략하거나 `null`만 허용**. Analysis는 문자열 ID를 보내지 않음 |
| `identity_descriptor` | **생략하거나 `null`만 허용**. 특징 추출·gallery 연결은 identity 단계의 책임 |

최상위 추가 필드는 금지다. metadata 안의 필드명·값·단위는 담당자가 정의하며 예제의 `backend`·`attributes`는 고정 스키마가 아니다. 내부 값은 일반 JSON 값만 사용하고 NaN·Infinity는 금지한다. Data의 **Python `json.dumps(metadata, allow_nan=False).encode("utf-8")` 기준 65,536바이트 이하**여야 한다. 한글 이스케이프·공백을 포함하는 계산이므로 압축 전송 크기와 다르다. Nginx 전체 본문 한도 2 MiB도 적용된다.

| `outcome` | 사용 시점 | 수락 후 Data 상태 |
|---|---|---|
| `complete` | 모델 분석 성공 | `complete` |
| `unconfigured` | 모델 미연결 | `unconfigured`, 자동 재시도 없음 |
| `retry` | 일시적 모델·자원 장애 | 횟수가 남으면 `pending`, 5번째는 `failed` |
| `failed` | 잘못된 입력·손상 이미지 등 영구 오류 | `failed` |

`accepted:true`이면 Data가 최신 이벤트의 `metadata.analysis`만 `{"status":상태,"updated_at":UTC시각,"result":요청 metadata}`로 교체한다. 결과를 `analysis`로 한 번 더 감싸서 보내지 않는다. 관측·identity 결과는 보존된다. 새 이벤트나 추가 푸시는 생성되지 않으며 기존 이벤트를 다시 조회해 결과를 확인한다.

### 오류 처리

| 응답 | 처리 |
|---|---|
| 401 `INVALID_INTERNAL_TOKEN` | 토큰 누락·불일치 확인 |
| 403 `INTERNAL_SCOPE_FORBIDDEN` | 다른 역할 토큰·잘못된 경로 사용 확인 |
| 503 `INTERNAL_TOKEN_NOT_CONFIGURED` | Data의 Analysis 토큰 설정 확인 |
| 422 `VALIDATION_ERROR` | JSON 필드·형식·크기 수정 |
| 409 `OBJECT_RESULT_CONFLICT` | 유효 lease의 `complete`에 전역 ID를 지정한 경우 등 결과 계약 확인 |
| 연결 실패·기타 5xx | 간격을 두고 재연결. 완료 응답 유실과 lease 만료를 고려 |

Data 오류는 `{"error":{"code":"...","message":"...","details":{}}}` 형식이다. 검증 오류의 `details`는 `location`·`message`·`type`을 가진 항목 배열이다. Nginx 오류는 JSON이 아닐 수 있다. 오류 메시지 원문에 의존하거나 예외·토큰을 결과 metadata에 복사하지 않는다.

## 3. 작업 처리와 실패 규칙

1. 처리할 여유가 있을 때 claim한다. `job:null`이면 대기하며 과도하게 반복 호출하지 않는다.
2. claim마다 새 lease가 발행되며 **임대는 5분, 연장 API는 없다.** 모델 실행과 결과 보고까지 이 안에 끝낸다. 작업을 미리 많이 가져와 로컬 대기열에 쌓지 않는다.
3. `running` 작업의 lease가 일치하고 아직 유효할 때만 완료가 수락된다. 만료·이전 lease·중복 완료·없는 ID·다른 단계 작업은 HTTP 200과 `accepted:false`다.
4. 완료 응답 유실 시 같은 lease·본문을 재전송할 수 있다. 현재 실행기는 완료 결과를 메모리에 보관해 재전송 중 다시 분석하지 않는다. 첫 요청이 반영됐다면 재전송은 `false`이므로, 이 값만으로 이전 성공 여부까지 판별할 수 없다. `last_outcome:rejected`는 유지하되 이후 정상 빈 claim에서 해당 완료 거부 오류를 지워 유휴 상태를 영구 장애로 표시하지 않는다. 별도 작업 상태 조회 API는 없다.
5. 중단된 작업은 임대 만료 후 다음 claim에서 회수된다. **중복 분석이 가능**하므로 외부 쓰기가 있다면 작업 ID로 중복을 막는다.

claim은 총 5회까지다. `retry` 후 1~4번째 대기 시간은 30·60·120·240초다. 5번째 시도의 `retry`는 실패로 종료한다. 5번째 lease가 만료된 경우 다음 claim이 작업과 이벤트 `metadata.analysis`를 함께 `failed`로 정리하고 `result.error_code:ATTEMPTS_EXHAUSTED`를 기록한다. claim 시에는 `running`, 재등록 시에는 `pending`으로 이벤트 상태도 같은 트랜잭션에서 갱신한다. 기존 버전에서 남은 상태 불일치는 이벤트 조회에서도 현재 작업 상태로 보정한다. 작업 생성·전이 이후 상태 객체에는 `status`, `updated_at`, `result`가 있으며 `running`은 완료 요청에 보내는 outcome이 아니다.

모델 연결 뒤에는 `/object-jobs/analysis/requeue-unconfigured`로 기존 `unconfigured` 작업을 다시 대기시킨다. 호출당 최대 100건, 횟수는 0으로 초기화한다. 필요하면 `requeued=0`까지 반복하며 크롭이 남아 있는지 확인한다. 재등록 시 이벤트 metadata도 `pending`으로 갱신하되 이전 result는 다음 결과 수락까지 보존한다. 재시작만으로 재등록되지 않으며 `failed`용 재등록 API는 없다.

### 공유 이미지와 시간 제한

- `SNAPSHOTS_ROOT`와 상대 경로를 결합하고 `..`·심볼릭 링크를 해석한 실제 경로가 저장소 내부의 일반 파일인지 확인한다. URL·저장소 밖 절대 경로를 열지 않는다.
- 사람 크롭은 박스가 없는 JPEG다. 공용 로더는 파일 8 MiB·1,600만 픽셀 상한을 JPEG 헤더에서 먼저 검사하고 디코딩 후 크기가 `(x2-x1)×(y2-y1)`인지 다시 확인한다. EXIF 회전 없이 저장된 BGR 픽셀을 사용한다. 기본 분석기는 각 변 16픽셀 이상 및 축소 표본의 최소 폭도 검사한다. 이미 잘린 이미지에 원본 bbox로 다시 자르지 않는다.
- Data는 pending·running 작업이 참조하는 이벤트·이미지를 보관 정리에서 보호한다. 생성 후 기본 30일(`DATA_JOB_MAX_AGE_DAYS`)을 넘긴 pending 또는 lease가 만료된 running은 `failed/OBJECT_RETENTION_EXPIRED`로 정리하며 유효한 lease는 만료까지 보호한다. 최종 작업 상태는 추가 보관기간 동안 남는다. 최종 상태·미설정 작업의 자료는 이후 보관 정책에 따라 삭제될 수 있고 수동 삭제·손상도 가능하다. 경로 이탈·없는 파일·손상 이미지는 `failed`로 보고하며 파일을 수정하거나 가짜 성공 결과를 만들지 않는다.
- Preprocessing의 미전송·격리·포화 대기 이미지 보호 목록이 불완전하거나 누락·노후됐을 때 Data는 이벤트·이미지 정리를 보류한다. Analysis는 이 목록이나 outbox DB를 수정하지 않는다. 모델 교체 전후의 크롭·기존 결과 보존기간은 [운영 정책](operations.md)을 함께 확인한다.
- HTTP·모델 호출에 유한한 제한 시간을 둔다. 시간 초과한 모델을 안전하게 취소·격리할 수 없으면 새 claim을 멈추고 준비 상태를 실패로 전환한다. 종료 신호에도 새 claim을 중단한다.

현재 Python 실행기는 HTTP 10초, 빈 큐·통신 실패 후 대기 1초를 사용한다. 플러그인 팩토리와 호출은 별도 `spawn` 프로세스에서 실행하며, 초기화 기본 30초·호출 기본 120초로 제한한다. 각각 `OBJECT_STARTUP_TIMEOUT_SECONDS`(`0 < 값 ≤ 120`)와 `OBJECT_MODEL_TIMEOUT_SECONDS`(`0 < 값 ≤ 240`)로 설정한다. 호출 시간 초과 시 자식을 종료하고 `retry/MODEL_TIMEOUT`을 보고한 뒤 다음 처리를 위해 재생성한다. 초기화 오류는 30초 후 재시도한다. 강제 종료에 실패한 경우에만 작업을 정지시켜 중첩 실행을 막는다. 이 제한은 Data의 **5분 lease와 별개**이므로 완료 전송 여유를 남겨야 한다.

## 4. 새 컨테이너 제작과 교체

### 구현 순서

1. 모델·출력 필드·실패 기준을 정한다. 관측 검증, 이미지 디코딩, 분석, 완료 보고를 분리한다.
2. claim 루프와 health HTTP 서버를 만든다. 빈 큐·Data 장애·늦은 완료·모델 시간 초과를 먼저 처리한다.
3. `server/services/analysis/`에 소스와 **새 Dockerfile**을 둔다. 선택한 언어의 빌드/런타임, HTTP·JSON·이미지·모델 의존성을 이미지에 포함한다. 시작 명령이 실제 새 프로그램을 실행해야 한다.
4. Compose의 `analysis.build`·`image`·healthcheck를 새 구현에 맞춘다. 1절의 네트워크·토큰·마운트·UID 계약을 유지하고 모델 파일을 `/models`에 준비한다.
5. 분리된 개발 배포에서 아래 교체 확인과 실제 작업 인수를 수행한다. 운영과 프로젝트 이름뿐 아니라 **포트·DB·이미지·모델·비밀 파일 경로도 분리**한다.

정적으로 링크한 Linux 실행 파일을 준비했다면 Dockerfile은 다음처럼 구성할 수 있다. **예시의 `analysis` 실행 파일과 `serve`·`healthcheck` 기능은 담당자가 구현해야 한다.** 다른 언어라면 해당 런타임·라이브러리 설치·빌드 단계를 사용한다. 빌드 context는 저장소 루트이므로 `COPY` 경로도 그 기준이다.

```dockerfile
FROM debian:12-slim
WORKDIR /app
COPY --chmod=0555 server/services/analysis/bin/analysis /app/analysis
USER 65532:65532
ENTRYPOINT ["/app/analysis"]
CMD ["serve"]
```

`serve`는 `0.0.0.0:8000`의 상태 서버와 작업 루프를 시작한다. 예시의 healthcheck 하위 명령은 지정 URL을 제한 시간 안에 조회하고 **HTTP 200일 때만 종료 코드 0**, 나머지는 실패를 반환하도록 만든다. Compose의 해당 서비스에 적용할 예시는 다음과 같다. 이미지 태그는 인수할 버전으로 구분한다.

```yaml
analysis:
  build:
    context: ..
    dockerfile: server/services/analysis/Dockerfile
  image: ai-cctv-analysis:handoff-v1
  healthcheck:
    test: ["CMD", "/app/analysis", "healthcheck", "http://127.0.0.1:8000/health/ready"]
    interval: 10s
    timeout: 5s
    retries: 6
    start_period: 20s
```

이 예시는 **기존 analysis 항목 중 바꿀 부분만** 보여 준다. env·volumes·user·depends_on·networks를 지우지 않는다. 완성된 외부 이미지를 쓰면 `build` 항목을 제거하고 해당 `image`를 준비한다. 현재 `.dockerignore`는 `build/`·`dist/`·모델·비밀 파일 등을 제외하므로 산출물 경로와 COPY 포함 여부를 확인한다. GPU는 기존 Analysis에 자동 할당되지 않으므로 필요하면 이 서비스의 장치·런타임도 구성한다.

`compose.dev.yml`의 Analysis는 `development` 빌드 단계와 Python `uvicorn --reload`를 전제로 한다. 다른 언어로 교체한 뒤 그대로 합치지 말고 해당 항목도 수정하거나 아래 운영 Compose만으로 시험한다.

### 교체하고 실제 실행 이미지를 확인하기

환경 준비를 마친 **개발용 `server/.env`**를 사용한다. 다음 명령은 저장소 루트 PowerShell 기준이며 실패하면 다음 단계로 넘어가지 않는다. `nginx` 기동은 필요한 Data·External·MediaMTX를 함께 준비한다. 실제 영상 입력을 쓸 때는 별도로 Preprocessing과 카메라도 기동한다.

```powershell
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --wait nginx
docker compose --env-file server/.env -f server/compose.yml stop analysis
docker compose --env-file server/.env -f server/compose.yml build analysis
docker compose --env-file server/.env -f server/compose.yml up -d --no-deps --force-recreate --wait analysis
$analysisContainer = docker compose --env-file server/.env -f server/compose.yml ps -q analysis
docker inspect --format '{{.Image}}' $analysisContainer
docker image inspect --format '{{.Id}}' ai-cctv-analysis:handoff-v1
docker inspect --format '{{json .Mounts}}' $analysisContainer
docker compose --env-file server/.env -f server/compose.yml logs --tail 100 analysis
```

컨테이너 ID가 비어 있지 않고 **두 이미지 ID가 같은지** 확인한다. Mounts의 `/snapshots`·`/models`가 의도한 호스트 폴더이며 `RW=false`인지 확인한다. 이미지 태그 이름이나 `healthy` 표시만으로 교체 성공을 판정하지 않는다. 외부 이미지는 `build` 대신 pull/load하고 해당 태그로 비교한다. `restart`는 새 이미지·환경을 적용하는 명령이 아니다.

새 컨테이너의 상태 서버도 직접 확인한다. 아래 Python은 **Data 컨테이너의 도구**이며 새 Analysis 이미지에 Python을 요구하지 않는다.

```powershell
docker compose --env-file server/.env -f server/compose.yml exec -T data python -c "import urllib.request; print(urllib.request.urlopen('http://analysis:8000/health/ready', timeout=3).read().decode())"
```

기존 Python 실행기를 재사용해도 된다. 이 경우 인자 없는 팩토리의 `process(job, crop_path)`가 lease를 제외한 완료 요청 dict를 반환하게 하고 `ANALYSIS_PLUGIN=모듈:팩토리`로 선택한다. 라이브러리는 `requirements.txt`·Dockerfile에 추가한다. 공통 실행기는 경로·존재를 검사하며 기본 분석기는 [공용 JPEG 로더](../lib/ai_cctv_core/processing/images.py)로 디코딩·크기 제한을 확인한다. 교체 모델도 같은 로더 또는 동등한 검증을 사용한다. 전체 컨테이너 교체에는 이 Python 함수 형식이 필요 없다.

## 5. 인수 검증

### 실제 교체 컨테이너에 작업 넣기

테스트 담당자는 개발 `SNAPSHOTS_DIR/analysis-fixture/crop.jpg`에 **20×40 픽셀의 유효한 사람 JPEG**를 준비한다. 새 Analysis를 실행한 상태에서 아래를 실행한다. **수동 claim/complete를 호출하지 않아야 실제 새 컨테이너가 처리한다.** 다른 대기 작업이 없는 개발 DB를 사용한다.

다음 보조 코드는 Data 컨테이너의 개발용 토큰으로 카메라·이벤트를 등록하고 결과를 읽는다. Analysis에 이 토큰이나 관리 권한을 주는 절차가 아니다. 동일 시험 카메라가 있으면 재사용하며 파일을 만들거나 DB를 직접 열지 않는다.

```powershell
@'
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

base = "http://nginx:8080/internal/data/v1"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(method, path, token_name, body=None):
    headers = {"X-Internal-Token": os.environ[token_name]}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=payload, headers=headers, method=method)
    with opener.open(request, timeout=10) as response:
        return json.load(response)

try:
    call("GET", "/cameras/cam-analysis-test", "DATA_EXTERNAL_TOKEN")
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
    call("POST", "/cameras", "DATA_EXTERNAL_TOKEN", {
        "camera_id": "cam-analysis-test", "name": "Analysis test",
        "stream_path": "cam-analysis-test", "enabled": False
    })

event = call("POST", "/events", "DATA_INFERENCE_TOKEN", {
    "camera_id": "cam-analysis-test", "event_type": "person_appeared",
    "occurred_at": datetime.now(timezone.utc).isoformat(), "person_id": "1",
    "object_observation": {
        "tracking_session_id": uuid4().hex,
        "bbox": [0, 0, 20, 40], "frame_width": 100, "frame_height": 100,
        "crop_path": "analysis-fixture/crop.jpg"
    }
})
timeout_seconds = 330
deadline = time.monotonic() + timeout_seconds
while True:
    saved = call("GET", f"/events/{event['id']}", "DATA_EXTERNAL_TOKEN")
    result = saved["metadata"]["analysis"]
    if result["status"] in {"complete", "unconfigured", "failed"}:
        break
    if time.monotonic() >= deadline:
        raise RuntimeError(f"Analysis result was not completed within {timeout_seconds} seconds")
    time.sleep(1)
assert result["status"] == "complete", result
assert saved["metadata"]["object"] == event["metadata"]["object"]
print(json.dumps(result, ensure_ascii=False, indent=2))
'@ | docker compose --env-file server/.env -f server/compose.yml exec -T data python -
```

출력의 `metadata.analysis.result`에 해당하는 `result` 객체를 모델 기대값과 대조한다. 예시의 `timeout_seconds=330`은 5분 임대와 짧은 작업 대기 여유를 포함하며, 개발 환경의 대기·재시도 시간을 고려해 필요하면 조정한다. `unconfigured`는 모델 미연결, `pending` 지속은 큐·통신·처리 지연을 먼저 확인한다. 이 시험은 유효 입력 한 건의 연결 확인이며 정확도·오류·재시도 인수를 대신하지 않는다. 이 예제에서 비활성 카메라에 직접 등록한 관측 이벤트도 작업이 생성된다.

### 기존 자동 검사와 새 컨테이너 인수의 구분

```powershell
docker compose -f server/compose.test.yml build tests
docker compose -f server/compose.test.yml run --rm tests python -m pytest -c tests/runner/pytest.ini --rootdir=. tests/automated/test_object_processing.py server/services/analysis/tests -q
```

이 명령은 **저장소 Python 실행기·Data 계약의 회귀 검사**다. `compose.test.yml`의 `tests`는 별도 테스트 이미지이며 교체한 `analysis` 이미지·모델·마운트를 실행하지 않는다. 같은 파일의 `integration` 프로필도 Data·External HTTP 연동을 검사하며 새 Analysis 컨테이너를 띄우지 않는다. Python 실행기를 없앴다면 그 실행기에 종속된 테스트를 그대로 새 구현 검증이라고 제출하지 않는다.

이번 기본 구현은 로컬 개발 Python에서 합성 JPEG를 실제 디코딩해 색 분리·배경 억제·치수·파일 상한·손상 거부를 검사했다. Docker나 실제 CCTV 입력을 사용한 기동·성능·의복 색 정답률은 검증하지 않았다. 위 실제 컨테이너 절차를 실행하고 원본 크롭과 영역별 RGB·색명·비율을 함께 보관해야 운영 인수 근거가 된다.

새 컨테이너는 실제 인수 이미지와 공유 파일로 다음을 확인하고 테스트 방법·결과를 남긴다.

| 인수 항목 | 남길 근거 |
|---|---|
| 교체·기동 | 컨테이너 ID, 빌드/인수 이미지 ID·버전, 모델 버전·해시, 올바른 UID·읽기 전용 마운트, health 응답 |
| 정상 분석 | 위 실제 입력의 이벤트 ID, `complete` 결과, 모델별 기대 속성·값·단위와 실제 출력 |
| 단계 독립 | 전역 ID가 없는 입력도 처리하고, identity가 먼저/나중에 끝나도 관측·identity·전역 ID를 덮어쓰지 않음 |
| 잘못된 입력·결과 | 경로 이탈·없는/손상 크롭·잘못된 bbox, 초과 metadata·NaN·전역 ID 지정이 차단되고 가짜 성공을 남기지 않음 |
| 임대·장애 | 빈 큐, Data 장애, 응답 유실·중복 완료, 5분 만료·재할당 후 늦은 결과 거절, 5회 한도 확인 |
| 상태·보존 | claim→running, 재등록→pending, 시도 소진→failed/ATTEMPTS_EXHAUSTED, 장기 미처리→OBJECT_RETENTION_EXPIRED, 실행 중 유효 lease·crop 보존 |
| 모델·복구 | 미설정→모델 연결→unconfigured 재등록, 모델 오류·시간 초과, 종료·재시작 뒤 작업 재개 |

HTTP 계약과 모델 품질은 따로 인수한다. 모델 품질은 대표 크롭·기대 결과·측정 지표·처리시간을 담당자와 검토자가 합의하고 결과를 기록한다. 기존 테스트 통과나 health 200만으로 새 모델 인수를 끝내지 않는다.

## 6. 인수 후 이 문서를 삭제하기 전

새 `server/services/analysis/README.md`에 실제 빌드·기동 명령, 선택한 언어/모델, env·경로·UID·GPU 조건, 입력·출력 스키마와 예제, 오류·재시도·상태 의미, 인수 이미지/모델 버전과 검증 결과를 남긴다. 운영자가 미설정 작업 재처리와 시간 초과 복구를 수행할 방법도 포함한다.

- [ ] 새 컨테이너 제작·교체·인수를 완료하고 검토자가 결과를 확인했다.
- [ ] 실제 교체 이미지를 사용하는 반복 가능한 계약 테스트를 남겼다. 기본 Python 테스트와의 구분도 기록했다.
- [ ] 필수 API·임대·경로·결과 계약이 README·테스트에 보존되어 이 문서 없이 유지보수할 수 있다.
- [ ] 문서 색인·컴포넌트 README·설치 패키지의 SRS 참조를 새 문서로 바꾼 뒤 이 임시 문서를 삭제한다.

코드 확인이 필요할 때의 기준은 [관측·완료 스키마](../lib/ai_cctv_core/contracts/objects.py), [작업 API](../server/services/data/app/api/objects.py), [임대·결과 저장](../server/services/data/app/database/repositories/objects.py), [현재 Python 실행기](../lib/ai_cctv_core/processing/worker.py), [현재 상태 서버](../server/services/analysis/app/main.py)다. 다른 SRS나 구조 문서를 먼저 읽을 필요는 없다.
