# 감지·식별 기동 및 통합 진단 복구 계획

작성일: 2026-10-29

목표: 감지·식별 켜기의 빌드 실패를 해결하고, 기존 인증키로 Data 연결을 복구한다. Windows 통합 진단이 중단 없이 실제 장애를 표시하도록 수정한 뒤 GPU 실행을 확인한다.

## 1. 확인한 원인과 작업 전제

| 확인 항목 | 사용자 세션에서 확인한 사실 |
| --- | --- |
| Docker 상태 | Engine은 실행 중이며 Data, External, MediaMTX, nginx는 healthy |
| preprocessing 상태 | 실행 중이지만 unhealthy, Data 준비 상태 false, 카메라 작업자 0개 |
| 최근 빌드 실패 | CPU용 `onnxruntime==1.20.2` 설치 실패. 해당 인덱스는 `1.20.1`을 제공하지만 `1.20.2`를 제공하지 않음 |
| 이전 이미지 | 실행 컨테이너에 `gpu_requested` 및 `camera_capture.py`가 없음 |
| 저장된 인증키 | Data와 preprocessing의 감지·식별 키가 모두 일치 |
| 실행 중인 인증키 | Data는 저장된 값과 일치하지만 preprocessing은 두 키 모두 불일치 |
| HTTP 오류 | nginx 경유와 Data 직접 요청 모두 감지 API가 `401 INVALID_INTERNAL_TOKEN`. 식별 claim도 서버 로그에서 401 확인 |
| 진단 중단 | Docker 출력을 CP949로 디코딩하다 UnicodeDecodeError 발생 → stdout이 None → `_compose_rows()`의 `.strip()`에서 AttributeError |
| 공통 오류 안내 | 서비스 실행 코드가 Docker stderr를 버리고 모든 비정상 종료를 같은 안내문으로 변환 |
| 현재 GPU 설정 | 설치된 `compose.env`는 `PREPROCESSING_GPU=false`, 감지·식별 장치는 auto |

실행 환경은 `C:\ProgramData\AI_CCTV\config\compose.env`를 기준으로 한다. 저장된 인증키를 정상 기준으로 삼고, 현재 컨테이너에 남은 이전 값을 새 컨테이너 생성으로 갱신한다.

## 2. 범위

- CPU ONNX Runtime 버전, Docker 명령 출력 처리, 진단 오류 처리, 배포 복구에 필요한 부분을 수정한다.
- 현재 저장된 설정과 인증키를 재사용한다. 설치 초기화, 인증키 재생성, DB·녹화 파일 삭제는 하지 않는다.
- 카메라 녹화 제어, 추적·관측 계약, 좌표 TTL의 변경은 포함하지 않는다.
- Docker 접근은 확인된 사용자 세션에서 수행한다. 일반 작업 환경에서 Docker를 찾지 못한 결과를 사용자 Docker의 장애로 판정하지 않는다.

## 3. 1단계: CPU 이미지 빌드 복구

대상:

- `server/services/preprocessing/Dockerfile`
- `server/compose.yml`
- `server/compose.gpu.yml` 확인

작업:

1. CPU 기본값과 기본 Compose의 `ONNX_RUNTIME_PACKAGE`를 `onnxruntime==1.20.1`로 맞춘다.
2. GPU override의 `onnxruntime-gpu==1.20.2`는 별도 배포 패키지로 취급한다. CPU와 GPU의 버전 문자열이 같아야 한다고 가정하지 않는다.
3. CPU/GPU 이미지마다 ONNX Runtime 배포 패키지가 하나만 설치되는 구성을 유지한다.
4. 기존 PyTorch 버전, CUDA cu126 경로 및 모델 파일은 이번 빌드 오류와 별도로 유지한다.

검증:

- 실제 Linux/Python 3.11 이미지에서 CPU 빌드를 완료한다.
- 빌드된 이미지에서 `onnxruntime` import, 버전 및 CPU provider를 확인한다.
- 설치 목록에 `onnxruntime`과 `onnxruntime-gpu`가 동시에 존재하지 않는지 확인한다.

완료 기준: preprocessing 이미지 빌드 성공 및 CPU ONNX Runtime import 성공.

## 4. 2단계: Windows 진단의 인코딩 오류 수정

대상:

- `server/operations/compose.py`: 사전조건 확인과 `ComposeAdapter.run()`
- `server/operations/doctor.py`: Docker info/version, Compose 구성·상태 출력 처리

작업:

1. 해당 Docker subprocess 호출에 `encoding="utf-8"`, `errors="replace"`를 명시한다.
2. stdout/stderr에 `.strip()`을 적용하기 전에 값과 타입을 확인한다.
3. Compose 상태 조회가 성공했지만 stdout이 None인 경우에는 출력 수집 오류를 진단 항목으로 남긴다. 이를 정상적인 빈 컨테이너 목록으로 표시하지 않는다.
4. 정상적인 빈 문자열, 빈 JSON 배열, 단일 JSON 객체, 줄별 JSON 출력의 기존 처리는 유지한다.
5. 한 진단 항목의 출력 오류가 전체 진단 결과 표시를 중단하지 않도록 한다.
6. GUI는 모듈을 이미 로드하므로 수정 후 서버 제어판을 다시 실행해 확인한다.

검증:

- 한글·Unicode 기호가 포함된 Docker 출력으로 기존 오류가 재발하지 않는지 확인한다.
- None 출력, 명령 실패, 잘못된 JSON을 각각 실패/경고 항목으로 표시하는지 확인한다.
- 사용자 Windows 세션의 통합 진단에서 결과 목록이 끝까지 표시되는지 확인한다.

완료 기준: UnicodeDecodeError 및 NoneType.strip 없이 실제 서비스 상태와 오류가 표시됨.

## 5. 3단계: 켜기 버튼의 실제 실패 원인 표시

대상:

- `server/operations/services.py`: `_run()` 및 서비스 작업 오류 전달
- `server/desktop/common/tasks.py`, 서버 제어판: 기존 오류 전달 흐름 확인

작업:

1. 빌드·기동 명령에서도 stderr를 버리지 않고 실패 원인을 회수한다.
2. 조회 명령의 stdout JSON과 stderr를 분리해 JSON 파싱을 깨뜨리지 않는다.
3. 긴 빌드 출력은 임시 파일 등에 수집하고, 실패 시 제한된 마지막 부분만 읽는다. 사용 후 임시 기록은 정리한다.
4. GUI에는 작업 대상, 종료 코드, 정제한 실패 요약을 표시한다. 패키지 설치 실패와 서비스 unhealthy를 Docker 미실행으로 안내하지 않는다.
5. 토큰·비밀번호·인증 포함 URL·전체 환경변수/Compose JSON은 오류 메시지에 넣지 않는다. 기존 마스킹 코드를 우선 재사용한다.
6. 현재 작업 제한 시간과 작업 프로세스 트리 정리 동작은 유지한다.

검증:

- 패키지 설치 실패 시 `No matching distribution`과 대상 패키지를 확인할 수 있어야 한다.
- 엔진 연결 실패, 명령 비정상 종료, timeout을 구분할 수 있어야 한다.
- 가짜 비밀번호·토큰·인증 URL을 넣은 오류 출력이 그대로 노출되지 않아야 한다.

완료 기준: 공통 안내문만 보이던 빌드 실패에서 실제 수정 가능한 원인을 확인할 수 있음.

## 6. 4단계: 기존 설정으로 CPU 실행 복구

실행 전:

1. 현재 설치 env 경로, 컨테이너 생성 시각·이미지 ID, preprocessing 상태를 기록한다.
2. 저장된 감지·식별 키의 일치 여부를 재확인한다. 키 원문이나 해시는 출력하지 않는다.
3. 저장된 CPU/auto 장치 설정을 사용해 복구 결과를 먼저 분리 확인한다.

배포:

1. 설치된 env와 현재 저장소의 Compose 정의로 preprocessing 이미지를 빌드한다.
2. 빌드 성공 후 preprocessing 컨테이너만 재생성하여 저장된 env를 다시 주입한다. 단순 restart로 대체하지 않는다.
3. 필요하지 않은 Data·nginx·MediaMTX 등의 재생성이나 전체 서버 중단은 피한다.
4. Docker 명령이 실패하면 해당 단계에서 멈추고 새 오류를 확인한다. 초기화로 우회하지 않는다.

검증:

- 실행 중인 Data/preprocessing의 두 인증키가 서로 일치하고 각각 저장된 값과 일치함.
- preprocessing에서 감지 API 조회가 HTTP 200.
- 기존 식별 작업 루프 로그에서 반복적인 401이 사라짐. 검증을 위해 불필요한 작업 claim을 추가하지 않음.
- `data_ready=true`, Identity `ready=true`, `DATA_UNAVAILABLE` 해소.
- 실행 컨테이너에 새 캡처 코드와 `gpu_requested` 상태 필드가 존재함.
- 카메라가 활성화된 경우 worker_count와 카메라 작업자가 정상 생성됨. 카메라가 꺼져 있으면 작업자 0개만으로 실패 판정하지 않음.
- 실제 영상에서 감지 결과와 좌표 전달을 확인하고 새 timing 필드를 수집함.

완료 기준: 인증 오류 없이 최신 코드로 CPU 감지·식별 실행 가능.

## 7. 5단계: GPU 전환 및 실제 가속 확인

CPU 복구 기준을 충족한 후 진행한다.

1. 설치된 `compose.env`의 기존 값을 기록한 뒤 `PREPROCESSING_GPU=true`, `INFERENCE_DEVICE=cuda:0`, `IDENTITY_DEVICE=cpu`를 적용한다.
2. `compose.gpu.yml`이 실제 명령에 포함되고, CUDA cu126 PyTorch 및 GPU ONNX Runtime으로 빌드되는지 확인한다.
3. 초기에는 로컬 카메라 한 대와 YOLO만 GPU를 사용한다. OSNet은 CPU에 두어 4 GB GPU에서 부하를 분리한다.
4. 새 컨테이너에서 `torch.cuda.is_available()`, 장치 이름, CUDA 버전 및 실제 worker `execution_device=cuda:0`를 확인한다.
5. 자동 CPU 폴백을 GPU 성공으로 간주하지 않는다. 요청한 GPU가 준비되지 않으면 원인을 상태에 남긴다.
6. 준비 구간 이후 최근 100회 이상을 기준으로 모델·전체 추론·IPC·좌표 PUT p50/p95, 좌표 만료 및 실패 횟수를 CPU와 비교한다. 비교 시 모델·입력·카메라 조건을 맞춘다.

완료 기준: 실제 CUDA 추론, 인증 정상, 지속 실행, CPU 대비 처리 지연 개선. 성능 목표를 달성하지 못하면 timing 결과로 다음 병목을 특정한다.

## 8. 검증 범위와 복구 방법

코드 검증은 기존 `server/tests/test_operations_compose.py`를 우선 확장하고, 서비스 실행 오류 전달에는 필요한 최소 회귀 테스트를 추가한다. 전처리의 최신 프레임 캡처와 장치 선택 관련 기존 테스트도 실행한다. 이후 사용자 세션에서 통합 진단과 감지·식별 켜기를 실제 확인한다.

검증은 순서대로 수행한다: 코드 회귀 검증 → CPU 이미지 빌드 → CPU 실행/인증 복구 → GUI 진단·오류 표시 → GPU 전환 → 성능 비교.

GPU 단계만 실패하면 기록한 CPU 설정으로 되돌리고 검증된 CPU 이미지로 preprocessing을 재생성한다. 이전 인증키가 들어 있는 과거 컨테이너를 복구 대상으로 삼지 않는다. 저장된 인증키와 영속 데이터는 유지한다.

최종 보고에는 변경 파일, 빌드 결과, 인증 일치 여부, 실제 장치, CPU/GPU timing 비교, 남은 오류를 포함한다. 계획서 삭제는 별도 요청이 있을 때 수행한다.
