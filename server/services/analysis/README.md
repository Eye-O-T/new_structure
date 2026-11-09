# Analysis 서비스

Data에서 작업과 사람 영역 이미지(크롭)의 경로를 받아 추가 정보(`metadata`)를 돌려주는 독립 컨테이너다. Preprocessing과 직접 통신하지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 작업기 시작·종료, Data 연결, 상태 확인 |
| `processors/attributes.py` | 기본 PAR 속성 분석기 (`PersonAttributeAnalyzer`, `/models/par_serving_exp3.pt`) |
| `processors/appearance.py` | 호환성 별칭 모듈 (`PersonAttributeAnalyzer`로 위임, OpenCV 색상 분석기 제거됨) |
| `processors/__init__.py` | 기본 분석기와 호환 별칭 export |
| [`lib/ai_cctv_core/processing/`](../../../lib/ai_cctv_core/processing/) | 인물 연결과 분석이 공유하는 작업 임대·입력 검증·시간 제한·결과 보고 |

기본 분석기는 학습된 PAR 번들(`par_serving_exp3.pt`)을 사용하는 `PersonAttributeAnalyzer`(`ANALYSIS_PLUGIN=server.services.analysis.processors.attributes:PersonAttributeAnalyzer`)다. 기존 OpenCV 기반 색상·품질 분석기(`LocalAppearanceAnalyzer`)는 제거되었으며, 이전 설정이 `LocalAppearanceAnalyzer`를 가리키더라도 `PersonAttributeAnalyzer`로 동작한다. Analysis 서비스는 PAR 분석기 외의 플러그인 선택을 거부한다.

```dotenv
ANALYSIS_PLUGIN=server.services.analysis.processors.attributes:PersonAttributeAnalyzer
```

기본 컨테이너 경로는 `/models/par_serving_exp3.pt`다. 호스트의 `MODELS_DIR`에 `par_serving_exp3.pt`를 두어야 한다. analysis 시작은 모델 번들·가중치 로드가 성공한 뒤에만 완료되며, 파일 누락·번들 오류·초기화 시간 초과는 제한된 오류 코드로 기록한 뒤 프로세스를 종료한다. analysis Compose 재시작은 최대 3회이며, 그 뒤에는 모델 복구 후 담당자가 다시 시작해야 한다. 실패한 시작은 작업을 수령하거나 완료·분석 완료 이벤트를 만들지 않는다. 가동 중 격리 모델의 재로딩이 실패하면 현재 작업을 성공 처리하지 않고 재시도 상태로 반환한 뒤 analysis 프로세스를 종료한다. PAR 분석기는 CPU에서 ResNet34 번들을 제한 로더로 읽고 `gender`, `age`, `hat`, `upper_color`, `lower_color`, `sleeve`, `lower_type`, `carrying`을 `metadata.attributes`에 반환한다. 속성 점수는 모델 출력의 상대 점수이며 실제 신원·나이 또는 정답 확률을 뜻하지 않는다. 이 모델에는 위험도·흉기·행동 분류가 없다.

이 분석기는 `global_person_id`를 지정할 수 없다. 인물 연결 작업과 완료 순서는 독립적이다. 스냅샷·모델은 읽기 전용이며 DB는 직접 열지 않는다.

## 현행 입출력과 구현 기준

플러그인은 `process(job: dict, crop_path: Path) -> dict`를 제공한다. `job`은 Data에서 임대한 객체 작업이며 `crop_path`는 검증된 로컬 크롭 경로다. 기본 분석기는 [공용 JPEG 로더](../../../lib/ai_cctv_core/processing/images.py)로 8 MiB 이하·1,600만 픽셀 이하, 관측 bbox와 정확히 일치하는 크기의 JPEG만 읽는다. 각 변이 16픽셀 미만인 크롭은 거부한다. 반환값은 `lease_id`를 제외한 `ObjectJobCompletion` 필드이고 공통 실행기가 임대 ID를 붙여 완료 결과를 보고한다. 교체 플러그인도 동일한 입력 검증을 유지해야 한다.

`outcome`은 `complete`·`retry`·`failed`·`unconfigured` 중 하나다. 추가 정보는 최대 64 KiB의 유한 JSON 객체 `metadata`에 담으며 Analysis 결과의 `global_person_id`와 `identity_descriptor`는 지정하지 않는다. 현재 호출 형식은 [플러그인 프로토콜](../../../lib/ai_cctv_core/processing/plugins.py), 필드 검증은 [객체 스키마](../../../lib/ai_cctv_core/contracts/objects.py), 오류·시간 제한 처리는 [공통 실행기](../../../lib/ai_cctv_core/processing/worker.py)와 [작업기 수명 관리](../../../lib/ai_cctv_core/processing/runtime.py)를 따른다.

### 기본 분석 결과의 의미

`metadata.backend`는 `par_resnet34`, `backend_version`은 번들의 `model_version`, `schema_version`은 `1`이다. `image`에는 원본 크롭의 `width`·`height`를 담고, `attributes`에는 단일 선택 속성(`gender`, `age`, `hat`, `sleeve`, `lower_type`)의 `{"value", "score"}`와 다중 선택 속성(`upper_color`, `lower_color`, `carrying`)의 상위 최대 3개 태그 목록 `[{"value", "score"}, ...]`을 담는다.

현재 결과·권한·작업 임대 동작은 [객체 스키마](../../../lib/ai_cctv_core/contracts/objects.py)와 [객체 작업 API](../data/app/api/objects.py)를 기준으로 유지한다. 분석 결과가 `outcome="complete"`로 수락되면 Data는 원본 등장 이벤트의 `metadata.analysis` 갱신과 함께 별도의 `analysis_completed` 이벤트를 원자적으로 생성한다(`failed`·`retry`·`unconfigured`는 생성하지 않음). 관제 화면에서는 타임라인의 `analysis_completed` 카드에서 성별·연령 요약을 표시하고 상세 창에서 전체 분석 결과를 조회한다.

`unconfigured`로 끝난 기존 작업은 모델 연결·컨테이너 재시작만으로 다시 처리하지 않는다. 모델을 연결하고 크롭 파일이 남아 있는지 확인한 뒤 내부 네트워크에서 `POST http://nginx:8080/internal/data/v1/object-jobs/analysis/requeue-unconfigured`를 호출한다. 본문은 없고 `X-Internal-Token`에는 `DATA_ANALYSIS_TOKEN`을 사용한다. 응답 `{"requeued": 수량}`만큼 최대 100건씩 다시 대기하므로 남은 작업이 있으면 반복한다. 완료·재시도·재등록의 저장 규칙은 [Data 작업 저장소](../data/app/database/repositories/objects.py)를 따른다.

## 실행과 검증

중앙 Compose로 실행한다. 소스 배포에서는 `server/.env`, 설치 배포에서는 설치 저장소의 `config/compose.env`에 `ANALYSIS_PLUGIN`을 둔다. Data 인증키는 해당 배포의 `ANALYSIS_SECRETS_FILE`이 가리키는 파일에 두며, 개발 기본 위치는 `server/secrets/analysis.env`다. 이 토큰은 32자 이상이며 Data에 설정한 같은 이름의 토큰과 일치해야 한다. 모델 의존성은 이 서비스의 `requirements.txt`에 추가하고 이미지를 다시 빌드한다. 모델은 `MODELS_DIR`에서 컨테이너의 `/models`에 읽기 전용으로 연결되며, 사용할 파일은 플러그인이 선택한다.

중앙 Compose로 실행하며 배포 파일·인증·모델 준비는 [배포 안내](../../../docs/deployment-guide.md)를 따른다.

플러그인 초기화·호출은 별도 `spawn` 자식 프로세스에서 실행한다. `OBJECT_MODEL_TIMEOUT_SECONDS`는 기본 120초(`0 < 값 ≤ 240`), `OBJECT_STARTUP_TIMEOUT_SECONDS`는 기본 30초(`0 < 값 ≤ 120`)다. 시간 초과한 자식은 종료한 뒤 재생성한다. analysis 모델 재로딩 실패는 작업을 재시도 처리한 뒤 서비스를 종료하며, 시작 재시도는 Compose 정책 `on-failure:3`으로 제한한다. 완료 HTTP 응답이 유실되면 보관한 동일 lease·본문을 재전송해 같은 프로세스 안에서 모델을 다시 실행하지 않는다. 프로세스 전체가 중단되면 Data의 임대 만료 후 재처리될 수 있다.

`/health/live`는 프로세스 생존 여부, `/health/ready`는 작업 처리 통로의 상태다. 시작 실패 때는 서버가 준비 상태를 열지 않고 프로세스가 종료하므로 Docker 상태와 서비스 로그에서 제한된 `ANALYSIS_MODEL_MISSING`, `ANALYSIS_MODEL_INVALID`, `ANALYSIS_MODEL_STARTUP_TIMEOUT`, `ANALYSIS_STARTUP_FAILED` 코드를 확인한다. 정상 실행 중에는 `backend`·`model_ready`·`last_error`·`last_outcome`을 실제 결과와 함께 확인한다. 서비스 상태 화면은 허용된 오류 코드만 한국어 상태로 표시한다. 세부 응답은 [상태 확인](../../../docs/operations.md#상태-확인)을 따른다.

이미지 빌드 과정에서 런타임 사용자로 PAR 분석기와 설정된 플러그인 별칭을 import 검증한다. 이 단계는 모델 파일이나 Data 접속 없이 import 오류를 탐지한다. `test_attributes.py`도 분석 의존성 또는 제품 모듈 import 실패를 건너뛰지 않고 실패로 보고한다.
