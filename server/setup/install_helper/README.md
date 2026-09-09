# 서버 설치 도우미

Windows에서 서버 설치와 최초 Edge 연결을 돕는 GUI·CLI다. 설정 생성과 공통 검증은 상위 `server/setup/`을 호출하며, 창을 닫아도 서버 컨테이너는 계속 동작한다. 설치 파일 사용자는 [설치 안내](../../../docs/guide.md#windows-설치)를 따른다.

| 위치 | 역할 |
|---|---|
| `server/setup/install_helper/` | 설치 화면·CLI, Docker 점검·시작·중지·통합 진단, 최초 Edge 검색·연결 |
| `server/setup/` | 저장 경로·관리자·모델·TLS·녹화 정책 검증, 운영 설정·인증 파일 생성 |
| `server/setup/validation.py` | 설치 도우미·소스 도구가 공유하는 배포 파일·인증 설정 검증 API |
| `server/setup/tools/` | 저장 폴더·비밀 파일·개발 인증서 생성, 객체 처리 설정 전환 |
| 서버 `/admin/` | 운영 중 Edge 등록·변경, 게시 계정 재발급, HD/FHD 변경, 장치·서버 상태 조회 |

서버가 꺼지면 웹 화면도 사용할 수 없으므로 시작·중지·장애 진단은 호스트 도우미에서 실행한다. 설치 완료 후에는 관리 화면의 **서버 관리자 화면 열기**로 공개 HTTPS 주소에 접속한다. LAN 검색·자동 Pairing은 관리 화면의 **카메라 연결**에서 진행한다.

## 처음 설치

마법사는 **1. 설치 준비 확인** → **2. 기본 설정** → **3. 설치 내용 확인** 순서로 안내한다. 첫 화면에서 Docker Desktop의 실행 여부·Linux 컨테이너 모드·Compose와 필요한 모델·TLS 파일을 확인한다. 누락되거나 잘못된 항목을 해결한 뒤 **다시 검사**를 누르고 통과하면 **다음**으로 진행한다. Docker Desktop 설치와 모델·인증서 준비는 사용자가 수행한다.

| 준비 항목 | 확인할 내용 |
|---|---|
| Docker Desktop·Compose | 설치되어 실행 중이며 Linux 컨테이너를 사용할 수 있어야 함 |
| 모델 | 사람 클래스가 `0`인 Ultralytics 호환 로컬 파일. 설치 프로그램에는 포함하지 않음 |
| TLS 인증서·개인키 | 사용할 HTTPS 이름의 인증서와 암호화되지 않은 PEM 개인키 |
| 관리자 비밀번호 | 기본 계정 `admin`의 비밀번호를 직접 입력. 12자 이상 |
| 휴대전화 접속 주소 | `https://cctv.example.com`처럼 실제 서버 주소와 필요 시 포트. 인증서의 서버 이름과 일치해야 함 |

모델·TLS 파일은 선택 저장소의 `models/`·`certs/tls.crt`·`certs/tls.key`, 서버 패키지의 `runtime/models/`·`runtime/certificates/`에서 후보를 찾는다. PC 전체를 검색하지 않으며 다른 위치의 파일은 직접 선택한다. 모델·인증서를 자동 다운로드하거나 자체 인증서를 만들어 신뢰 검사를 우회하지 않는다.

모델 사전 검사는 파일 형식·존재·크기를 확인하며 실제 추론 성공을 검사하지 않는다. TLS 사전 검사는 PEM 파일과 인증서·개인키의 일치를 확인한다. **인증서 만료일·접속 이름·휴대전화의 신뢰 여부는 별도로 확인한다.** 유효한 파일을 선택했더라도 실제 접속 주소나 단말의 신뢰 설정이 맞지 않으면 HTTPS 연결이 실패할 수 있다.

기본 설정 화면에서는 비밀번호와 비밀번호 확인을 입력하고 제안된 LAN IP 중 Edge·휴대폰이 접근할 서버 주소를 선택한다. 접속 주소 제안은 인증서·네트워크 환경에 맞는지 확인한 뒤 사용한다. 다음 값은 기본으로 준비되며 변경이 필요할 때만 **고급 설정 직접 지정**을 선택한다. 저장소는 화면 위 **변경…**으로 선택한다.

| 설정 | 기본값 |
|---|---|
| 저장소 | `C:\ProgramData\AI_CCTV` |
| 관리자 계정 | `admin` |
| HTTP·HTTPS·RTSP 포트 | `80`·`443`·`8554` |
| 녹화 조각·보관 기간 | `60초`·`7일` |
| 감지 장치 | `auto` |

세 번째 화면에서 내용을 확인하고 **설치 및 시작**을 누른다. 긴 작업은 백그라운드에서 수행하며 화면에 진행 단계·결과를 표시한다. 같은 작업의 중복 실행은 막는다. 실패하면 안내에 따라 Docker와 파일 상태를 확인한 뒤 재시도한다. 설정 생성이 끝난 뒤 서버 기동에서 실패했다면 관리 화면의 **서버 시작 / 업데이트 적용**으로 다시 시도하며 인증키를 새로 만들지 않는다. 완료 후 **서버 상태** 탭의 **상태 확인**과 **서버 관리자 화면 열기**로 확인한다.

## 기존 설치 관리

기본 저장소는 `C:\ProgramData\AI_CCTV`, 배포 env는 `config\compose.env`, 서버 설정은 `config\config.yaml`이다. GUI는 설치를 시작하거나 기존 설치를 불러올 때 저장 경로를 기억하며 다시 실행하면 기존 두 설정 파일을 읽어 관리 화면으로 연결한다. 다른 설치를 관리하려면 화면 위 **변경…**에서 해당 저장소를 선택한다. GUI의 서비스 명령은 선택한 저장소의 `config\compose.env`를 사용한다.

GUI의 초기 설정 생성은 신규 설치에서만 제공한다. 기존 배포의 서비스 토큰·JWT 키를 유지하며, **서버 시작 / 업데이트 적용**은 이미지 빌드·컨테이너 재생성으로 변경을 반영하고 **재시작**은 현재 컨테이너만 다시 실행한다. **중지**·**상태 확인**도 관리 화면에서 실행하며 상세 진단은 아래 CLI `doctor`를 사용한다. 저장 경로 기억은 관리자 비밀번호를 저장하는 기능이 아니다.

GUI는 서비스 작업 전에 같은 Compose 프로젝트의 현재 서버가 선택한 배포의 설정·DB 저장 위치를 사용하는지 확인한다. 다른 저장소의 서버가 있으면 변경을 거부한다. **변경…**에서 기존 서버의 올바른 저장소를 선택해 먼저 **중지**한 뒤 새 저장소로 돌아온다. 저장 위치를 확인할 수 없는 불완전한 컨테이너도 변경하지 않으므로 기존 설정과 Docker 상태를 확인한다.

**상태 확인**은 서비스별 실행 상태·준비 상태를 한국어로 요약한다. Docker 원문 출력은 GUI에 표시하지 않으며 작업 실패 때는 확인할 항목과 재시도 방법을 안내한다. 실제 영상·감지·녹화는 카메라 연결 후 별도로 확인한다.

| GUI 서비스 작업 | 제한 시간 |
|---|---|
| 서버 시작 / 업데이트 적용 | 전체 30분 |
| 상태 확인·재시작·중지 | 작업별 전체 2분 |
| 작업 안의 개별 구성·상태 조회 | 호출당 최대 30초, 위 전체 제한 안에서 실행 |

시간을 초과하면 도우미가 시작한 로컬 명령 프로세스를 정리한다. **이미 Docker에 반영된 작업은 자동 취소·롤백되지 않는다.** Docker Desktop과 **상태 확인**으로 현재 서버 상태를 확인한 뒤 같은 작업을 재시도한다. 설정과 인증키는 다시 생성하지 않는다.

설정·비밀 파일·DB 등 기존 운영용 폴더나 설치 중단 흔적이 있으면 새 설치로 덮어쓰지 않는다. 설정이 일부만 남았거나 읽을 수 없으면 표시된 진단에 따라 백업과 기존 파일을 확인한다. 자동 롤백은 제공하지 않으며 초기화 반복으로 복구하지 않는다. 새 설치가 목적이라면 기존 자료를 보존하고 별도의 빈 저장소를 선택한다.

위 배포 대조·제한 시간·상태 요약은 GUI 서비스 작업에 적용하며 기존 CLI 명령의 동작은 바꾸지 않는다. CLI `init`·`install`을 다시 실행하면 기존 파일의 백업을 남기지만 새 서비스 토큰·JWT 키가 만들어지므로 일상적인 시작·재시작이나 관리자 비밀번호 변경에 사용하지 않는다. 기존 관리자 계정은 DB에 남으며 초기 설정의 비밀번호를 다시 입력해도 그 계정의 비밀번호를 바꾸지 않는다.

### 카메라 연결

관리 화면의 **카메라 연결**에서 관리자 계정·비밀번호와 Pi에서 설정한 장치 연결 키를 입력한다. **Edge 찾기** → 장치 선택 → **선택한 Edge 연결** 순서로 진행하며 **연결 확인**으로 먼저 점검할 수도 있다. Pi 준비와 연결 실패 시 확인할 통신 경로는 [Edge 설치와 연결](../../../edge/README.md#설치와-연결)을 따른다.

서버·RTSP 주소, 직접 등록, 장치·카메라 ID, 관리·복구 주소, HD/FHD 화질, 백업·자격 증명 전달 파일 경로는 접힌 고급 설정에 있다. 서버 주소가 기존 설치와 다르거나 자동 검색 대신 직접 등록할 때만 필요한 값을 확인·변경한다. 작업 중에는 진행 상태를 표시하고 중복 실행을 막는다. 중앙 등록 후 Edge로 설정 전달만 실패했다면 안내된 전달 파일로 후속 설정을 완료하며 중앙에 같은 카메라를 다시 등록하지 않는다.

### CLI로 관리

설치본 CLI는 관리자 PowerShell에서 실행한다. PATH 추가를 선택하지 않았다면 설치 폴더에서 `.\AI_CCTV_CLI.exe`를 사용한다. 아래 경로는 기본 설치 기준이며 사용자 지정 배포라면 두 설정 경로를 모두 바꾼다.

```powershell
AI_CCTV_CLI.exe status --env-file 'C:\ProgramData\AI_CCTV\config\compose.env'
AI_CCTV_CLI.exe doctor 'C:\ProgramData\AI_CCTV\config\config.yaml' --env-file 'C:\ProgramData\AI_CCTV\config\compose.env'
```

| 명령 | 동작 |
|---|---|
| `preflight` | Docker 실행 여부·Compose·서버 패키지 사전 확인 |
| `start --env-file <경로>` | 설정을 확인하고 이미지 빌드·컨테이너 재생성·준비 상태 대기 |
| `stop --env-file <경로>` | 컨테이너·네트워크 종료 및 제거, 호스트 데이터 보존 |
| `restart --env-file <경로>` | 현재 컨테이너 재시작; 환경·이미지 변경은 `start`로 적용 |
| `status --env-file <경로>` | Compose 서비스 상태 조회 |
| `logs --env-file <경로>` | 최근 200줄부터 로그 추적; `Ctrl+C`로 조회 종료 |
| `doctor <config.yaml> --env-file <경로>` | 해당 설정·배포 파일·저장소·컨테이너 상태 진단 |
| `edge-register` | 최초 수동 Edge 등록과 게시 계정 파일 저장; [수동 연결](../../../edge/README.md#수동-연결) 참조 |

CLI가 기본 선택하는 env는 `AI_CCTV_COMPOSE_ENV_FILE` 환경변수, 설치 저장소의 `config\compose.env`, 소스의 `server/.env` 순서다. 설치 EXE는 설치 저장소의 경로를 사용하며, 소스 CLI도 설치 저장소에 env가 이미 있으면 이를 우선한다. **개발 배포는 서비스 명령에 `--env-file`을 명시한다.** 기본 저장소는 `AI_CCTV_DATA_ROOT`로 지정할 수 있다.

선택한 env와 같은 폴더의 `push.env`에서 FCM을 활성화하면 도우미는 시작·상태·로그·중지에 푸시 구성을 함께 적용한다. 파일 준비는 [모바일과 푸시](../../../docs/guide.md#모바일과-푸시), 백업·복원은 [운영과 백업](../../../docs/guide.md#운영과-백업)을 따른다.

## 개발 실행

Windows에서 Python 3.11·uv를 준비하고 저장소 루트의 PowerShell에서 실행한다.

```powershell
uv sync --project server/setup/install_helper --locked --extra test
uv run --project server/setup/install_helper --locked python -m server.setup.install_helper
```

GUI를 종료한 뒤 테스트와 정적 검사를 실행한다.

```powershell
uv run --project server/setup/install_helper --locked --extra test python -m pytest -c server/setup/install_helper/pyproject.toml server/setup/install_helper/tests server/setup/tests
uv run --project server/setup/install_helper --locked --extra test python -m ruff check --config tests/runner/ruff.toml server/setup
```

가상환경은 `server/setup/install_helper/.venv`다. 이 폴더의 `uv.lock`은 도우미와 로컬 `server/setup/`·`lib/` 의존성을 함께 고정한다. 상위 setup 패키지는 GUI 의존성 없이 설치할 수 있으며, GUI·CLI와 해당 개발 의존성은 하위 install_helper 프로젝트에서 관리한다.

소스 CLI는 설치 안내의 `AI_CCTV_CLI.exe`를 `uv run --project server/setup/install_helper --locked python -m server.setup.install_helper.cli`로 바꿔 실행한다. 전체 명령은 뒤에 `--help`를 붙여 확인한다. 다음은 개발용 `server/.env`의 상태만 조회하는 예시다.

```powershell
uv run --project server/setup/install_helper --locked python -m server.setup.install_helper.cli status --env-file server/.env
```

사용자 진단은 설치본 CLI의 `doctor`와 소스 모듈 `server.setup.install_helper.doctor`로 모은다. 둘 다 `server/setup/validation.py`의 공통 검증을 사용한다. 다음은 컨테이너 기동 전 배포 파일·인증·Compose 구성만 검사하는 명령이며 모델 파일과 실행 중 컨테이너 검사는 생략한다.

```powershell
python -m server.setup.install_helper.doctor --env-file server/.env --skip-runtime
```

이 직접 실행 모듈의 기본 env는 저장소의 `server/.env`다. 앞서 설명한 설치본·소스 CLI의 자동 env 선택과 다르므로 설치본을 검사할 때는 실제 `compose.env`를 `--env-file`로 지정한다. 전체 진단은 `--skip-runtime`을 빼고 실행하며 모델 파일 확인이 실제 추론 성공까지 보장하지는 않는다. GUI와 CLI는 진단 결과를 표시하고 공통 검증 모듈 자체에는 별도 사용자 명령을 두지 않는다.

## 설치 파일 빌드

Windows x64에서 Python 3.11·uv·Inno Setup 6를 준비하고 저장소 루트에서 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File .\server\setup\install_helper\packaging\build_windows_installer.ps1 -Version 0.3.0
```

Python이 PATH에 없거나 다른 버전이면 `-PythonExecutable 'C:\실제경로\python.exe'`, Inno Setup을 찾지 못하면 `-InnoCompiler 'C:\실제경로\ISCC.exe'`를 추가한다.

빌드는 잠금 파일에 따라 `build/windows-installer/.venv`에 환경을 만들고 테스트·실행 파일·설치 파일을 생성한다. GUI는 `AI_CCTV_Server_Install_Helper.exe`, CLI는 `AI_CCTV_CLI.exe`다. 설치 파일과 체크섬은 `dist/installer/`에 생성된다. 실제 Windows에서 설치·업데이트·제거를 확인하고 실행 파일 서명을 준비한다.
