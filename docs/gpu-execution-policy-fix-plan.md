# GPU 요청 시 CPU 실행 문제 수정 계획서

- 작성일: 2026-11-09 KST
- 대상: YOLO 감지, OSNet 재식별, PAR Analysis의 GPU 요청·실행 불일치
- 상태: 계획 작성. 제품 코드·운영 설정·컨테이너는 이번 단계에서 변경하지 않는다.

## 1. 목표와 적용 전제

사용자가 설치에서 `cuda` 또는 `cuda:N`을 선택하면 감지·재식별·PAR 분석에 그 선택을 일관되게 적용한다. GPU가 없거나 GPU 실행 준비가 실패하면 해당 모델을 정상 GPU 상태로 보고하지 않으며, CPU로 자동 전환하지 않는다.

이번 계획에서는 설치 화면의 단일 추론 장치 선택이 세 모델에 적용된다고 해석한다. 서비스별 명시적인 `cpu` 설정은 의도한 CPU 실행으로 구분하되, 설치기가 GPU 선택을 CPU 설정으로 덮어쓰는 동작은 제거한다. 영상 디코딩·crop 전처리·JSON 생성 등 일반 CPU 작업까지 GPU로 옮기는 것은 목표가 아니다.

`auto`와 명시적 GPU 요청은 구분한다. GPU 모드가 꺼진 기존 CPU 설치의 `auto`는 CPU 사용을 허용한다. GPU 모드에서 `auto`를 사용하는 모델은 GPU 우선이라는 의미가 명확히 드러나도록 아래 정책을 적용한다. 호스트 GPU 자동 발견만으로 기존 CPU 설치를 임의 전환하지 않는다.

## 2. 확인한 현재 상태

이전 확인 요청에서 얻은 기준선은 다음과 같다. 구현 전에 다시 확인하고 실제 배포 설정과 일치하는지 기록한다.

| 항목 | 확인 결과 |
| --- | --- |
| 호스트 | NVIDIA GeForce GTX 1650 with Max-Q Design, VRAM 4096 MiB, 드라이버 566.36 |
| Docker GPU 전달 | 기존 로컬 이미지에 `--gpus all`을 적용한 임시 컨테이너에서 `nvidia-smi` 성공 |
| 설치 env | `PREPROCESSING_GPU=false`, `INFERENCE_DEVICE=auto`, `IDENTITY_DEVICE=auto` |
| 실행 중 Preprocessing | GPU DeviceRequests 없음, PyTorch `2.8.0+cpu`, `torch.version.cuda=null` |
| 실행 중 OSNet | `CPUExecutionProvider`, 실제 장치 `cpu` |
| 실행 중 Analysis | GPU DeviceRequests 없음, PyTorch `2.13.0+cpu` |

Docker가 GPU를 전달할 수 있다는 점은 확인했지만, 이 장비에서 CUDA 모델 추론이 성공한다는 점까지 확인한 것은 아니다. 실제 GPU 이미지·드라이버·CUDA 라이브러리·모델 조합은 후속 인수 검사 대상이다.

### 확인된 코드 문제

1. [설치 설정 생성](../server/installer/configuration.py)은 `inference_device`가 `cuda`로 시작하면 `IDENTITY_DEVICE`를 `cpu`로 기록한다.
2. 새 설치에서 기본 `auto`는 GPU 실행 모드를 켜지 않는다. [ComposeAdapter](../server/operations/compose.py)는 `PREPROCESSING_GPU=true`일 때만 GPU override를 넣으므로, GPU가 있는 호스트에서도 기본 CPU 이미지가 사용될 수 있다. 이는 명시적 CUDA 선택과 별도로 설명해야 하는 현재 `auto`의 범위다.
3. [Analysis Dockerfile](../server/services/analysis/Dockerfile)은 CPU wheel에 고정되어 있고 [GPU Compose](../server/compose.gpu.yml)는 preprocessing에만 GPU를 할당한다. [PAR 분석기](../server/services/analysis/processors/attributes.py)는 네트워크·입력을 CUDA로 이동하지 않는다.
4. [호환 재식별 구현](../server/services/preprocessing/processors/identity/appearance.py)은 OpenCV CPU 타깃을 사용한다. GPU 요청 시 이 구현으로 바뀌면 요청과 실제 실행이 달라진다.
5. Preprocessing readiness·doctor의 GPU 판정은 주로 감지 워커만 확인한다. [공개 진단](../server/services/external/app/diagnostics.py)은 identity 장치 필드를 내보내지 않으며, Analysis에는 장치 메타데이터 자체가 없다.

`map_location="cpu"`는 가중치를 CPU에서 안전하게 읽는 단계이므로 그 자체를 결함으로 보지 않는다. 로드 이후 모델과 입력을 선택한 장치로 이동하는 처리가 필요하다. 감지의 `auto` CPU 폴백은 이미 로그를 남긴다.

## 3. 수정 후 장치 정책

기존 `PREPROCESSING_GPU` 키는 배포 호환성을 위해 유지한다. GPU override를 선택하는 이 키의 적용 범위를 Preprocessing과 Analysis의 GPU 이미지·자원 할당으로 맞춘다. 모델별 실제 요청은 `INFERENCE_DEVICE`, `IDENTITY_DEVICE`, 새 `ANALYSIS_DEVICE`로 확인한다. 추가 GPU 모드 플래그나 별도 설치 선택지는 만들지 않는다.

| 요청/상황 | 처리 |
| --- | --- |
| 설치에서 `cuda` 또는 `cuda:N` 선택 | GPU 모드 활성화, 세 장치 값을 같은 CUDA 장치로 기록. `cuda`는 `cuda:0`으로 정규화 |
| 모델별 `cuda:N` | 해당 장치가 실제로 준비되어야 성공. 잘못된 index·라이브러리 누락·GPU 실행 실패 시 CPU 폴백 금지 |
| GPU 모드=true, 모델별 `auto` 또는 미지정 | GPU가 필요하다는 배포 의도에 따라 기본 GPU를 선택. 준비 실패를 CPU 정상 실행으로 바꾸지 않음 |
| 모델별 명시적 `cpu` | 의도한 CPU 예외로 표시. 다른 GPU 모델까지 CPU로 변경하지 않음 |
| GPU 모드=false, 모델별 `auto` | 기존 CPU 배포 허용. 실제 선택 장치와 선택 사유를 진단에서 확인 가능하게 함 |
| GPU 모드=false인데 모델별 `cuda:N` | CPU 이미지로 실행하기 전에 설정 불일치 오류. GPU override 적용을 요구 |
| 기존 설치를 `auto`로 다시 열기 | 기존 GPU 모드 선택을 보존. 시작할 때마다 호스트를 재탐지해 이미지·설정을 임의 변경하지 않음 |

기존 `IDENTITY_DEVICE=cpu`가 사용자의 수동 선택인지 과거 설치기의 강제 값인지는 값만으로 구별할 수 없다. 모든 CPU 값을 임의 변환하지 않는다. 이번 GPU 재설정에서는 변경 전 값을 기록하고, 사용자가 명시한 CUDA 선택을 세 모델에 다시 적용한다.

## 4. 수정 순서와 검증

### 1단계: 설치·Compose의 GPU 의도 전달

- `initialize()`의 identity CPU 강제 대입을 제거한다. GPU 선택 시 `INFERENCE_DEVICE`, `IDENTITY_DEVICE`, `ANALYSIS_DEVICE`를 같은 선택값으로 저장한다. CPU 선택 시 세 모델을 CPU로 맞춘다.
- GUI·CLI의 장치 설명과 설치 결과 요약에 선택 범위 및 `auto`의 의미를 반영한다. 기존 `auto` 설치가 GPU 강제 요청인 것처럼 표시되지 않게 한다.
- `ANALYSIS_DEVICE`를 기본 Compose 환경과 예제 env에 추가한다. 누락된 기존 설정도 정책표에 따라 해석한다.
- `compose.gpu.yml`에 Analysis GPU 이미지·자원·빌드 인자를 추가한다. Preprocessing과 Analysis 모두 기본 CPU 이미지와 GPU 이미지를 별도 태그로 구분한다.
- 시작·빌드·재생성·진단이 같은 env와 Compose 파일 조합을 사용하는지 확인한다. GPU 설치에서 기본 `compose.yml`만 사용한 수동 빌드·재생성 예제는 GPU override를 포함하도록 고친다.

**검증:** 임시 설치 디렉터리에서 `cpu`, `auto`, `cuda:0`, 다른 GPU index, 기존 GPU 설치 재실행을 확인한다. Compose 해석 결과의 두 서비스 이미지·빌드 인자·DeviceRequests 요청을 검사한다. GPU 요청과 CPU 이미지가 섞이면 배포 전에 실패해야 한다.

### 2단계: 감지·OSNet·호환 플러그인의 실행 정책 일치

- 감지의 기존 명시적 CUDA 실패 정책을 유지하고, GPU 모드의 `auto` 처리만 정책표와 맞춘다. 장치 문자열 검증·index 검증을 서비스마다 다르게 처리하지 않는다.
- OSNet은 명시적 GPU 요청에서 CUDA provider 누락·세션 준비 실패·첫 실제 추론 실패를 오류로 처리한다. 실행 중 GPU 실패 후 CPU 세션으로 재생성되는 경로도 허용하지 않도록 실제 ONNX Runtime API와 동작을 확인한다.
- `CUDAExecutionProvider`가 목록에 있다는 사실만으로 GPU 실행 성공으로 판정하지 않는다. 실제 OSNet 세션으로 추론하고, 통합 검증에서는 주요 Conv 등 모델 연산이 CUDA provider에서 수행되는지 확인한다. CPU의 보조 연산 존재를 전체 CPU 실행과 혼동하지 않는다.
- CPU 전용 `LocalAppearanceIdentity`는 GPU가 필요한 요청에서 명확한 설정 오류로 거부한다. OpenCV CUDA 기능을 새로 구축하거나 모델을 검증 없이 OSNet으로 바꾸지 않는다. 명시적 CPU 사용과 기존 특징 공간은 유지한다.
- 마이그레이션 도구가 GPU 설정에서 CPU 전용 호환 플러그인을 조용히 선택하지 않게 한다.

**검증:** GPU 모드·요청 장치·provider 유무·잘못된 index·초기화/추론 오류 조합을 검사한다. 명시적 GPU 요청의 실패 후 CPU 실행이 한 번이라도 수행되면 실패다. CPU 모드와 기존 identity 특징·GPID 연결 검사는 계속 통과해야 한다.

### 3단계: Analysis의 GPU 이미지와 실제 PAR 추론

- Analysis Dockerfile의 PyTorch 설치를 CPU/GPU 빌드 인자로 선택할 수 있게 한다. 기본 CPU 빌드를 보존하고 GPU override에서 CUDA wheel 조합을 선택한다.
- PyTorch·torchvision 버전, CUDA wheel 제공 여부, GTX 1650 아키텍처와 현재 드라이버 지원을 공식 배포 자료와 실제 커널 실행으로 확인한 뒤 버전을 고정한다. 현재 CPU 버전에 GPU wheel이 있을 것이라고 가정하지 않으며, 단순 버전 통일을 위한 의존성 일괄 업그레이드는 하지 않는다.
- 기존 런타임 사용자 import 빌드 검사는 유지한다. 이미지 빌드 자체에는 GPU나 모델 파일을 요구하지 않고, CUDA 실행 검사는 별도 런타임 단계에서 한다.
- `PersonAttributeAnalyzer`에서 `ANALYSIS_DEVICE`와 GPU 모드를 읽고 실제 실행 장치를 정한다. 가중치는 기존 제한 로더로 CPU에 읽은 뒤 네트워크를 선택 장치로 이동하고, 입력 batch도 같은 장치로 이동한다.
- 결과를 NumPy로 바꾸기 전에 출력 텐서를 CPU로 옮긴다. 결과 schema·backend·8개 속성·점수 형식은 유지한다.
- 준비 완료 전에 실제 장치에서 합성 입력으로 warm-up한다. CUDA가 보이지만 지원 커널·VRAM·라이브러리 문제로 모델이 실행되지 않는 경우도 이 단계에서 검출한다. 이 출력은 운영 결과·이벤트·identity gallery에 저장하지 않는다.
- `runtime_metadata()`로 `requested_device`, `execution_device`, `execution_provider`를 공통 격리 실행기에 전달한다. provider는 제한된 PyTorch CPU/CUDA 식별값을 사용한다.
- 장치 준비 실패가 `ANALYSIS_MODEL_INVALID`로 잘못 분류되지 않도록 로드·장치 준비 예외를 구분한다. 필요한 안전 오류 코드(예: `ANALYSIS_DEVICE_UNAVAILABLE`, `ANALYSIS_DEVICE_EXECUTION_FAILED`)를 격리 실행기의 허용 목록과 관리 화면 설명까지 함께 반영한다.

**검증:** 실제 PT 파일을 읽기 전용으로 연결하고 네트워크를 차단한 임시 GPU 컨테이너에서 `IsolatedProcessor`의 spawn 경로로 초기화·합성 JPEG 추론을 수행한다. 모델과 입력의 실제 CUDA 장치, 결과 8개 속성, 유한한 점수 범위를 확인한다. GPU가 없는 실행·잘못된 index·CUDA 실행 실패에서는 준비 완료나 CPU 성공 결과가 나오면 안 된다. CPU 이미지에서도 기존 PAR 검사를 유지한다.

### 4단계: readiness·doctor·외부 진단 보완

- 감지뿐 아니라 identity와 Analysis의 요청 장치·실제 장치·provider를 각각 판정한다. 의도한 CPU 예외와 GPU 요청 실패를 구분한다.
- GPU가 필요한 모델이 CPU로 실행 중이면 정상 GPU 상태를 반환하지 않는다. 시작 전 오류와 가동 중 오류는 기존 서비스 수명·유한 재시도 정책에 맞춰 처리한다.
- 카메라 워커가 0개면 감지 GPU 사용은 미검증으로 보고한다. identity·PAR warm-up 성공만으로 감지의 실제 실행까지 검증됐다고 표시하지 않는다.
- External의 identity 및 Analysis 진단에 제한된 장치 필드를 포함한다. 장치 이름·오류 코드는 검증된 형식만 공개하며 토큰·인증 URL·원시 예외를 넣지 않는다.
- doctor의 자원 할당 검사를 Analysis에도 적용하고, 실제 모델 실행 검증과 `torch.cuda.is_available()` 검사 결과를 구분한다. 선언된 응답 스키마가 영향을 받으면 OpenAPI도 함께 갱신한다.

**검증:** 감지 CUDA/identity CPU, 감지 CPU/identity CUDA, PAR CPU, 전부 CUDA, 명시적 CPU 예외, 워커 없음, provider 준비 실패의 진단을 확인한다. GPU가 필요한 경로가 CPU인데 `OK`로 묶이면 실패다.

### 5단계: 기존 배포와 계약 서명 호환

- 기존 이미지·설정·선택한 Compose 파일을 기록하고, 현재 수정 중인 다른 작업과 GPU 변경을 구분한다.
- Compose 환경 항목 변경은 [배포 계약 서명](../server/deployment/signature.py)에 영향을 준다. 기존 서명과 새 서명의 차이가 배포 계약에 한정되는지 확인하고, 필요한 경우 검증된 이전 서명에만 적용하는 비파괴 호환 갱신을 추가한다.
- DB 스키마·도메인 데이터 계약을 그대로 유지한다. 서명 불일치를 우회하기 위해 배포 초기화나 데이터 삭제를 실행하지 않는다.
- GPU 재설정에서 기존 토큰·모델·카메라·이벤트·저장 경로를 보존한다. GPU를 선택했는데 재초기화 후 identity만 CPU로 돌아가는 회귀도 검사한다.

**검증:** 이전 정상 배포 → GPU 설정 갱신 → 재시작 후 세 장치 설정이 유지되어야 한다. 계약과 무관한 손상·다른 데이터 루트의 서명 불일치는 기존처럼 거부해야 한다.

### 6단계: 실제 장비 인수와 배포

1. 기본 CPU 이미지와 두 GPU 이미지를 검증한다. GPU 검증은 현재 GTX 1650에서 실제 모델 커널을 실행해 판정한다.
2. Preprocessing과 Analysis를 먼저 임시 컨테이너에서 각각 확인한 뒤, 함께 실행했을 때 VRAM과 초기화·추론 시간을 측정한다. 4GB GPU에서 감지·OSNet·PAR가 동시에 실행 가능한지 확인한다.
3. 같은 모델·해상도·입력과 동일한 카메라 수로 CPU 기준선과 비교한다. 감지 p50/p95, 좌표 PUT, 사용자 좌표 API의 관측 나이, identity·analysis 작업 지연, GPU 메모리와 재시작을 기록한다.
4. 두 AI 서비스만 새 이미지로 재생성한다. 컨테이너 GPU 요청·실제 CUDA 모델 실행·각 readiness·doctor·External 결과가 일치하는지 확인한다.
5. 실제 카메라에서 준비 이후 5분 이상 확인한다. 감지 추론 p95 1초 이하, 사용자 좌표 API 관측 나이 p95 2초 이하를 목표로 하며, GPU 사용만으로 바운딩 박스 문제가 해결됐다고 판정하지 않는다.
6. `pid:n gpid:null` 출현·박스, GPID 후속 반영, PAR 결과 표시가 정상인지 확인한다. GPU 전환이 탐지 누락·추적 단절·분석 결과 형식 회귀를 만들면 인수 실패다.

카메라가 꺼져 있으면 고정 입력·격리 검증과 실제 영상 검증을 구분해 기록한다. VRAM 부족·CUDA 오류가 생겼을 때 CPU로 바꿔 성공으로 처리하지 않는다. 필요한 자원·동시 실행 범위를 기록하고 GPU 요구를 충족할 때까지 해당 인수 항목을 미완료로 둔다.

## 5. 예상 변경 파일

| 파일 | 변경 내용 |
| --- | --- |
| `server/installer/configuration.py`, `gui.py`, `server/cli.py` | GPU 선택 전달, identity CPU 강제 제거, 장치 범위 설명 |
| `server/compose.yml`, `compose.gpu.yml`, `.env.example` | Analysis 장치·CUDA 빌드·GPU 자원, CPU/GPU 이미지 구분 |
| `server/operations/compose.py`, 설치 workflow의 관련 경로 | 같은 GPU Compose 조합 사용 및 설정 충돌 검사 |
| `server/services/preprocessing/app/settings.py`, `processors/detection/device.py`, `processors/identity/osnet.py` | GPU 모드와 모델별 실제 요청 정책 일치 |
| `processors/identity/appearance.py`, `server/setup/tools/enable_object_processing.py` | GPU 요청의 CPU 전용 호환 경로 거부 |
| `server/services/analysis/Dockerfile`, `processors/attributes.py`, `app/main.py` | GPU 빌드·모델/입력 이동·warm-up·장치 메타데이터·준비 판정 |
| `lib/ai_cctv_core/processing/isolation.py` | 필요한 안전 장치 오류 코드 전달; 격리 수명 구조 유지 |
| Preprocessing `app/main.py`, `server/operations/doctor.py`, `services.py` | 세 모델의 GPU 판정과 오류 설명 |
| `server/services/external/app/diagnostics.py` 및 영향받는 응답 스키마 | identity·Analysis 장치 공개 진단 |
| `server/deployment/signature.py` | 필요한 배포 계약 호환 갱신 |
| 해당 테스트·서비스 README·배포/운영 문서 | 장치 정책·실패 시 동작·실행 검증·재생성 명령 |

기존 검사를 확장하는 것을 우선한다. 대상에는 `test_device_policy.py`, `test_osnet_provider.py`, Analysis `test_attributes.py`, `test_operations_compose.py`, `test_operations_doctor_gpu.py`, `test_contract_signature.py`, External 진단 및 설치 설정 검사가 포함된다. 장치 선택을 모의한 단위 검사와 실제 GPU 커널 검사는 서로 대체하지 않는다.

## 6. 완료 기준과 되돌림

- 설치에서 명시한 CUDA 장치가 세 모델 설정에 반영되고 재시작 후 유지된다.
- GPU가 필요한 감지·OSNet·PAR는 실제 GPU 추론에 성공하며 CPU 폴백이 없다.
- GPU 불가·잘못된 index·런타임 실패는 명확한 오류이며 GPU 정상으로 표시되지 않는다.
- 의도한 CPU 설치 및 모델별 CPU 예외는 정상 작동하고 실제 장치가 정확히 표시된다.
- 기존 GPID 연결·analysis 이벤트/결과·모델 파일·DB·인증 설정이 유지된다.
- 고정 입력 검사와 실제 영상 인수 결과, 남은 제약을 따로 기록한다.

문제가 생기면 GPU 변경 전 이미지·설정만 복원한다. 기존 값이 CPU였으면 복원 상태를 CPU로 명확히 보고하고, GPU 수정 완료라고 기록하지 않는다. 데이터를 초기화하거나 모델을 자동 교체하지 않는다.

## 7. 기존 표시 문제 계획과의 관계

[PID 출현·바운딩 박스 계획](pid-appearance-and-live-box-fix-plan.md)의 관측 창 0초 변경은 별도 원인이므로 독립적으로 검증한다. 추론 성능 단계에서는 이번 GPU 실행 일치 작업을 먼저 수행하고, 이후에도 GPU 실행이 목표 지연을 충족하지 못할 때 해당 경로의 병목을 측정한다. CPU 튜닝은 의도한 CPU 배포의 대책으로 유지한다. 좌표의 3초 만료 정책을 느슨하게 하거나 촬영 시각을 바꿔 GPU 전환 실패를 숨기지 않는다.
