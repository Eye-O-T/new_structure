# Windows 설치 프로그램 빌드 및 사용

## 목차

- [소비자 설치](#소비자-설치)
- [CLI 대체 경로](#cli-대체-경로)
- [업그레이드와 제거](#업그레이드와-제거)
- [설치 파일 빌드](#설치-파일-빌드)

## 소비자 설치

지원 대상은 Windows 10/11 x64다. 설치 파일을 실행하기 전에 Docker Desktop과
Docker Compose v2를 설치할 수 있지만, Docker가 아직 없어도 AI CCTV 설치 자체는
완료할 수 있다. 실제 서버를 시작할 때는 Docker Desktop이 실행 중이어야 한다.

1. `AI_CCTV_Server_Setup_<version>_x64.exe`를 실행한다.
2. 설치가 끝나면 **AI CCTV Configurator**를 연다. Program Files의 코드는 읽기
   전용으로 유지하고 ProgramData에 운영 설정을 쓰기 위해 Windows UAC 승격을 요청한다.
3. 신뢰할 수 있는 배포처에서 AI 모델(`.pt`, `.onnx`, `.engine`)을 별도로
   다운로드하고, Configurator의 **Downloaded AI model**에서 해당 로컬 파일을 선택한다.
   설치 프로그램은 모델을 포함하거나 자동 다운로드하지 않는다.
4. 운영 HTTPS 인증서와 암호화되지 않은 PEM 개인키를 준비하고 **TLS certificate**와
   **TLS private key**에서 두 로컬 파일을 함께 선택한다. 개인키는 화면이나 로그에
   출력되지 않고 제한된 DACL로 ProgramData에 복사된다.
5. 저장 경로, 관리자 계정, 공개 HTTPS Origin과 RTSP bind를 입력하고
   **Validate and create configuration**을 누른다.
6. Docker Desktop을 실행한 뒤 **Start services**를 누른다.
7. **Show service status**로 서비스 상태를 확인한다.

기본 데이터 위치는 다음과 같다.

```text
C:\ProgramData\AI_CCTV\
├── config\
├── secrets\
├── models\
├── database\
├── recordings\
├── recovered\
├── snapshots\
├── logs\
└── certs\
```

모델은 선택된 원본 경로에서 `models` 디렉터리로 원자적으로 복사된다. 이후 원본
다운로드 파일을 이동해도 실행 중인 서버에는 영향을 주지 않는다.

## CLI 대체 경로

GUI를 사용할 수 없으면 시작 메뉴의 **AI CCTV CLI Console**을 열거나 새 터미널에서
`AI_CCTV_CLI.exe`를 실행한다. 설치 중 PATH 추가를 해제했다면 전체 경로를 사용한다.
최초 `install`은 관리자 PowerShell에서 실행해야 하며, 조회·로그 명령에는 승격이 필요하지
않다. `install`은 사전 점검, 설정 생성과 서비스 시작을 순서대로 수행한다.

```powershell
AI_CCTV_CLI.exe preflight

AI_CCTV_CLI.exe install `
  --data-root 'C:\ProgramData\AI_CCTV' `
  --model 'D:\Downloads\person-model.pt' `
  --tls-certificate 'D:\Certificates\cctv.crt' `
  --tls-private-key 'D:\Certificates\cctv.key' `
  --admin-username admin `
  --public-base-url 'https://cctv.example.com'

AI_CCTV_CLI.exe doctor `
  'C:\ProgramData\AI_CCTV\config\config.yaml'

AI_CCTV_CLI.exe status
AI_CCTV_CLI.exe logs
AI_CCTV_CLI.exe stop
```

관리자 비밀번호는 `--admin-password`로 명령행에 노출하지 않으면 대화형 숨김
입력으로 받는다. 운영 환경에서는 이 방식을 사용한다.

기본 설치에서는 Bootstrap 카메라를 만들지 않는다. 중앙 서비스를 시작한 다음 GUI의
**Discover Edge on trusted LAN**으로 Pairing 중인 Edge를 선택하고 **Register Edge and
camera**를 사용한다. Configurator는 성공 시 일회성 게시 자격증명을 Edge에 자동 전달하고,
자동 전달 실패 또는 수동 등록에서는 보호된 Handoff 파일을 만든다. GUI를 사용할 수
없으면 CLI의 `edge-register`로 Edge와 카메라를 함께 등록한다. `install
--camera <id:name>`은 Edge 관리 Metadata 없이 기존 Bootstrap을 복원해야 할 때만 쓰는
고급 호환 옵션이다.

## 업그레이드와 제거

동일 `AppId`의 새 설치 파일을 실행하면 프로그램 코드와 Compose 정의만 교체된다.
`C:\ProgramData\AI_CCTV`의 설정, Secret, 모델, DB, 영상과 Snapshot은 덮어쓰거나
삭제하지 않는다. Configurator에서 초기 구성을 다시 생성하면 기존 주요 설정과
Secret에는 `.bak` 백업이 만들어진다.

제거 프로그램은 가능한 경우 먼저 `docker compose down`을 실행한다. 이는 Container와
전용 Network만 내리며 bind mount 데이터는 삭제하지 않는다. 제거 후에도 ProgramData의
운영 데이터는 그대로 남으므로 재설치에 사용할 수 있다. 데이터를 완전히 삭제하려면
서비스를 중지하고 백업을 확인한 다음 관리자가 ProgramData 디렉터리를 별도로 삭제해야
한다. 설치 프로그램은 이 삭제를 자동 수행하지 않는다.

기본값이 아닌 데이터 경로를 선택한 경우 제거 프로그램은 해당 경로를 자동으로 찾지
못하므로, 제거 전에 다음처럼 그 경로의 `compose.env`를 지정해 서비스를 먼저 중지한다.

```powershell
AI_CCTV_CLI.exe stop --env-file 'D:\AI_CCTV\config\compose.env'
```

현재 저장 영상은 디스크에서 별도로 암호화되지 않는다. 운영 장비에는 BitLocker 같은
OS·볼륨 수준 보호와 적절한 Windows 계정·백업 접근 제어를 적용한다.

## 설치 파일 빌드

빌드 장비에는 Python 3.11과 Inno Setup 6가 필요하다. 모델과 라이선스 파일은 빌드
입력에 포함되지 않는다.

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\configurator\packaging\build_windows_installer.ps1 `
  -Version 0.3.0
```

스크립트는 전용 빌드 가상환경을 만들고 Configurator 단위 테스트와 Ruff를 통과한 뒤
GUI/CLI 실행 파일 및 Inno Setup 설치 파일을 생성한다.

```text
dist\AI_CCTV_Configurator.exe
dist\AI_CCTV_CLI.exe
dist\installer\AI_CCTV_Server_Setup_0.3.0_x64.exe
dist\installer\AI_CCTV_Server_Setup_0.3.0_x64.exe.sha256
```

코드 서명은 이 단계의 빌드 스크립트에 포함되지 않는다. 외부 배포 전에는 설치 파일과
두 실행 파일에 조직의 인증서로 서명하고, 깨끗한 Windows VM에서 설치·업그레이드·제거
시험을 수행해야 한다.
