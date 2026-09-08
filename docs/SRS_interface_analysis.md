# Analysis 컨테이너 교체 규약

Analysis는 사람 이미지와 관측 정보를 받아 의상·행동 등 추가 분석 결과를 `metadata.analysis`에 기록하는 컨테이너다. 이 문서는 다른 언어·모델로 구현해도 나머지 서비스를 수정하지 않고 연결하기 위한 규약이다. 전체 흐름은 [구조 문서](architecture.md), 감지·전역 인물 연결은 [Preprocessing 규약](SRS_interface_preprocessing.md)을 따른다.

현재 기본 분석기는 입출력만 갖춘 블랙박스로 `unconfigured`를 반환한다. 의상·행동 분석 모델은 포함되어 있지 않다.

## 1. 역할과 배포 경계

- Data의 `analysis` 작업만 가져와 분석하고 결과를 돌려준다. SQLite를 직접 열거나 이벤트·푸시·실시간 좌표를 생성하지 않는다.
- `global_person_id`의 생성·변경은 Preprocessing의 identity 작업이 담당한다. Analysis는 이를 결과에 지정할 수 없다.
- 두 단계는 독립적으로 실행된다. 입력의 `global_person_id`가 `null`이어도 분석해야 하며, identity 완료를 기다리거나 완료 순서를 가정하지 않는다.
- 현재 관측은 사람 등장마다 한 번 생성한다. 연속 프레임·영상·다른 이벤트의 metadata·감지 confidence가 분석 입력에 포함된다고 가정하지 않는다.

| 항목 | 유지할 계약 |
|---|---|
| 서비스·네트워크 | Compose 서비스 이름 `analysis`, 기존 `internal` 네트워크 |
| Data 주소 | `DATA_SERVICE_URL=http://nginx:8080/internal/data/v1` |
| 인증 | `analysis.env`의 `DATA_ANALYSIS_TOKEN`을 `X-Internal-Token` 헤더로 전송. 기존 실행기는 32자 이상을 요구 |
| 이미지 입력 | `SNAPSHOTS_ROOT=/snapshots`, 기존 호스트 `SNAPSHOTS_DIR`를 읽기 전용으로 마운트 |
| 모델 | 기존 호스트 `MODELS_DIR`를 `/models`에 읽기 전용으로 마운트. 모델 파일명은 분석 구현이 결정 |
| 시간 | UTC 사용. Data가 발행한 `occurred_at`은 시간대가 있는 ISO 8601 문자열 |
| 상태 확인 | 컨테이너 내부 `0.0.0.0:8000`에서 아래 HTTP 경로 제공. 호스트 공개 포트 추가 없음 |
| 권한·설정 | 비관리자 사용자로 실행하고 입력 파일 수정 금지. DB·녹화·`config.yaml` 마운트와 다른 서비스의 토큰은 필요 없음 |

컨테이너 교체 시 이미지·빌드 설정과 필요한 의존성을 변경할 수 있다. 현재 Compose의 상태 확인 명령은 Python으로 HTTP를 호출하므로 Python이 없는 이미지는 같은 경로·성공 조건을 검사하는 명령으로 바꾼다. 컨테이너 내부 코드를 Python 클래스 구조에 맞출 필요는 없다. 내부 Data 연결은 HTTP이므로 인증 헤더가 외부 프록시나 로그로 나가지 않게 한다.

## 2. 작업 가져오기와 입력

처리기가 `claim`을 주기적으로 호출해 작업을 가져가는 방식이며 Data가 먼저 연결하지 않는다. 아래 세 API는 모두 `DATA_SERVICE_URL` 뒤에 경로를 붙인 **POST**다. `claim`과 `requeue-unconfigured`에는 요청 본문이 없고, `complete`에는 JSON 본문과 `Content-Type: application/json`을 보낸다. 모두 analysis 토큰이 필요하며 다른 단계·일반 Data API에는 이 토큰을 사용할 수 없다.

```http
POST /internal/data/v1/object-jobs/analysis/claim HTTP/1.1
Host: nginx:8080
X-Internal-Token: <DATA_ANALYSIS_TOKEN>
```

응답은 HTTP 200이다. 대기 작업이 없으면 `{"job": null}`, 있으면 다음 형태다.

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

| `job` 필드 | 형식·의미 |
|---|---|
| `id`, `event_id` | 정수. 작업 ID와 원본 이벤트 ID. 완료 URL에는 `id` 사용 |
| `attempts` | 이번 claim **이전**의 시도 횟수. 첫 응답은 0이며 Data는 claim 시 내부 횟수를 1 증가 |
| `camera_id` | 카메라 식별 문자열 |
| `person_id` | 해당 카메라·추적 세션 내 인물 ID 문자열, 1~256자. 정상 관측 작업에는 필수 |
| `global_person_id` | 문자열 또는 `null`. claim 시점의 값이며 분석 도중 다른 단계에서 갱신될 수 있음 |
| `occurred_at` | 원본 이벤트 발생 UTC 시각. 작업 수신 시각이 아님 |
| `object_observation` | 아래 관측 객체. 이벤트 전체 metadata가 아님 |
| `lease_id` | 소문자 16진수 32자리. 이번 작업 처리 권한을 구별하는 값이며 완료 요청에 그대로 전송 |

`(camera_id, object_observation.tracking_session_id, person_id)`가 로컬 추적의 식별 키다. `person_id`만으로 다른 카메라·재접속 구간의 사람을 합치지 않는다. 현재 응답에 `stage`, `lease_until`, 이벤트 전체 본문은 없다.

| 관측 필드 | 검증 규칙 |
|---|---|
| `schema_version` | 정수 `1` |
| `tracking_session_id` | 소문자 16진수 32자리. 재접속·추적 초기화 때 변경 |
| `object_class` | 문자열 `person` |
| `bbox` | 정수 4개 `[x1,y1,x2,y2]`. 원본 프레임의 픽셀 좌표이며 `0 ≤ x1 < x2 ≤ frame_width`, `0 ≤ y1 < y2 ≤ frame_height` |
| `frame_width`, `frame_height` | 각각 1~16384의 정수. 크롭 이미지 크기가 아니라 원본 영상 크기 |
| `crop_path` | 1~4096자의 스냅샷 저장소 상대 경로. 현재 생산자는 사람 영역의 JPEG를 저장 |
| `annotated_snapshot_path` | 최대 4096자의 상대 경로 또는 `null`. 박스가 그려진 전체 이미지이며 분석 필수 입력이 아님 |

관측 스키마에 없는 추가 필드는 허용하지 않는다. Data는 생략된 `schema_version=1`, `object_class="person"`, `annotated_snapshot_path=null` 기본값을 채워 저장한다. 새로운 스키마 버전은 조용히 기존 버전으로 처리하지 말고 호환 여부를 확인한다.

크롭을 열기 전에 `SNAPSHOTS_ROOT`와 상대 경로를 결합하고, `..`·심볼릭 링크까지 해석한 실제 경로가 저장소 내부의 일반 파일인지 확인한다. URL·외부 절대 경로를 따라가거나 파일을 수정하지 않는다. 파일은 작업 생성 후 삭제될 수 있다. 경로·파일·이미지 형식 오류는 가짜 분석 결과 대신 명확한 실패로 보고한다.

## 3. 결과 보내기

`POST /object-jobs/analysis/{id}/complete`에 다음 JSON을 보낸다. 아래 속성 이름·값은 **모델 출력 예시**이며 현재 기본 분석기가 생성하는 값은 아니다.

```json
{
  "lease_id": "abcdef0123456789abcdef0123456789",
  "outcome": "complete",
  "metadata": {
    "backend": "example-model",
    "version": "1",
    "attributes": {"coat_color": "blue"}
  }
}
```

| 필드 | 규칙 |
|---|---|
| `lease_id` | 필수. claim에서 받은 값 |
| `outcome` | 필수. `complete`, `unconfigured`, `retry`, `failed` 중 하나 |
| `metadata` | JSON 객체, 생략 시 `{}`. 중첩 객체·배열·문자열·유한수·불리언·`null` 허용. NaN·Infinity 금지 |
| `global_person_id` | Analysis는 생략하거나 `null`만 전송. ID를 지정하면 거부 |

최상위에 이외의 필드를 추가하지 않는다. `metadata` 크기는 Data의 Python `json.dumps(metadata, allow_nan=False)` 결과를 UTF-8로 바꾼 기준 **65,536바이트 이하**여야 한다. 기본 직렬화는 한글을 `\uXXXX`로 바꾸고 구분자 공백을 포함하므로, 클라이언트의 압축 JSON 전송 크기만으로 한도를 판단하면 안 된다. 모델별 결과는 `metadata` 안에서 이름으로 구분하고 모델·버전 정보를 남기는 것을 권한다. 토큰·비밀값·예외 원문은 결과에 넣지 않는다.

| `outcome` | Data에 저장되는 상태와 다음 동작 |
|---|---|
| `complete` | 분석 성공. `complete`로 종료 |
| `unconfigured` | 모델 미연결. `unconfigured`로 종료하며 자동 재시도 없음 |
| `retry` | 일시적 장애. 횟수가 남으면 `pending`, 최대 횟수에 도달하면 `failed` |
| `failed` | 입력 오류 등 재실행해도 해결되지 않는 실패. `failed`로 종료 |

수락하면 HTTP 200 `{"accepted": true}`다. Data는 최신 이벤트 metadata의 `analysis` 부분만 다음 형태로 교체하며, identity 결과·원본 관측을 보존한다.

```json
{
  "status": "complete",
  "updated_at": "2026-09-07T01:00:01.000Z",
  "result": {"backend": "example-model", "version": "1", "attributes": {"coat_color": "blue"}}
}
```

이 값은 이벤트의 `metadata.analysis`에 들어간다. 추가 분석 완료가 새 이벤트나 추가 푸시를 만들지는 않는다. 클라이언트는 이벤트를 다시 조회해 결과를 확인한다.

## 4. 임대·중복·실패 처리

**작업은 중복 실행될 수 있다.** Data의 claim은 작업을 5분간 임대하고 각 claim마다 새 `lease_id`를 발행한다. 만료 연장 API는 없다. 모델 수행과 결과 전송을 이 시간 안에 마쳐야 한다. 정상 처리 가능한 작업만 가져오고, 장시간 선점한 채 로컬 대기열에 쌓지 않는다.

- 완료는 해당 단계의 `running` 작업이면서 lease가 일치하고 아직 만료되지 않았을 때만 수락한다.
- 만료·이전 lease·이미 완료한 작업·없는 작업·다른 단계 작업은 **HTTP 200 `{"accepted": false}`**다. 성공으로 처리하거나 결과가 저장되었다고 가정하지 않는다.
- 완료 응답이 유실되면 같은 lease·본문으로 재전송할 수 있지만, 첫 요청이 반영됐다면 재전송은 `false`다. 이 응답만으로 이전 수락 여부까지 구별할 수 없으며 별도 작업 상태 조회 API는 없다.
- 중단된 작업은 5분 임대 만료 후 다음 claim에서 회수된다. 외부 시스템에 부수적인 쓰기를 한다면 작업 `id`로 중복을 방지한다.
- claim은 작업당 총 5회까지다. `retry` 후 대기 시간은 1~4번째 시도에 각각 30·60·120·240초이며 5번째는 실패로 종료한다.
- 5번째 작업도 프로세스 중단으로 만료되면 다음 claim이 작업을 `failed`로 정리한다. 이 만료 정리에서는 이벤트의 `metadata.analysis`가 함께 갱신되지 않으므로 metadata 상태만으로 작업 대기열 상태를 완전히 추정할 수 없다.

현재 실행기는 모델 호출을 120초로 제한한다. 시간 초과는 `retry`와 `{"error_code":"MODEL_TIMEOUT"}`으로 보고하고 추가 claim을 중단해 준비 상태를 503으로 바꾼다. 실행 중인 모델 스레드를 강제 종료하지 않으므로 운영자가 컨테이너를 재시작해야 한다. 교체 구현은 같은 장애 의미를 유지하되 작업 취소·격리는 해당 언어에 맞게 구현할 수 있다. 현재 실행기의 입력·결과 오류는 `failed`/`INVALID_OBJECT_RESULT_OR_CROP`, 그 밖의 모델 오류는 `retry`/`ANALYZER_UNAVAILABLE`이다.

모델 연결 후 `POST /object-jobs/analysis/requeue-unconfigured`를 호출하면 HTTP 200 `{"requeued": 100}`처럼 재등록 건수를 반환한다. 호출당 최대 100건을 `pending`으로 옮기고 횟수를 0으로 초기화한다. 필요한 만큼 반복하며 기존 크롭이 남아 있어야 한다. 이벤트 metadata는 다음 완료 보고 전까지 `unconfigured`로 남을 수 있다. `failed` 작업의 재등록 API는 현재 없다.

| HTTP 응답 | 의미와 처리 |
|---|---|
| 401 `INVALID_INTERNAL_TOKEN` | 토큰 누락·불일치. 설정 수정 |
| 403 `INTERNAL_SCOPE_FORBIDDEN` | 다른 서비스의 토큰 또는 허용되지 않은 API. 요청·권한 수정 |
| 409 `OBJECT_RESULT_CONFLICT` | 유효 lease의 분석 결과에 전역 ID를 지정한 경우. 결과 계약 수정 |
| 422 `VALIDATION_ERROR` | 필드·JSON 값·크기 규칙 위반. 본문 수정 |
| 503 `INTERNAL_TOKEN_NOT_CONFIGURED` | Data의 해당 단계 토큰 미설정. 배포 설정 수정 |
| 연결 실패·기타 5xx | Data·Nginx 장애 등. 요청 간격을 두고 재시도하며 lease 만료와 중복 처리에 유의 |

Data 오류 JSON은 `{"error":{"code":"...","message":"...","details":{}}}` 형태다. 입력 검증의 `details`는 오류 목록 배열일 수 있다. Nginx가 직접 반환한 오류는 같은 JSON 형식을 보장하지 않는다.

## 5. 상태 확인과 교체 인수 검사

| 경로 | 상태·응답 |
|---|---|
| `GET /health/live` | 프로세스 응답 가능 시 200 `{"status":"alive","service":"analysis"}` |
| `GET /health/ready` | claim 통신이 준비되고 작업자가 멈추지 않았으면 200. `status`, `ready`, `stalled`, `last_error`, `last_outcome` 제공 |
| 준비 실패 | 작업자 미생성·Data 통신 실패·모델 시간 초과로 정지한 상태는 503 |

정상 준비 응답 예시는 `{"status":"ready","ready":true,"stalled":false,"last_error":null,"last_outcome":"complete"}`다. 마지막 작업 오류가 남아 있지만 작업 수신이 가능한 상태는 200과 `status="degraded"`다. 기본 블랙박스의 `unconfigured`는 모델이 없다는 뜻으로, 통신 준비 실패와 구분한다. 초기에는 `last_outcome=null`일 수 있다. 준비 상태 200만으로 분석 모델의 존재·정확도가 검증되지는 않는다.

완료 응답이 `accepted:true`가 아니면 현재 실행기는 `last_outcome="rejected"`, `last_error="COMPLETION_NOT_ACCEPTED"`로 표시한다. 이 거절만으로 수신을 중단하지 않으며 이후 작업이 정상 완료되면 오류를 해제한다. `rejected`는 진단값이며 완료 요청의 `outcome`으로 보내는 값이 아니다.

교체 전 다음 항목을 확인한다.

1. 기존 Compose 네트워크·토큰·읽기 전용 마운트로 시작하고 상태 확인이 동작한다.
2. 전역 ID가 없는 유효 관측을 처리하여 `metadata.analysis`만 갱신하고 identity 결과는 보존한다.
3. 경로 이탈·없는 크롭·잘못된 좌표·초과 metadata·NaN·전역 ID 지정이 차단된다.
4. 빈 대기열, 완료 중복, lease 만료·재할당, Data 장애와 5회 한도에서 잘못된 결과가 반영되지 않는다.
5. 모델 미설정→연결→재등록, 모델 오류·시간 초과와 컨테이너 재시작을 확인한다.
6. 실제 크롭·모델로 결과를 검증한다. 계약 테스트 통과와 모델 정확도 검증을 구분한다.

현재 Python 연결 방식은 [기본 분석기](../server/services/analysis/processors/__init__.py)의 `process(job, crop_path)`를 교체하고 `ANALYSIS_PLUGIN`을 지정하는 참고 구현이다. 전체 컨테이너 교체 시 이 환경변수·함수 형식은 필수가 아니다. 계약의 근거는 [객체 스키마](../lib/ai_cctv_core/contracts/objects.py), [Data API](../server/services/data/app/api/objects.py), [작업 저장소](../server/services/data/app/database/repositories/objects.py), [작업 실행기](../lib/ai_cctv_core/processing/worker.py), [검증 테스트](../tests/automated/test_object_processing.py), [Compose](../server/compose.yml)다. 설치·운영은 [프로젝트 README](../README.md)를 따른다.
