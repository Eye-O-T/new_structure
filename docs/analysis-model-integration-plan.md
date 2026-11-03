# Analysis 제출 모델 적용 분석 및 실행 계획서

작성일: 2026-11-03, Asia/Seoul  
분석 대상: `par_serving_exp3.pt`와 현재 작업 트리  
기준 커밋: `032213a` — 기존 미커밋 변경이 있으므로 분석 기준은 커밋 단독이 아닌 현재 파일 내용이다.  
최초 작성 범위: 파일·소스·설정 조사와 계획 작성.

## 구현 상태 — 2026-11-03

P0 적용을 완료했다.

- `attributes.py`에 `weights_only=True` 제한 로딩과 번들 계약 검증을 추가했다.
- Analysis 단위 테스트 3건과 CI 테스트 경로를 추가했다.
- 제출 모델을 `C:\ProgramData\AI_CCTV\models\par_serving_exp3.pt`에 해시 검증 방식으로 설치했다.
- 실제 `compose.env`를 `compose.env.before-par-exp3.bak`으로 백업하고 `PersonAttributeAnalyzer`를 선택했다.
- 운영 태그 `ai-cctv-analysis:0.3.0`을 빌드했다. 서버 전체는 작업 전부터 중지되어 있어 기동하지 않았다.
- 컨테이너에서 제한 로딩, strict 가중치 적용, 8개 헤드의 유한 출력, 실제 JPEG 전체 추론, Compose 플러그인 선택과 읽기 전용 모델 마운트를 확인했다.

실제 카메라 이벤트의 Data 저장·External API 확인과 운영 부하는 서버 및 카메라를 기동한 현장 검증으로 남아 있다. Desktop·Mobile 속성 표시, 과거 `complete` 이벤트 재처리, 설치 도구의 PAR 전용 입력은 계획대로 후속 범위다.

## 1. 적용 위치와 권고안

제출 파일은 **사람 크롭의 외관 속성을 예측하는 ResNet34 다중 헤드 모델 번들**이다. 기존 `server/services/analysis/processors/attributes.py`의 `PersonAttributeAnalyzer`에 연결하는 것이 적합하다. 이 클래스는 이미 제출 번들의 형식, 기본 파일명, 전처리, 출력 헤드, 후처리를 구현하고 있다.

현재 로컬 설치를 기준으로 필요한 연결은 다음과 같다.

| 구분 | 적용 대상 | 판단 근거 |
|---|---|---|
| 호스트 모델 파일 | `C:\ProgramData\AI_CCTV\models\par_serving_exp3.pt` | 실제 설치 `compose.env`의 `MODELS_DIR`가 이 폴더를 지정한다. |
| 컨테이너 모델 파일 | `/models/par_serving_exp3.pt` | Compose가 `MODELS_DIR`를 `/models`에 읽기 전용 마운트하며 기존 분석기의 기본 경로와 같다. |
| 모델 실행 코드 | `server/services/analysis/processors/attributes.py`의 `PersonAttributeAnalyzer` | 번들 구조와 기존 네트워크의 가중치 키·크기가 정적으로 일치한다. |
| 플러그인 선택 | `C:\ProgramData\AI_CCTV\config\compose.env`의 `ANALYSIS_PLUGIN` | 현재 이 키가 없어 소스 Compose 기준 기본 색상 분석기를 선택한다. |
| 결과 저장 | 이벤트의 `metadata.analysis.result` | Data가 Analysis 완료 응답의 `metadata`를 이 위치에 저장한다. |

활성화할 플러그인 값은 다음과 같다.

```dotenv
ANALYSIS_PLUGIN=server.services.analysis.processors.attributes:PersonAttributeAnalyzer
```

**권고: 기존 CPU 구현과 기본 파일명을 먼저 사용하여 연결·검증한다.** 현재 Compose는 이 플러그인 선택값과 모델 디렉터리 마운트를 이미 지원하므로 첫 적용에 Compose 수정이나 새로운 추론 서비스가 필요하지 않다. 배포용 로더 보완과 검증을 선행한 뒤 실제 설치 설정에서 분석기를 선택한다.

이 모델은 YOLO 탐지 모델이나 OSNet 재식별 모델을 대체하지 않는다. `MODEL_FILE`과 `IDENTITY_MODEL_PATH`는 각각 현재 탐지·재식별 용도다. 설치 화면의 기존 탐지 모델 입력에 이 파일을 지정하면 PAR 모델이 YOLO 경로로 전달되므로, 별도 모델로 취급해야 한다.

## 2. 확인 범위와 현재 설치 상태

### 직접 확인한 사항

- 제출 원본: `C:\Users\gksrn\Documents\카카오톡 받은 파일\par_serving_exp3.pt`.
- 실제 설치 설정 파일: `C:\ProgramData\AI_CCTV\config\compose.env`가 존재한다. 저장소의 `server/.env`는 없다.
- 설치 설정: `MODELS_DIR='C:\ProgramData\AI_CCTV\models'`, `MODEL_FILE=yolo11n.pt`, `IDENTITY_MODEL_PATH=/models/osnet_x0_25_msmt17.onnx`, `PREPROCESSING_GPU=false`, `INFERENCE_DEVICE=auto`.
- 설치 설정에는 `ANALYSIS_PLUGIN`, `PAR_MODEL_PATH`가 없고, 모델 폴더에는 YOLO·OSNet 및 OSNet 동반 파일만 있다. 제출 PAR 파일은 아직 없다.
- 소스의 `app/main.py`, `compose.yml`, `.env.example`은 기본 분석기로 `LocalAppearanceAnalyzer`를 선택한다.
- `attributes.py`와 CPU PyTorch 설치 구문은 이미 추적 중인 프로젝트 파일에 존재한다.

### 이번 분석에서 검증하지 못한 사항

- 호스트 Python에는 `torch`가 설치되어 있지 않다. 이번 작업에서는 새 패키지를 설치하지 않았다.
- `docker ps`가 Docker named pipe 접근 권한 오류로 실패했다. 실행 컨테이너의 이미지 버전·환경변수·상태는 확인하지 못했다. 설정 파일을 읽었다는 사실만으로 실행 중인 컨테이너의 설정까지 같다고 단정하지 않는다.
- 모델 forward, 실제 `load_state_dict`, CPU 지연시간·메모리, 실제 카메라 정확도는 미검증이다. 아래 정적 호환성 결과와 구분한다.
- 제공 파일 외의 학습 소스, 데이터셋 설명, 라벨별 평가표, 기준 이미지와 기준 출력은 전달받지 못했다.

모델에 포함된 라벨·수치·표시 규칙은 분석 대상 데이터로 읽었으며, 이를 사용자 작업 지시로 취급하지 않았다.

## 3. 제출 파일 조사 결과

### 3.1 파일 식별 및 무결성

| 항목 | 조사 결과 |
|---|---|
| 파일 크기 | 85,373,069 bytes, 약 81.42 MiB |
| SHA-256 | `e3ec03c516ab6429dada23b285def0509478332028180361bc9ee19404033c69` |
| 저장 형식 | ZIP 기반 PyTorch 체크포인트, 최상위 딕셔너리 번들 |
| 번들 형식·버전 | `format=par-serving-bundle`, `format_version=1` |
| Backbone·모델 버전 | `resnet34`, `exp3` |
| 저장된 학습 정보 | `trained_epoch=23`, `val_macro_f1=0.5693593725489301` |
| 가중치 항목 | `state_dict` 232개 텐서, 파라미터와 버퍼를 합쳐 21,323,278개 원소 |
| 저장 dtype | FloatStorage 196개, LongStorage 36개 |
| 구조 비교 | ResNet34의 3/4/6/3 residual block과 8개 선형 헤드를 기준으로 232개 키·크기 모두 일치. 누락·추가·크기 불일치 0건 |
| 수치·파일 검사 | 모든 저장 텐서의 NaN/Inf 0건, 저장소 바이트 수 검사 통과, ZIP 전체 CRC 검사 통과 |

조사는 `zipfile`과 `pickletools`로 pickle 연산을 정적으로 해석하고, 텐서 저장소를 NumPy 배열로 읽는 방식으로 수행했다. 파일이 지정하는 Python 함수를 호출하거나 일반 pickle 역직렬화를 수행하지 않았다. TorchScript 실행 코드가 든 모델이 아니라 `state_dict`와 설정을 담은 번들이다.

이 검사는 파일 손상과 구조 불일치를 찾는 검사다. 정답률, 제출자 신원, 학습 정보의 진실성을 증명하지 않는다. 특히 저장된 macro F1 약 0.5694는 번들 자체의 기록이며 현재 CCTV 환경의 정확도가 아니다. 이를 “정확도 56.94%”로 표시하면 안 된다.

### 3.2 입력과 전처리 계약

번들에 저장된 값은 다음과 같다.

- `input_size=[256, 128]`.
- 정규화 평균: `[0.485, 0.456, 0.406]`.
- 정규화 표준편차: `[0.229, 0.224, 0.225]`.

현재 `PersonAttributeAnalyzer`는 이를 높이 256, 너비 128로 사용한다. 공용 로더의 uint8 BGR 이미지를 RGB로 바꾸고, PIL 이미지로 변환한 뒤 `Resize((256, 128)) → ToTensor() → Normalize(...)`를 적용한다. 입력 텐서는 float32 `1×3×256×128`이다.

크롭 로더가 이미 보장하는 조건은 다음과 같다.

- 공유 스냅샷 저장소 내부의 실제 파일이며, 상대 경로 검증을 통과해야 한다.
- 완전한 JPEG, 최대 8 MiB, 최대 16,000,000픽셀.
- 디코딩 전후 이미지 크기가 관측 bbox의 너비·높이와 정확히 같아야 한다.
- PAR 분석기는 각 변 16픽셀 미만인 입력을 거부한다.

학습 때의 RGB 순서, resize 보간법·antialias, 종횡비 변형 허용 여부는 번들만으로 확정할 수 없다. 현재 코드가 사용하는 PIL/torchvision 처리와 학습 기준 출력의 일치를 확인해야 한다. 이미지가 입력 가능하다는 조건과 속성 추정에 충분한 화질이라는 조건도 별개다.

### 3.3 출력 헤드와 라벨

모든 헤드는 512차원 공유 특징을 입력받는다. 아래의 라벨 순서는 번들에 저장된 순서이며, 가중치와 함께 보존해야 한다.

| 속성 | 클래스 수 / 가중치 크기 | 라벨 | 현재 코드의 해석 |
|---|---|---|---|
| `gender` | 2 / `2×512` | Male, Female | softmax 후 최고점 하나 |
| `age` | 5 / `5×512` | <=15, 16-30, 31-45, 46-60, 61+ | softmax 후 최고점 하나 |
| `hat` | 2 / `2×512` | No Hat, Hat | softmax 후 최고점 하나 |
| `upper_color` | 11 / `11×512` | Black, Blue, Brown, Green, Grey, Orange, Pink, Purple, Red, White, Yellow | sigmoid, 최고점 강제 포함 + 0.7 이상 |
| `lower_color` | 11 / `11×512` | 위와 같은 색상 순서 | sigmoid, 최고점 하나만 포함 |
| `sleeve` | 3 / `3×512` | Long Sleeve, Short Sleeve, No Sleeve | softmax 후 최고점 하나 |
| `lower_type` | 3 / `3×512` | Long Pants, Short Pants, Skirt | softmax 후 최고점 하나 |
| `carrying` | 5 / `5×512` | Backpack, MessengerBag, PlasticBag, Luggage, Other | sigmoid, 0.6 이상만 포함 |

출력은 합계 42개 logit을 8개 속성으로 나누는 구조다. 색상·소지품은 번들의 `multi_label`에 포함되며 softmax를 적용하면 안 된다. 현재 구현은 다중 라벨 결과를 점수순 최대 3개로 자르고, 점수는 소수 넷째 자리로 반올림한다.

특히 다음 동작을 검증·문서화해야 한다.

- 상의 색상은 최고점이 0.7 미만이어도 최소 하나가 나온다.
- 하의 색상은 `threshold=null`, `always_top=true`여서 현재 규칙상 하나만 나온다.
- 소지품은 모든 점수가 0.6 미만이면 빈 목록이다. 빈 목록을 “소지품 없음이 확인됨”으로 바꾸지 않는다.
- 단일 분류 속성은 낮은 점수에서도 하나를 선택한다. 현재 구현에는 `unknown` 판정이 없다.
- 나이·성별 값도 영상에서 추정한 모델 라벨이다. 실명, 실제 나이, 신원 식별의 근거가 아니다.
- 이 파일에는 흉기·위험도·행동 분류 헤드가 없다. `processors/mock.py`의 가짜 위험도 필드와 연결하지 않는다.

## 4. 프로젝트 데이터 흐름과 연동 경계

```mermaid
flowchart LR
    A[카메라 / MediaMTX] --> B[Preprocessing: YOLO 탐지·추적]
    B --> C[스냅샷·사람 크롭 저장]
    B --> D[Data: 이벤트와 객체 작업 생성]
    D --> E[identity 작업: OSNet]
    D --> F[analysis 작업: 공용 Worker]
    C --> G[PersonAttributeAnalyzer]
    M[/models/par_serving_exp3.pt] --> G
    F --> G
    G --> H[Data: metadata.analysis.result]
    H --> I[External: 인증된 이벤트 API]
    I --> J[Desktop / Mobile]
```

1. Preprocessing은 `object_observation`을 가진 사람 이벤트와 크롭 경로를 Data에 전달한다.
2. Data의 `_enqueue_object_jobs()`는 해당 이벤트에 `identity`, `analysis` 작업을 각각 만든다. 현재 분석은 이벤트 크롭 단위이며 실시간 모든 프레임에 속성을 붙이는 경로가 아니다.
3. Analysis의 `app/main.py`는 공용 `running_worker()`에 플러그인 참조를 전달한다.
4. `load_factory()`는 `모듈:팩토리`를 import하고, `IsolatedProcessor`가 `spawn` 자식 프로세스에서 모델을 한 번 초기화한다.
5. `ObjectWorker`는 Data에서 작업 하나를 임대하고, 안전한 크롭 경로를 검증한 뒤 `process(job, crop_path)`를 호출한다.
6. 분석기는 `outcome`과 `metadata`를 반환한다. 공용 Worker가 `lease_id`를 붙여 완료 API에 제출한다.
7. Data는 유효한 lease인지 확인한 뒤 최신 이벤트 JSON의 `analysis` 부분을 갱신한다. 별도로 끝나는 identity 결과는 보존한다.

Analysis는 Data API를 사용하며 DB를 직접 열지 않는다. `global_person_id`나 `identity_descriptor`도 반환하지 않는다. 이 제한은 Worker와 Data 저장소에서 검사하므로 기존 계약을 유지한다.

## 5. 이미 구현된 부분과 적용 전 보완점

| 항목 | 현재 상태 | 실행 계획 |
|---|---|---|
| 네트워크 구조 | ResNet34 + `ModuleDict` 선형 헤드 구현 완료 | 기존 구현 재사용. 새 모델 클래스나 추론 API 추가 불필요 |
| 가중치 로딩 | `load_state_dict()`의 기본 strict 로딩 사용 | 실제 CPU 환경에서 strict 로딩을 실행해 정적 결과 확정. `strict=False`로 우회하지 않음 |
| 플러그인 선택 | 기본값은 `LocalAppearanceAnalyzer` | 대상 배포의 `ANALYSIS_PLUGIN`만 PAR 모듈 경로로 변경 |
| 플러그인 export | `processors/__init__.py`는 PAR 클래스를 export하지 않음 | `processors.attributes:PersonAttributeAnalyzer`를 직접 선택하면 수정 불필요. `processors:PersonAttributeAnalyzer`는 현재 사용할 수 없음 |
| 모델 마운트 | 두 서비스가 공통 `/models` 읽기 전용 마운트 사용 | 실제 `MODELS_DIR`에 별도 이름으로 복사하고 해시 비교 |
| 경로 설정 | Python은 `PAR_MODEL_PATH`를 읽지만 Compose environment에는 이 키가 없음 | 기본 파일명으로 첫 적용. 별도 경로가 필요하면 후속 배포 변경에서 전달 경로 추가 |
| 번들 로더 | `torch.load(..., weights_only=False)` 및 일부 키 존재만 검사 | 제한 로더 적용과 지원 번들 계약 검증을 배포 전에 보완 |
| 실행 장치 | 네트워크와 입력 모두 CPU에 생성됨 | 첫 배포는 CPU. `INFERENCE_DEVICE=cuda:0`이나 GPU override만으로 PAR이 GPU로 전환되지 않음 |
| 의존성 | Dockerfile이 CPU torch/torchvision을 이미 설치 | 기존 버전 조합으로 import·빌드·forward 확인. 필요 없는 중복 설치나 ONNX 변환 생략 |
| 설치 도구 | 탐지 모델·OSNet만 별도 처리 | 초기 적용은 검증된 수동 배치. 반복 설치 지원은 PAR 전용 선택·검증 추가 |
| 설정 재생성 | 설치 설정 생성기의 `compose_values`에 PAR 선택값 보존 로직이 없음 | 재설정·재설치 시 수동 선택이 사라질 수 있음. 제품화 단계에서 값 보존 검증 추가 |
| 결과 계약 | 자유 JSON `metadata` 안에 저장 가능 | DB 스키마 변경 없이 기존 Analysis 완료 계약 사용 |
| 화면 표시 | Desktop·Mobile 상세가 PAR attributes를 표시하지 않음 | 화면 표시가 필요할 때 별도 단계로 구현. 서버 연결 완료와 사용자 표시 완료를 구분 |
| 테스트·CI | 현재 CI의 서버 검사 대상에 Analysis 전용 테스트 경로가 없음 | Analysis 단위·실모델 검사 경로를 명시적으로 추가 |

### 5.1 로더 보완의 구체 범위

현재 번들은 텐서, 기본 자료형, 딕셔너리, OrderedDict로 구성되어 있다. `weights_only=True`, `map_location="cpu"`로 읽는 방식을 우선 검증하고 사용한다. 실패했다고 자동으로 `weights_only=False`로 되돌리는 경로는 두지 않는다. PyTorch 문서는 제한 모드가 텐서와 기본 자료형 등의 로딩에 한정된다고 설명한다. [공식 torch.load 문서](https://docs.pytorch.org/docs/2.15/generated/torch.load.html)

검증 항목은 현재 구현이 실제 지원하는 계약에 한정한다.

- `format=par-serving-bundle`, `format_version=1`, `backbone=resnet34` 확인. 현재 코드는 backbone 이름을 검사하지 않고 항상 ResNet34를 만든다.
- 입력 크기의 두 양수 정수, 정규화 값의 3채널 길이·유한 수치·양수 표준편차 확인.
- 속성명·비어 있지 않은 라벨 목록, `multi_label`과 `display_rules`의 유효한 속성 참조 확인.
- 임계값의 `null` 또는 0~1 유한 수치, `always_top`의 boolean 형식 확인.
- 가중치 키·shape strict 일치와 실제 추론의 유한 출력 확인.

모델 아키텍처를 자동 추론하거나 모든 backbone을 지원하는 범용 프레임워크는 만들지 않는다. 번들의 모델 버전·파일 SHA-256과 실제 실행 패키지 버전은 배포 기록으로 남긴다.

### 5.2 의존성 검토 결과

현재 Dockerfile의 고정값은 `torch==2.13.0+cpu`, `torchvision==0.28.0+cpu`다. 공식 호환 표에서 torch 2.13 / torchvision 0.28은 Python 3.11을 지원하고, 공식 CPU 인덱스에 두 패키지의 CPython 3.11 Linux x86_64 wheel이 존재한다. 따라서 파일 내용만 보고 버전 오타나 미출시 버전이라고 판단할 근거는 없다. 실제 설치·import 성공은 이미지 빌드로 검증해야 한다. [공식 호환 표](https://github.com/pytorch/vision#installation), [torch CPU 인덱스](https://download.pytorch.org/whl/cpu/torch/), [torchvision CPU 인덱스](https://download.pytorch.org/whl/cpu/torchvision/)

Pillow·torchvision의 실제 버전과 resize 동작도 기준 출력 비교에 함께 기록한다. 첫 연결을 위해 전체 프로젝트의 torch 버전을 통일하거나 Preprocessing 의존성을 바꾸지 않는다.

## 6. 변경 대상과 우선순위

아래 표는 **후속 구현·배포 때의 계획**이며 이번 문서 작성에서 적용한 변경이 아니다.

| 우선순위 | 파일·설정 | 필요한 작업 | 완료 기준 |
|---|---|---|---|
| P0 | `server/services/analysis/processors/attributes.py` | 제한 로딩·현재 번들 계약 검사 보완, 기존 전처리·후처리 유지 | 실모델 strict 로딩·기준 출력·오류 입력 검사 통과 |
| P0 | 신규 `server/services/analysis/tests/test_attributes.py` | 전처리와 표시 규칙, 잘못된 번들·크롭, 결과 계약 검사 | 실제 실패 조건을 재현하는 테스트 통과 |
| P0 | 운영 `MODELS_DIR`와 `compose.env` | 모델 파일 설치, PAR 플러그인 선택 | 해시 일치, 실제 컨테이너에서 선택값과 파일 가독성 확인 |
| P0 | `server/services/analysis/README.md` | PAR 선택 방법·모델 경로·출력 구조·CPU 정책·검증 방법 기록 | 현행 기본 분석기 설명과 PAR 사용 설명이 구분됨 |
| P1 | `server/installer/models.py`, `server/installer/configuration.py` | PAR 전용 선택·검증·복사, 선택값 보존 추가 | 탐지 모델·OSNet을 바꾸지 않고 최초 설치·재설정 후 선택 유지 |
| P1 | `server/operations/compose.py`의 사전 점검 | PAR를 선택한 경우에만 PAR 파일 존재·경로 확인 | 일반 색상 분석기 배포는 PAR 없이 동작하고, PAR 누락은 기동 전에 안내 |
| P1·조건부 | `server/compose.yml`, `server/.env.example` | 기본 파일명 이외의 경로가 필요할 때 `PAR_MODEL_PATH` 전달 추가 | env 입력이 컨테이너에 전달되며 계약 변경 절차 검증 완료 |
| P1 | `.github/workflows/ci.yml` | Analysis 테스트를 실행하는 CPU 검사 경로 추가 | Analysis 관련 검사가 CI에서 누락되지 않음 |
| P1 | `server/tools/export_release_manifest.py` 또는 별도 검증 기록 | Analysis 의존성·모델·기준 출력 기록 보완 | 이미지 ID, 모델 해시, 패키지 버전, 검증 결과 추적 가능 |
| P2·선택 | `server/desktop/admin/application.py`의 `open_event_detail()` | 이벤트 Analysis 상태와 8개 속성 결과 표시 | 대기·실패·기존 backend·새 backend를 구분하고 낮은 점수를 사실로 단정하지 않음 |
| P2·선택 | `mobile/lib/features/events/presentation/event_detail_screen.dart` | 같은 이벤트 속성 표시·새로고침 연동 | 비동기 분석 완료 후 새 결과 확인 가능 |

`app/main.py`, 공용 Worker, Data의 DB 스키마, External의 기본 이벤트 전달 API는 이번 모델의 첫 연결을 위해 바꿀 필요가 없다. 새 backend는 기존 색상 분석기의 `clothing_colors`·영상 품질 지표를 생성하지 않으므로, 기존 결과를 사용하는 소비자가 있다면 backend별 해석을 확인한다. 두 분석기를 함께 실행하는 조합 플러그인은 현재 요청 범위에 추가하지 않는다.

## 7. 결과 저장 형태와 소비 방법

현재 PAR 구현의 완료 응답은 다음 구조다. 값은 **형식 설명용 가상 예시이며 실제 모델을 실행해 얻은 예측이 아니다.** `lease_id`는 플러그인이 아닌 공용 Worker가 추가한다.

```json
{
  "outcome": "complete",
  "metadata": {
    "schema_version": 1,
    "backend": "par_resnet34",
    "backend_version": "exp3",
    "image": {"width": 128, "height": 256},
    "attributes": {
      "gender": {"value": "Male", "score": 0.71},
      "age": {"value": "16-30", "score": 0.52},
      "hat": {"value": "No Hat", "score": 0.84},
      "upper_color": [{"value": "Black", "score": 0.8}],
      "lower_color": [{"value": "Blue", "score": 0.66}],
      "sleeve": {"value": "Long Sleeve", "score": 0.72},
      "lower_type": {"value": "Long Pants", "score": 0.83},
      "carrying": []
    }
  }
}
```

Data가 저장한 이벤트에서는 다음 경로로 읽는다.

```text
metadata.analysis.status
metadata.analysis.updated_at
metadata.analysis.result.backend
metadata.analysis.result.backend_version
metadata.analysis.result.attributes
```

Data는 최대 64 KiB의 유한 JSON `metadata`를 허용한다. 위 속성 결과는 이 계약 안에 담을 수 있다. External의 `_public_event()`는 미디어 내부 경로를 제거하고 나머지 metadata를 전달한다. 현재 Mobile의 `Event`도 metadata를 보관하므로, 결과 전달 자체에는 새 DB 컬럼이나 공개 API 필드가 필요하지 않다. 다만 속성별 검색·집계는 현재 구현에 포함되지 않으며 별도 요구사항이다.

화면의 기존 `Confidence`는 이벤트 탐지 신뢰도다. PAR 속성별 `score`와 다른 값이므로 한 필드에 섞지 않는다. 기존 backend의 결과, `pending/running`, `unconfigured/failed`도 함께 처리해야 한다.

## 8. 단계별 실행 및 검증 계획

### 단계 1 — 모델 인수 자료 확보와 기준 확정

담당: Analysis 모델 담당자 + 연동 담당자.

1. 이번 SHA-256을 제출자와 대조하여 적용 파일을 고정한다.
2. 학습 데이터·모델 사용 조건, 라벨 정의·순서, 평가 데이터 구성과 라벨별 precision/recall/F1을 확보한다.
3. 학습 때의 전처리 명세와 대표 크롭·기준 logit 또는 점수를 받는다. 밝기·가림·전신/부분 크롭·소지품이 없는 사례를 포함한다.
4. `val_macro_f1` 계산 방식, 클래스 불균형, 표시 임계값의 선정 근거를 확인한다.

검증: 제출본과 적용본을 해시로 식별하고, 기준 입력·출력과 사용 조건을 기록할 수 있어야 한다. 자료가 미비하면 합성 입력을 통한 연결 검증과 실제 품질 검증을 분리해 기록한다.

### 단계 2 — 기존 분석기 보완과 CPU 단독 확인

담당: Analysis 연동 담당자.

1. 5.1의 제한 로더·계약 검사를 구현한다.
2. 고정된 CPU 패키지 환경에서 모델을 실제로 읽고, 기존 네트워크에 strict 로딩한다.
3. 합성 이미지로 forward의 shape·유한 수치·결과 JSON 계약을 검사한다.
4. 기준 크롭으로 RGB·resize·정규화와 헤드별 후처리가 담당자 기준 출력과 일치하는지 비교한다. 수치 허용 오차는 실행 환경과 함께 합의·기록한다.
5. 동일 입력을 반복하여 eval 모드·점수·정렬 동작을 확인한다.

검증: 실모델 로딩 성공, 8개 헤드의 크기 일치, NaN/Inf 없는 출력, 기준 출력 비교 통과. 합성 입력 성공만으로 정확도 검증 완료라고 판정하지 않는다.

### 단계 3 — 임대·저장·API 경로 검증

담당: Analysis + Data 연동 담당자.

1. 검증 환경에서 사람 크롭이 있는 이벤트를 생성한다.
2. Analysis 토큰으로 작업을 가져와 완료하고, 같은 이벤트의 `metadata.analysis.result.backend=par_resnet34`, `backend_version=exp3`를 확인한다.
3. identity 작업과 완료 순서를 바꾸어도 양쪽 결과가 보존되고 전역 인물 ID가 Analysis에 의해 바뀌지 않는지 확인한다.
4. 잘못된 JPEG·bbox 불일치·최소 크기 미달은 실패, 추론 시간 초과는 재시도로 처리되는지 확인한다.
5. 완료 응답 유실·프로세스 재시작·만료 lease 시나리오가 기존 Worker 계약대로 동작하는지 확인한다.
6. 권한 있는 이벤트 API 응답에 속성이 포함되고 내부 크롭 경로는 노출되지 않는지 확인한다.

검증: 실제 이벤트 하나 이상의 Analysis 완료가 API까지 확인되어야 한다. `/health/ready`의 HTTP 200만으로 적용 성공을 판정하지 않는다.

### 단계 4 — 현재 설치에 제한 적용

담당: 배포 담당자.

1. 대상 Compose 프로젝트·env·모델 마운트가 이 설치와 같은지 Docker 접근 가능한 세션에서 확인한다. DB는 프로젝트의 일관된 백업 절차를 사용하고 설정·이전 이미지 식별자도 보관한다.
2. 검증한 모델을 실제 `MODELS_DIR`에 복사한다. 기존 `install_local_model()`의 임시 파일·해시 비교·교체 기능을 재사용할 수 있다. 복사 전후 SHA-256을 확인하고 컨테이너 사용자 읽기 권한을 검사한다.
3. 실제 `compose.env`에 1절의 `ANALYSIS_PLUGIN` 한 항목을 추가한다. 기본 파일명 사용 시 `PAR_MODEL_PATH` 추가는 필요 없다.
4. 검증된 Analysis 이미지를 빌드하고, Data·Nginx·Preprocessing이 정상인 상태에서 Analysis만 재생성한다. 재시작만으로 변경된 환경변수가 반영된다고 가정하지 않는다.
5. readiness 본문의 선택 플러그인과 `model_ready`, `last_error`, `last_outcome`을 확인하고 새 이벤트 완료까지 검증한다.
6. 단일 카메라에서 시작하여 실제 입력률, 작업 처리 p50/p95, CPU·메모리와 미처리 대기열 증가 여부를 측정한다.

현재 CPU 설정에 대한 명령 예시는 다음과 같다. **P0 검증과 대상 배포 확인 후 실행할 절차이며 이번 조사에서는 실행하지 않았다.** 다른 설치에서 GPU·push override를 사용 중이면 그 배포의 동일한 Compose 파일 집합을 사용한다.

```powershell
# 저장소 루트에서 실행. 실제 운영 설정은 저장소 server/.env가 아니다.
$deploymentEnv = 'C:\ProgramData\AI_CCTV\config\compose.env'
$composeFile = 'C:\Users\gksrn\Desktop\new_structure\server\compose.yml'

docker compose --env-file $deploymentEnv -f $composeFile --profile ai config --quiet
docker compose --env-file $deploymentEnv -f $composeFile --profile ai build analysis

# 기존 의존 서비스가 정상이고 이 설치의 프로젝트임을 확인한 뒤 적용한다.
docker compose --env-file $deploymentEnv -f $composeFile --profile ai up -d --no-deps --force-recreate --wait analysis
```

운영 도구의 `analysis-on`은 현재 Preprocessing도 `up --build`로 먼저 실행한다. 모델 연결 검증 중 다른 서비스의 재빌드까지 수행할지 의도적으로 선택해야 한다.

시간 제한은 우선 기존 기본값을 유지한다. 초기화 기본 30초·상한 120초, 모델 호출 기본 120초·상한 240초이며 Data lease는 5분이다. 실제 초기화가 제한을 넘는 경우에만 측정값을 근거로 조정한다. 작업은 한 Worker에서 직렬 처리하므로, 카메라 수보다 **유입되는 사람 이벤트 작업률과 평균 처리시간**을 기준으로 용량을 판단한다. 유입률을 λ, 처리시간을 t라 할 때 `λ×t<1`이 필요한 기본 조건이며 운영 여유를 추가해야 한다.

### 단계 5 — 반복 배포 지원과 선택적 화면 연동

담당: 설치·운영 도구 담당자, Desktop·Mobile 담당자.

1. PAR 전용 모델 입력과 기존 선택값 보존을 설치 도구에 추가한다. 모델을 선택하지 않은 설치의 기본 색상 분석기는 유지한다.
2. 기본 파일명 외의 경로가 필요할 때 Compose에 `PAR_MODEL_PATH` 전달을 추가하고, 배포 계약 변경 영향을 검증한다.
3. CI에 Analysis 테스트 실행 경로를 추가하고, 모델 파일은 Git에 커밋하지 않는다. 실모델 검증은 준비된 파일을 명시적으로 주입한다.
4. 사용자 표시가 요구되면 Desktop·Mobile 이벤트 상세에 Analysis 상태·속성·점수를 표시한다. 완료 후 이벤트 재조회가 필요하다.

검증: 신규 설치·기존 설치 갱신·설정 재생성·롤백을 모두 통과한 후 반복 배포 지원이 완료된 것으로 판단한다.

## 9. 배포 시 반드시 고려할 현행 동작

### 9.1 `PAR_MODEL_PATH`의 설정 계층

현재 Python 클래스는 이 변수를 지원하지만 `compose.env`에 쓰는 것만으로는 컨테이너에 전달되지 않는다. Compose의 `.env`/`--env-file`은 치환용 입력이며, 현재 Analysis `environment`가 이 변수를 참조하지 않는다. 비밀 파일에 임의로 넣는 것도 이 프로젝트의 Analysis 허용 키가 `DATA_ANALYSIS_TOKEN`뿐이어서 적합하지 않다.

첫 적용은 기존 기본 경로를 사용한다. 경로 선택이 제품 요구사항이 되면 후속 Compose의 Analysis environment에 `PAR_MODEL_PATH: ${PAR_MODEL_PATH:-/models/par_serving_exp3.pt}`를 추가하는 방안을 검증한다.

### 9.2 Compose 변경과 계약 서명

`server/deployment/signature.py`는 `compose.yml` 전체 내용의 해시를 배포 계약에 포함한다. CLI `_inspect_or_reset_installation()`, 설치 GUI, 서버 제어판에는 계약 불일치를 만나면 `reset_incompatible_deployment()`를 호출하는 경로가 실제로 존재한다. 관련 테스트도 저장 데이터 초기화를 기대한다.

따라서 PAR 경로 한 줄을 추가하는 변경도 설치 검사에서 계약 불일치가 될 수 있다. 운영 DB·설정을 보존해야 하는 기존 설치에서는 해당 자동 초기화 경로를 사용한 배포를 피하고, 호환 변경의 이관 방식과 서명 갱신을 검증 환경에서 먼저 확정해야 한다. 원인 확인 없이 서명 파일을 삭제·덮어써 통과시키지 않는다.

첫 적용에서 Compose 변경 없이 기존 플러그인 선택값을 사용하는 이유다. 다만 현재 작업 트리에는 이미 다른 Compose 변경이 있으므로, 기존 설치 서명과 현재 코드가 일치한다고 가정해서도 안 된다. 모델 작업 때문에 설치 도우미를 다시 실행하는 방식은 권하지 않는다.

### 9.3 기존 이벤트 재분석

- 기존 `unconfigured` 작업은 재시작만으로 돌아오지 않는다. 크롭이 남아 있는지 확인한 뒤 Analysis 토큰으로 내부 `POST /internal/data/v1/object-jobs/analysis/requeue-unconfigured`를 호출한다.
- 이 API는 요청당 최대 100건을 다시 대기시킨다. 처리량과 남은 크롭을 확인하며 반복한다.
- `LocalAppearanceAnalyzer`로 이미 `complete`된 작업은 이 API의 대상이 아니다. 모델 변경만으로 과거 이벤트가 PAR 결과로 바뀌지 않는다.
- 이미 완료된 과거 작업을 재분석하려면 결과 보존·대상·모델 버전 정책을 별도로 설계해야 한다. 이번 첫 적용에 직접 SQL 일괄 수정이나 모든 이벤트 재처리를 포함하지 않는다.

### 9.4 상태 확인과 롤백

현재 health 응답의 `backend`는 선택된 플러그인 참조이며, 이벤트 결과의 `backend=par_resnet34`와 다르다. `PersonAttributeAnalyzer`는 `runtime_metadata()`를 제공하지 않아 장치 진단 필드는 비어 있을 수 있다. 이 값을 GPU 사용 여부의 증거로 해석하지 않는다.

초기화 실패·지속 재시도·대기열 증가·기준 출력 불일치가 발생하면 이전 Analysis 이미지와 플러그인 선택값을 복원하고 Analysis를 재생성한다. 종전 기본값은 `server.services.analysis.processors:LocalAppearanceAnalyzer`다. 모델 파일만 복원해도 이미 메모리에 적재된 네트워크는 바뀌지 않으므로 재생성이 필요하다.

롤백은 앞으로 처리할 작업의 backend를 바꾼다. 이미 저장된 PAR 결과는 자동으로 취소되지 않는다. 장애 중 임대된 작업은 만료·재시도 정책을 확인하고, 진단을 위해 기존 모델과 해시 기록을 보관한다.

## 10. 인수 기준과 남은 확인사항

### 기술적 연결 완료 기준

- 원본·배치본 모델의 SHA-256 일치.
- 제한 로더와 실모델 strict 로딩 성공, 8개 헤드 출력·전처리·표시 규칙 검증 통과.
- 실제 선택된 플러그인이 `processors.attributes:PersonAttributeAnalyzer`임을 확인.
- 새 사람 이벤트가 `analysis.status=complete`가 되고 `result.backend=par_resnet34`, `backend_version=exp3`가 저장됨.
- External 이벤트 조회로 동일 결과 확인, identity 결과·기존 미디어 권한 유지.
- 오류 입력·시간 초과·프로세스 재생성·완료 전송 재시도 검증 통과.
- 목표 이벤트 입력률에서 대기열이 계속 증가하지 않으며 지연시간·메모리 측정값 기록.
- 이전 이미지·설정으로의 롤백 확인.

### 현장 사용 전 추가 확인

- 라벨별 성능과 낮은 품질 크롭에서의 동작, 학습·운영 전처리 일치.
- 화면에 표시할 속성과 낮은 점수 처리, 강제 top-1 표시의 의미.
- 과거 완료 이벤트를 재분석할 필요가 있는지와 결과 이력 보존 방식.
- 운영 목표 카메라 수·이벤트 유입률·허용 지연시간. 수치가 정해지기 전에는 운영 성능 합격을 확정할 수 없다.

**현재 판정:** 제출본은 기존 PAR 플러그인과 정적으로 호환되며 적용 위치도 확정할 수 있다. 운영 활성화와 품질 인수는 CPU 실추론·Data 연동·실제 입력 부하 검증 이후 단계다.

## 부록. 근거가 되는 소스 위치

| 경로 | 확인 내용 |
|---|---|
| `server/services/analysis/processors/attributes.py:23,31,48,72` | 번들 형식·기본 경로, 네트워크, 로더, 분석기 |
| `server/services/analysis/app/main.py:22` | Analysis Worker와 플러그인 선택 |
| `server/services/analysis/processors/__init__.py` | 기본 색상 분석기 export와 미설정 플러그인 |
| `server/services/analysis/Dockerfile:9` | CPU torch/torchvision 고정 설치 |
| `server/compose.yml:337` | Analysis 빌드·환경변수·모델과 스냅샷 읽기 전용 마운트 |
| `lib/ai_cctv_core/processing/plugins.py` | `module:factory` 로딩과 `process()` 프로토콜 |
| `lib/ai_cctv_core/processing/images.py` | JPEG·파일 크기·픽셀 수·bbox 일치 검사 |
| `lib/ai_cctv_core/processing/isolation.py`, `worker.py`, `runtime.py` | spawn 격리, 임대·완료·재시도, 시간 제한·상태 |
| `lib/ai_cctv_core/contracts/objects.py` | 관측·완료 결과·metadata 크기 계약 |
| `server/services/data/app/database/repositories/objects.py:105,118,164` | 두 단계 작업 생성, 5분 lease, 단계별 결과 병합 |
| `server/services/data/app/api/objects.py`, `security.py` | 객체 작업 API와 Analysis 토큰 권한 |
| `server/services/external/app/api/events.py:51` | 내부 이미지 경로 제거와 공개 이벤트 metadata 전달 |
| `server/installer/models.py:137` | 해시 검증을 포함하는 모델 설치 함수 |
| `server/installer/configuration.py:51,573` | 설치 입력과 Compose 설정 재생성 |
| `server/operations/compose.py:172`, `services.py:450` | 배포 파일 선택과 Analysis 활성화의 Preprocessing 선행 동작 |
| `server/deployment/signature.py:149,308`, `server/cli.py:135` | Compose 계약 해시와 불일치 초기화 경로 |
| `server/desktop/admin/application.py:1132` | 현재 Desktop 이벤트 상세 |
| `mobile/lib/features/events/domain/event.dart`, `presentation/event_detail_screen.dart` | Mobile metadata 보관과 현재 상세 표시 |
| `.github/workflows/ci.yml:28` | 현재 서버 테스트 실행 대상 |

부록 경로는 저장소 기준이며 줄 번호는 조사 당시 현재 작업 트리 기준이다. 표에서 짧게 쓴 Data `security.py`는 `server/services/data/app/security.py`, Mobile `presentation/event_detail_screen.dart`는 `mobile/lib/features/events/presentation/event_detail_screen.dart`를 뜻한다.
