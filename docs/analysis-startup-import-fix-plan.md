# analysis 시작 실패 수정계획서

- 작성일: 2026-11-09, KST
- 대상: `ai-cctv-analysis-1`의 `ANALYSIS_STARTUP_FAILED` 및 `unhealthy`
- 상태: 계획 수립. 이 문서 작성 단계에서는 제품 코드·운영 설정·컨테이너를 변경하지 않는다.

## 1. 확인된 원인

`server/services/analysis/processors/__init__.py`가 공용 라이브러리에 없는 `MetadataBlackBox`를 import한다. `PersonAttributeAnalyzer`의 하위 모듈을 직접 지정해도 Python이 패키지의 `__init__.py`를 먼저 실행하므로 같은 오류가 발생한다.

```text
ImportError: cannot import name 'MetadataBlackBox'
from 'ai_cctv_core.processing.plugins'
```

앞선 로그 조사에서 동일 빌드 이미지와 읽기 전용 모델 마운트로 분석기 초기화만 실행해 이 오류를 재현했다. 네트워크를 차단했으므로 운영 작업을 수령하거나 완료 처리하지 않았다.

| 확인 항목 | 결과 |
| --- | --- |
| 분석기 설정 | `server.services.analysis.processors.attributes:PersonAttributeAnalyzer` |
| 공용 패키지 | `ai-cctv-core==0.3.0`, `load_factory` 존재, `MetadataBlackBox` 없음 |
| 모델 파일 | `/models/par_serving_exp3.pt`, 85,373,069바이트로 존재 |
| 실패 위치 | 모델 로드 전, 분석 패키지 import 단계 |
| 프로세스 | 시작 후 약 1초 만에 종료, 종료 코드 3, OOM 종료 아님 |
| 재시작 정책 | `on-failure:3`, 자동 재시작 3회 후 중지 |
| 최종 표시 | 앱 시작 실패로 컨테이너가 `unhealthy`가 되어 Compose 대기가 실패 |

모델 파일의 존재는 확인했지만 이번 실패 상태에서는 모델 로드와 추론에 도달하지 못했다. import를 고친 뒤 실제 모델 준비 여부를 따로 검증한다.

## 2. 수정 목표와 범위

정상 PAR 모델을 사용한 분석기가 import·격리 초기화·추론을 통과하고, analysis 서비스가 Data 연결 후 `ready`로 유지되어야 한다. 동일한 잘못된 import는 테스트와 이미지 빌드에서 실패로 드러나야 한다.

기존 PT 모델 필수 정책, 모델 준비 실패 시 종료, 자동 재시작 3회 제한을 유지한다. 이번 수정은 분석 패키지의 잘못된 참조와 이를 놓친 검사에 한정한다. 공용 라이브러리에 대체 블랙박스를 추가하거나 분석 플러그인 허용 목록을 넓히지 않는다.

## 3. 수정 순서와 검증

### 1단계: 잘못된 import와 export 제거

- [processors/__init__.py](../server/services/analysis/processors/__init__.py)의 `MetadataBlackBox` import와 `__all__` 항목을 제거한다.
- `PersonAttributeAnalyzer`와 기존 `LocalAppearanceAnalyzer` 호환 별칭을 유지한다. 두 이름은 같은 PAR 분석기 클래스를 가리켜야 한다.
- 명시적 attributes 경로, 패키지 경로, 기존 appearance 경로가 모두 같은 클래스로 해석되는지 `load_factory()`로 확인한다.

**검증:** `ANALYSIS_PLUGIN_ALIASES`의 모든 허용 경로를 모델 인스턴스 생성 없이 import할 수 있어야 한다. `MetadataBlackBox` 참조가 분석 패키지에서 제거되어야 한다.

### 2단계: 테스트가 import 오류를 숨기지 않도록 수정

- [test_attributes.py](../server/services/analysis/tests/test_attributes.py)의 광범위한 `except ImportError`와 이에 따른 `skipTest`를 제거한다.
- 분석 의존성이 설치된 analysis 이미지에서 테스트를 실행하도록 검사 환경을 명시한다. 필수 의존성이나 제품 모듈 import 실패는 검사 실패로 처리한다.
- 기존 번들 검사·다중 라벨 선택·모델 누락 테스트를 유지하고, 허용된 모든 플러그인 경로가 같은 PAR 클래스에 연결되는지 검사한다. 별칭 검사를 의존성 오류로 건너뛰는 클래스에 가두지 않는다.

**검증:** 의존성이 갖춰진 이미지에서 분석 테스트가 건너뛰기 없이 통과해야 한다. 잘못된 import를 포함한 수정 전 소스에서는 검사 자체가 실패해야 한다. 운영 모델 파일은 이 단위 검사에 사용하지 않는다.

### 3단계: 이미지 빌드 시 import 검사 추가

- [analysis Dockerfile](../server/services/analysis/Dockerfile)에서 분석 소스 복사 후 PAR 분석기와 호환 별칭을 import하는 짧은 검사를 추가한다.
- 검사는 실제 런타임 라이브러리와 배포 사용자로 수행한다. 모델을 생성하거나 작업자를 시작하지 않으며 `/models` 마운트·인증 토큰·Data 연결을 요구하지 않는다.

**검증:** 잘못된 패키지 참조나 필수 라이브러리 import 실패가 있으면 이미지 빌드가 비영 종료 코드로 끝나야 한다. 정상 소스에서는 모델 파일이 없는 빌드 환경에서도 검사에 성공해야 한다.

### 4단계: 실제 모델과 격리 실행기 확인

- 새 이미지에 모델 폴더만 읽기 전용으로 연결하고 네트워크를 차단한 임시 컨테이너에서 검증한다.
- 기본 경로의 `par_serving_exp3.pt`를 실제로 로드하고 `IsolatedProcessor` 초기화가 제한시간 안에 완료되는지 확인한다. 직접 클래스 생성뿐 아니라 운영에서 사용하는 자식 프로세스 경로를 검사한다.
- 임시 경로에 만든 비균일 합성 JPEG를 사용해 추론을 실행한다. `outcome=complete`, `backend=par_resnet34`, `backend_version=exp3` 및 8개 속성의 출력 구조·점수 범위를 확인한다. 합성 입력으로 사람에 대한 예측 정확도를 판정하지 않는다.
- 임시 모델 경로로 누락·손상 모델을 검사해 기존 `ANALYSIS_MODEL_MISSING`·`ANALYSIS_MODEL_INVALID` 분류가 유지되는지 확인한다. 운영 파일을 변경하지 않는다.

**검증:** 정상 모델의 격리 초기화·추론이 성공하고, 임시 컨테이너 종료 후 자식 프로세스가 남지 않아야 한다. 검증 중 운영 작업 수령·완료 보고·이벤트 생성은 0건이어야 한다.

### 5단계: analysis만 운영 반영

- 현재 컨테이너의 이미지 식별자, Compose 프로젝트·파일·env 경로를 기록한다. 기존 작업 폴더의 변경분을 검토하고 이번 수정분을 구분한다.
- 같은 설치 설정으로 analysis를 빌드하고, 앞 단계의 검사를 통과한 뒤 analysis만 재생성한다. `restart`만으로는 이미지가 갱신되지 않으므로 새 이미지로 컨테이너를 만든다.
- `/health/live`와 `/health/ready`, 실제 backend, `ready=true`, `model_ready=true`, 컨테이너 health를 확인한다. 초기 준비 후 최소 60초 동안 재시작 횟수가 증가하지 않아야 한다.
- 새로 발생한 정상 분석 작업이 있으면 `par_resnet34 / exp3` 결과와 분석 완료 이벤트가 저장되는지 확인한다. 새 입력이 없으면 서비스 기동 성공과 실제 작업 검증 대기를 구분해 기록한다.

기본 Compose를 사용하는 현재 설치의 실행 예시는 다음과 같다. 구현 시 컨테이너 라벨에 기록된 Compose 파일과 일치하는지 먼저 확인한다.

```powershell
docker compose --env-file 'C:/ProgramData/AI_CCTV/config/compose.env' -f server/compose.yml --profile ai build analysis
docker compose --env-file 'C:/ProgramData/AI_CCTV/config/compose.env' -f server/compose.yml --profile ai up -d --no-deps --force-recreate --wait --wait-timeout 120 analysis
```

## 4. 예상 변경 파일

| 파일 | 변경 내용 |
| --- | --- |
| `server/services/analysis/processors/__init__.py` | 존재하지 않는 블랙박스 참조 제거 |
| `server/services/analysis/tests/test_attributes.py` | import 실패 시 검사 실패, 허용 플러그인 경로 검증 |
| `server/services/analysis/Dockerfile` | 런타임 패키지 import를 빌드 중 검사 |
| `server/services/analysis/README.md` | 필수 의존성 환경에서 검사하는 방법과 준비 상태 확인 절차 |

공유 격리 실행기와 앱의 종료 정책은 이번 오류를 해결하기 위해 변경할 필요가 없다. 이 계획은 [PAR 모델 연결 계획](analysis-par-model-fix-plan.md)의 배포 후 발견된 import 결함을 다룬다.

## 5. 완료 기준과 실패 시 처리

1. 모든 허용 PAR 플러그인 경로가 정상 import되고 호환 별칭이 같은 클래스를 가리킨다.
2. 분석 테스트가 건너뛰기 없이 통과하고 Docker 빌드의 import 검사도 통과한다.
3. 실제 모델의 격리 초기화와 합성 입력 추론이 성공한다.
4. 새 analysis 컨테이너가 `healthy`, readiness가 `ready`로 유지되며 재시작 횟수가 증가하지 않는다.
5. 실제 새 입력이 있는 경우 정상 결과 저장까지 확인한다. 입력 부재로 검증하지 못한 항목은 명시한다.

후속 모델 로드·기동 오류가 나타나면 analysis를 중지한 상태로 두고 해당 오류를 추가 분석한다. 잘못된 import가 남은 이미지나 대체 분석기로 되돌려 성공 상태를 만들지 않는다. 기존 이벤트·분석 결과·녹화·모델 파일은 보존한다.

## 6. Execution result

- Removed the nonexistent `MetadataBlackBox` import and export. Kept `PersonAttributeAnalyzer` and the `LocalAppearanceAnalyzer` compatibility alias.
- Removed the broad `ImportError` catch and dependency skip from `test_attributes.py`; added a check that all four allowed plugin references resolve to the PAR analyzer class.
- Added a Docker build import check under runtime UID 65532. It requires neither the model file nor a Data connection.
- The analysis image build passed, and all 6 `test_attributes.py` tests passed in that image.
- An isolated run with the operational model mounted read-only and networking disabled completed synthetic JPEG inference: `complete`, backend `par_resnet34`, version `exp3`, and 8 attributes.
- Recreated only the analysis service. `/health/live` and `/health/ready` returned HTTP 200; readiness reported `ready=true`, `model_ready=true`, and `last_error=null`. It remained healthy with 0 restarts after 60 seconds.
- No operational camera/Data job completion was verified. Synthetic inference does not replace validation against a real incoming job.
