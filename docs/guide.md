# 설치·운영·개발 상세 안내

처음 사용하는 순서는 [프로젝트 안내](../README.md)를 따른다. 이 문서는 설치 설정, 소스 배포, 운영·백업, 개발·검증의 상세 절차를 다룬다. 구성 요소별 설명과 API 명세는 [문서 색인](README.md)에서 찾을 수 있다.

Preprocessing·Analysis의 현행 구현과 계약 코드는 각 [서비스 README](README.md#영구-문서-서버-구성-요소-개발)에서 확인한다. 명령은 별도 설명이 없으면 저장소 루트의 PowerShell에서 실행한다.

- [코드와 설정 위치](#코드와-설정-위치)
- [설치 선택](#설치-선택): [Windows 설치](#windows-설치), [소스 배포](#소스-배포)
- [Edge 연결](#edge-연결)
- [모바일과 푸시](#모바일과-푸시)
- [운영과 백업](#운영과-백업)
- [업그레이드](#업그레이드)
- [개발과 검증](#개발과-검증)
- [배포 조건](#배포-조건)
- [상세 설정 부록](#상세-설정-부록): 통신 경로·포트, 인증 토큰, 호스트 저장소

## 코드와 설정 위치

| 경로 | 역할 |
|---|---|
| `server/services/data/` | SQLite·이벤트·작업·복구 |
| `server/services/external/` | 인증·공개 API·푸시 |
| `server/services/preprocessing/` | 감지·추적·인물 연결 |
| `server/services/analysis/` | metadata 분석 |
| `server/services/mediamtx/` | 영상 수신·녹화·재생 |
| `server/services/nginx/` | HTTPS·중계 |
| `server/config/` | 설정 예시 |
| `server/secrets/` | 인증 설정 예시 |
| `server/setup/tools/` | 저장 폴더·인증키·개발 인증서 초기화, 객체 처리 설정 전환 |
| `server/setup/validation.py` | GUI·CLI가 공유하는 배포 파일·설정 검증 API |
| `server/services/data/tools/` | Data의 온라인 DB 백업 도구 |
| `server/services/data/app/database/migrations/` | Data의 SQL 스키마 이력; 기존 파일 대신 새 버전을 추가 |
| `server/services/external/tools/` | 첫 관리자 생성·공개 OpenAPI 생성 도구 |
| `server/compose.yml` | 운영 컨테이너 구성 |
| `server/compose.dev.yml` | 개발용 코드 연결 |
| `server/compose.test.yml` | 독립 테스트 환경 |
| `server/compose.push.yml` | Firebase 푸시 추가 설정 |
| `edge/` | Raspberry Pi 프로그램 |
| `mobile/` | Flutter 앱 |
| `server/setup/` | 서버 설치·설정 준비 공통 기능과 호스트 도구 |
| `server/setup/install_helper/` | Windows 설치 도우미·최초 Edge 연결·서버 기동·통합 진단 |
| `lib/` | Python 공통 패키지 ai_cctv_core |
| `tests/automated/` | 공통·통합 자동 테스트 |
| `tests/runner/` | 테스트 이미지·pytest·Ruff 설정 |
| `tests/mock_edge/` | MP4 모의 카메라 |
| `docs/` | 설치·운영·개발 안내, 구조 설명·교체 규약 2개·OpenAPI 문서 |

## 설치 선택

설치 EXE·Edge DEB·Android APK는 저장소에 포함하지 않는다. 배포 담당자에게 받은 파일을 사용하거나 [패키지 빌드](#패키지-빌드)와 [모바일 안내](../mobile/README.md)에 따라 만든다. Windows 설치 파일이 있으면 다음 절을, 소스를 실행하면 [소스 배포](#소스-배포)를 따른다.

아래 명령은 별도 표시가 없으면 **저장소 루트의 PowerShell**에서 실행한다. Windows 설치본에서 수동 Compose 명령을 쓸 때는 설치 폴더(기본 `C:\Program Files\AI_CCTV`)에서 실행한다. 예시 주소·경로는 실제 값으로 바꾼다.

### Windows 설치

Windows 10/11 x64와 Linux 컨테이너 모드의 Docker Desktop·Compose v2 이상이 필요하다. 설치 도우미는 설치 준비 확인 → 기본 설정 → 설치 내용 확인 순서로 안내한다. 기본값을 사용하면 필수 파일·관리자 비밀번호·접속 주소만 확인하고 **다음**으로 진행할 수 있다.

1. `AI_CCTV_Server_Setup_<version>_x64.exe` 설치 후 **AI CCTV 서버 설치 도우미**를 연다. 첫 화면에서 Docker·Compose와 모델·TLS 파일을 확인하고 누락되거나 잘못된 항목을 안내한다. 필요한 파일이 없으면 호환 모델, 신뢰 인증서와 암호화되지 않은 PEM 개인키를 선택한 뒤 **다시 검사**를 누른다.
2. 관리자 계정은 기본 `admin`이며 **12자 이상의 비밀번호를 직접 입력**한다. 제안된 LAN IP 중 Edge·휴대폰에서 접근할 주소를 선택하고 휴대전화 접속 주소를 확인한다. HTTPS 주소의 서버 이름은 인증서와 일치해야 한다.
3. 기본 저장소 `C:\ProgramData\AI_CCTV`, 포트 80·443·8554, 녹화 조각 60초·보관 7일, 감지 장치 `auto`를 사용한다. 다른 값이 필요하면 **고급 설정 직접 지정**을 선택한다. 저장소는 화면 위 **변경…**으로 선택한다.
4. 설정을 확인하고 **설치 및 시작**을 누른다. 설정 준비·서버 빌드와 기동은 백그라운드에서 수행하며 진행 단계와 문제 발생 시 확인할 항목을 안내한다. 안내에 따라 상태를 확인한 뒤 해당 작업을 재시도한다.
5. 설치가 끝나면 관리 화면에서 서버 상태를 확인하고 **서버 관리자 화면 열기**로 접속한다. 최초 Edge 검색·연결은 **카메라 연결**에서 진행한다.

도우미는 선택 저장소와 서버의 기본 모델·인증서 폴더에서만 기존 파일을 찾는다. Docker Desktop·모델·인증서를 대신 다운로드하거나 인증서 신뢰를 우회하지 않는다. 파일 확인 성공이 모델 추론 성공이나 휴대전화의 인증서 신뢰까지 보장하지는 않는다. 자세한 준비 항목은 [설치 도우미 안내](../server/setup/install_helper/README.md#처음-설치)를 참고한다.

도우미는 PATH에서 `docker` 명령을 찾는다. Docker가 설치되어도 인식되지 않으면 새 PowerShell에서 `docker --version`을 실행하고 Docker CLI 경로가 PATH에 있는지 확인한 뒤 도우미를 다시 실행한다.

설정 생성에는 관리자 권한이 필요하다. 배포 env는 저장소 아래 `config\compose.env`이며 코드와 별도로 DB·영상·모델·인증 파일을 보존한다. 도우미는 설치하거나 관리한 저장 경로를 기억하고 다시 실행할 때 기존 `config.yaml`·`compose.env`를 읽어 관리 화면으로 연결한다. **기존 설치에서는 설정·서비스 토큰·JWT 키를 다시 생성하지 않는다.** **서버 시작 / 업데이트 적용**은 이미지·설정을 반영하고, **재시작**은 현재 컨테이너를 다시 실행한다. 운영 중 관리는 [서버 관리자 화면](#서버-관리자-화면)을 사용하며 도우미는 종료해도 된다.

CLI `init`·`install`도 기존 설정과 불완전한 설치를 기본적으로 보호한다. 의도적인 인증키 재발급·재초기화에만 `--reset-existing`을 사용하며 기존 파일은 `.bak`으로 보존한다. 기존 배포의 일상적인 시작에는 `start --env-file <실제 경로>`, 단순 재시작에는 `restart --env-file <실제 경로>`를 사용한다.

GUI 대신 설치 후 새로 연 관리자 PowerShell에서 사용할 수 있다. 설치 때 PATH 추가를 선택하지 않았다면 설치 폴더에서 `AI_CCTV_CLI.exe`를 `.\AI_CCTV_CLI.exe`로 바꿔 실행한다. 아래 `192.0.2.10`은 서버 LAN IP의 예시다. 비밀번호는 숨김 입력으로 받는다.

```powershell
AI_CCTV_CLI.exe preflight
AI_CCTV_CLI.exe install --model 'D:\Models\person.pt' --identity-model 'D:\Models\osnet_x0_25_msmt17.onnx' --tls-certificate 'D:\TLS\tls.crt' --tls-private-key 'D:\TLS\tls.key' --public-base-url 'https://cctv.example.com' --public-bind 192.0.2.10 --rtsp-bind 192.0.2.10
AI_CCTV_CLI.exe status
```

### 소스 배포

초기 설정 스크립트용 Python 3.11, Docker/Compose v2 이상, 개발 인증서용 OpenSSL을 준비한다. 아래는 **기본 경로를 쓰는 새 개발 배포**용이다. 운영 설정을 예제로 덮어쓰지 않는다.

```powershell
Copy-Item server/.env.example server/.env
Copy-Item server/config/config.example.yaml server/config/config.yaml
python server/setup/tools/init_runtime.py
```

**설정 예시를 복사한 직후, 서버를 처음 실행하기 전에 카메라 등록 방식을 선택한다.** Pi를 `pair`로 연결하거나 관리자 API·웹에서 새로 등록할 배포라면 `server/config/config.yaml`의 예시 `cameras:` 목록 전체를 다음 한 줄로 바꾼다.

```yaml
cameras: []
```

이 경우 카메라 게시 계정은 실제 등록할 때 발급하므로 초기 인증 파일은 카메라 인자 없이 만든다.

```powershell
python server/setup/tools/generate_secrets.py
```

MP4 모의 카메라처럼 **설정으로 카메라를 미리 등록할 배포**라면 예시 `cam-001`을 실제 목록에 맞게 수정하고, 위 명령 대신 `python server/setup/tools/generate_secrets.py --camera-id cam-001`을 실행한다. 여러 카메라는 같은 명령에 `--camera-id`를 반복한다. 이 방식으로 이미 등록한 ID를 Pairing·웹·API에서 다시 생성하지 않는다.

카메라 등록 방식을 선택한 뒤 개발 인증서를 만든다.

```powershell
python server/setup/tools/generate_dev_cert.py
```

Linux에서는 `Copy-Item`을 `cp`, `python`을 Python 3.11의 `python3` 명령으로 바꾼다. `init_runtime.py`는 폴더만 만든다. 저장 위치를 바꾸면 각 초기화 스크립트의 `--help`로 출력 경로도 맞춘다.

포트·호스트 경로는 `server/.env`, 카메라·감지·보관 정책은 `server/config/config.yaml`, 서비스 간 인증값은 생성된 `server/secrets/*.env`에서 관리한다. `.env`의 상대 경로는 `server/` 기준이다. 운영 중 추가 등록은 [Edge 연결](#edge-연결)을 따른다.

감지 모델은 별도로 준비해 `MODELS_DIR/MODEL_FILE`(기본 `server/runtime/models/default.pt`)에 둔다. Compose가 `MODEL_FILE`로 컨테이너의 `MODEL_PATH`를 지정하므로 모델 파일명을 바꿀 때는 `server/.env`의 `MODEL_FILE`을 수정한다. `config.yaml`의 `inference.model_path`만 바꿔도 기본 Compose의 모델 경로는 바뀌지 않는다. 기본 구현은 사람 클래스 번호가 `0`인 Ultralytics 호환 모델을 사용한다. 모델 없이 영상·녹화 연결부터 시험하려면 `config.yaml`의 `inference.enabled`를 `false`로 설정한다. 이때 자동 사람 감지·박스 표시는 작동하지 않는다.

인물 식별에는 탐지 모델과 별도로 **OSNet x0.25 ONNX**가 필요하다.
[모델 준비 도구](../server/tools/README.md)의 의존성을 준비한 뒤 저장소 루트에서
`python server/tools/prepare_osnet.py`를 실행하면
`server/runtime/models/osnet_x0_25_msmt17.onnx`가 생성된다.
`MODELS_DIR`가 다르면 도구의 `--output`을 해당 폴더로 지정한다.
서비스는 모델을 자동 다운로드하지 않으며, 감지를 꺼도 인물 식별 작업자는 별도로 실행된다.

기존 HSV 배포를 전환할 때는 준비한 모델을 실제 `MODELS_DIR`에 넣고 기존 배포 env의
`IDENTITY_PLUGIN=server.services.preprocessing.processors.identity:OsNetIdentity`,
`IDENTITY_MODEL_PATH=/models/osnet_x0_25_msmt17.onnx`를 설정한다.
그 뒤 Data·Preprocessing 컨테이너를 재생성하여 새 설정을 적용한다.
이 작업에 `init`·`install` 재실행은 필요하지 않다. 기존 ID는 유지되며 완료된 관측을
자동으로 다시 판정하지 않는다. 매칭 기준과 한 장 관측의 제한은
[Preprocessing 안내](../server/services/preprocessing/README.md#기본-osnet-모델과-data의-인물-연결)를 따른다.

`server/.env`에서 다음을 확인한다.

| 설정 | 확인 사항 |
|---|---|
| `PUBLIC_BASE_URL` | 앱 접속 주소. `https://서버주소[:포트]` 형식이며 `/api/v1` 제외 |
| `PUBLIC_BIND_ADDRESS` | HTTPS 요청을 받을 서버 IP. 기본 `127.0.0.1`은 PC 내부 접속용 |
| `RTSP_BIND_ADDRESS` | 원격 Edge가 접근할 중앙 신뢰 LAN IP |
| `CONFIG_FILE`, `*_DIR` | 설정·DB·녹화·복구·스냅샷·모델·인증서 실제 경로 |
| `*_SECRETS_FILE` | 생성한 역할별 env 5개, 서로 다른 경로 |
| `AI_CCTV_UID/GID` | Linux 호스트 저장소 소유자와 일치 |
| `RECORDING_SEGMENT_SECONDS` | 10~300초, 기본 60초 |
| `IDENTITY_PLUGIN`, `IDENTITY_MODEL_PATH` | 기본 OSNet 구현체와 컨테이너의 `/models/...onnx` 경로 |
| `IDENTITY_MATCH_THRESHOLD`, `IDENTITY_MATCH_MARGIN` | Data 판정 기준 0.97·0.05. 실제 CCTV 자료로 교정 필요 |
| `COMPOSE_PROJECT_NAME` | 운영·업데이트 때 유지할 프로젝트 이름 |

위 개발 인증서는 `localhost`용이다. PC 안에서 시험할 때는 `PUBLIC_BASE_URL=https://localhost`로 맞추고 시험 클라이언트에 해당 인증서를 신뢰하도록 설정한다. 인증서 생성만으로 신뢰가 등록되지는 않는다. 휴대폰에서 `localhost`는 휴대폰 자신을 뜻하므로 실제 서버 주소와 단말이 신뢰하는 인증서가 필요하다. 운영 인증서는 `CERTS_DIR/tls.crt`(전체 인증서 체인), 개인키는 `tls.key`에 둔다.

`PUBLIC_HTTPS_PORT`를 기본 443에서 바꾸면 `PUBLIC_BASE_URL`과 앱·관리자 화면의 주소에도 같은 포트를 넣는다. 기본 Nginx의 HTTP 리다이렉트는 443을 사용하므로 사용자 지정 포트에서는 `https://서버주소:포트`로 직접 접속한다.

외부 접속에는 서버 주소로 연결되는 DNS·방화벽·필요시 공유기 포트 전달 설정도 필요하며 설치 도우미가 자동으로 구성하지 않는다. 8554는 신뢰 LAN에서만 사용하고 내부 8000·8080·8888·9996·9997은 외부에 공개하지 않는다.

```powershell
python -m server.setup.install_helper.doctor --env-file server/.env --skip-runtime
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --wait --remove-orphans
python server/services/external/tools/bootstrap_admin.py --username admin
```

위 `doctor --skip-runtime`은 `server/setup/validation.py`의 공통 배포 파일·인증 검증과 Compose 구성 검사를 실행하며 모델·실행 중 컨테이너 검사는 생략한다. `--skip-runtime` 없이 실행하면 호스트·모델 파일·컨테이너 상태도 진단하지만 실제 모델 로딩·추론 성공은 별도로 확인해야 한다. 이 모듈의 기본 env는 소스의 `server/.env`이며 설치본의 `compose.env`를 검사하려면 `--env-file`로 명시한다. `bootstrap_admin.py`는 12자 이상의 관리자 비밀번호를 숨김 입력으로 받는 소스 배포용 도구이며, 기존 계정의 비밀번호를 변경하는 명령이 아니다. 설치 도우미 설치에서는 설정 생성 시 관리자를 준비한다. 중앙 기동 후 [Edge](#edge-연결)와 [모바일](#모바일과-푸시)을 연결한다.

## Edge 연결

Raspberry Pi 설치·Pairing·업데이트는 [Edge 안내](../edge/README.md#설치와-연결)를 따른다. 실제 장비 없이 MP4로 시험하려면 [Mock Edge](../tests/mock_edge/README.md)를 사용한다.

자동 검색을 사용할 수 없다면 [토큰 내보내기·수동 등록](../edge/README.md#수동-연결)을 따른다. 관리·복구 주소는 중앙 컨테이너에서도 접근할 수 있어야 한다.

## 모바일과 푸시

앱 설치·로그인은 [모바일 README](../mobile/README.md), 대체 앱 개발은 [OpenAPI](openapi.yaml)를 따른다. 영상 확인에는 Firebase가 필요하지 않다.

푸시를 켜려면 같은 기존 Firebase 프로젝트의 두 파일을 준비한다.

- Android: `mobile/android/app/google-services.json`. 등록 package name과 applicationId가 같아야 한다.
- 서버: FCM 발송 권한의 서비스 계정 JSON. 저장소 밖에 보관하고 External이 읽게 한다. APK에는 넣지 않는다.

실제 `compose.env` 또는 `.env`와 같은 폴더에 **`push.env`**를 만든다. 다음 항목을 `키=값` 형식으로 한 줄씩 저장한다. [전체 예시](../server/push.env.example)의 프로젝트 ID와 파일 경로는 실제 값으로 바꾼다.

| 설정 키 | 예시 값 |
|---|---|
| `PUSH_ENABLED` | `true` |
| `FIREBASE_PROJECT_ID` | `your-existing-project-id` |
| `FIREBASE_SERVICE_ACCOUNT_FILE` | `'C:/ProgramData/AI_CCTV/secrets/firebase-service-account.json'` |

설치 도우미 관리 화면의 **서버 시작 / 업데이트 적용**으로 적용한다. 수동 실행은 기본 env 다음에 push env를 지정한다.

```powershell
docker compose --env-file C:/path/to/compose.env --env-file C:/path/to/push.env -f server/compose.yml -f server/compose.push.yml up -d --build --wait --remove-orphans
```

이후 상태·로그·중지에도 같은 추가 구성을 사용한다. Android 알림 권한을 허용하고 실제 이벤트 → 알림 → 상세 → 녹화를 확인한다. 기본은 모든 이벤트 수신이다. 키가 없으면 일반 앱 기능은 유지하되 실제 푸시는 검증할 수 없다.

## 운영과 백업

### 서버 관리자 화면

서버 기동 후 브라우저에서 `https://서버주소/admin/`에 접속하고 관리자 계정으로 로그인한다. Edge·카메라 등록, 등록 정보 수정, 게시 계정 재발급, 지원 HD/FHD 변경을 수행한다. 시스템 상태에는 External·Data·Preprocessing·MediaMTX, 감지·인물 연결 작업자·전송 대기열·복구 작업자·저장소·푸시 상태를 표시한다. Analysis 컨테이너의 모델 상태는 이 화면의 검사 범위에 포함하지 않는다. 전체 컨테이너 진단은 호스트의 `doctor`를 사용한다.

브라우저에서 Edge를 수동 등록할 때는 [수동 연결](../edge/README.md#수동-연결)의 토큰과 관리·복구 주소를 사용한다. 등록·게시 계정 재발급 후에는 JSON을 내려받아 해당 Edge의 `setup --publish-credentials-file`로 적용한다. LAN 자동 검색·최초 Pairing은 설치 도우미를 사용한다.

초기 `config.yaml`에 Edge 연결 정보를 넣어 배포했다면 운영 중 주소·장치 연결·토큰을 바꿀 때 초기 설정도 맞춘다. 설정의 장치 ID·관리/복구 URL과 `data.env`의 `EDGE_AUTH_TOKENS_JSON`이 모두 있는 항목은 Data가 재시작할 때 다시 적용한다. 자세한 조건은 [Data 초기화](../server/services/data/README.md)를 참고한다.

전체 서버의 시작·중지·재시작과 실행 불가 상태의 진단은 설치 도우미나 Docker Desktop에서 수행한다. 웹 화면에서 Docker를 직접 제어하지 않는다.

### 호스트 상태와 백업

아래 env는 실제 설치 경로로 바꾼다. FCM을 쓰면 위의 추가 env·Compose 파일도 포함한다.

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml ps
docker compose --env-file C:/path/to/compose.env -f server/compose.yml logs --tail 100
```

`healthy`와 Nginx `/healthz`만으로 전체 성공을 판단하지 않는다. 로그인·각 카메라 영상·새 이벤트·중앙/복구 녹화 재생을 확인한다. 블랙박스 `unconfigured`는 모델 미구현이며 [상태 의미](architecture.md#상태-확인)를 참고한다.

### 전체 백업

전체 백업은 Edge 로컬 백업을 확인하고 중앙 서비스를 정상 정지한 뒤 다음을 **같은 시점의 세트**로 복사한다.

| 대상 | 포함 경로 |
|---|---|
| 설정·인증 | 실제 env, `CONFIG_FILE`, 서비스별 비밀 파일 5개, 사용 중인 push.env·Firebase 계정, 설치 도우미 설치의 `release-manifest.json` |
| DB·미디어 | `DATABASE_DIR`, `RECORDINGS_DIR`, **별도의 `RECOVERED_DIR`**, `SNAPSHOTS_DIR` |
| 모델·TLS | `MODELS_DIR`, `CERTS_DIR` |
| 복원 기준 | 코드·이미지 버전, 모델·파일 해시, 실제 경로와 파일 권한 |

1. `docker compose --env-file C:/path/to/compose.env -f server/compose.yml down`
2. 실제 경로의 백업을 완료한다.
3. `docker compose --env-file C:/path/to/compose.env -f server/compose.yml up -d --wait`

호스트의 복구 영상은 중앙 녹화와 별도 폴더다. 실행 중 SQLite 파일을 개별 복사하지 않는다. DB만 온라인 백업하는 `python server/services/data/tools/backup_database.py`는 `server/.env` 배포용이며 결과는 `DATABASE_DIR/backups`다. 영상·설정은 포함하지 않는다. 이 도구는 `--server-dir` 아래 `.env`를 사용하므로 설치 도우미의 `config/compose.env`를 자동 선택하지 않는다.

### 백업 복원

복원할 때 서비스를 중지하고 현재 자료를 별도 보존한다. 같은 백업 세트의 코드·이미지·DB·미디어·설정·키를 복원하고 무결성·권한·실제 재생을 확인한다. **자동 DB downgrade는 없다.**

### 인증서 갱신

인증서 갱신은 새 `tls.crt`·`tls.key` 배치 → 동일 Compose 명령의 `exec -T nginx nginx -t` → `exec -T nginx nginx -s reload` 순서로 한다. 실제 단말 검증과 만료일을 확인한다.

### Edge 녹화 수동 복구

자동 복구가 놓친 구간은 Edge에 원본이 남아 있을 때 수동으로 가져온다. 아래 토큰은 [수동 연결](../edge/README.md#수동-연결)에서 내보낸 해당 Edge의 보호 파일이며, 명령에는 값 대신 환경변수 이름을 전달한다. 기간은 UTC로 한 번에 최대 24시간을 지정한다.

```powershell
$env:EDGE_RECOVERY_TOKEN = (Get-Content -Raw -LiteralPath 'C:\secure\edge-001-control.token').Trim()
try { docker compose --env-file C:/path/to/compose.env -f server/compose.yml exec -T -e EDGE_RECOVERY_TOKEN data python -m app.workers.recovery --edge-url http://192.0.2.41:8002 --camera-id cam-001 --start 2026-09-07T00:00:00Z --end 2026-09-07T01:00:00Z } finally { Remove-Item Env:EDGE_RECOVERY_TOKEN }
```

실제 배포의 추가 Compose 설정도 포함한다. 이 명령은 크기·해시를 검사해 중앙에 등록하며 Edge 원본을 삭제하지 않는다. 자세한 인자는 Data 내부의 `python -m app.workers.recovery --help`로 확인한다.

## 업그레이드

보관 정책·장애별 복구 절차와 버전별 실환경 인수 기록은 [운영 기준](operations.md)을 따른다. 이번 보관 정책은 오래된 이벤트·crop도 정리하므로 업그레이드 전에 보관 기간과 전체 백업을 확인한다.

먼저 [전체 백업](#전체-백업)을 완료하고 기존 프로젝트 이름·데이터 경로를 기록한다. Windows 배포는 담당자에게 받은 새 버전 설치 EXE와 체크섬을 확인한 뒤 기존 설치 폴더에 설치한다. 소스 배포는 새 버전의 소스를 확보하고 그 폴더에서 아래 작업을 진행한다. **서버 시작 / 업데이트 적용**과 Compose의 `--build`는 현재 폴더의 코드를 사용하며 새 소스 버전을 자동으로 내려받지 않는다.

Windows 설치 프로그램은 코드를 교체하지만 운영 설정을 자동 이관하지 않는다. 이전 inference·identity 구성에서 전환할 때만 별도로 준비한 **Python 3.11**로 다음을 실행한다. 이 도구와 필요한 표준 라이브러리 기반 모듈은 현재 Windows 설치 패키지에도 포함하므로, 소스 저장소 루트 또는 설치 폴더(기본 `C:\Program Files\AI_CCTV`)에서 실행할 수 있다.

```powershell
python server/setup/tools/enable_object_processing.py --server-dir server --env-file C:/path/to/compose.env
```

Windows 설치를 이관하면 `--server-dir`를 실제 설치의 `server` 절대 경로로, `--env-file`을 운영 중인 `compose.env`로 바꾼다. 기존 토큰을 보존하며 오래된 단일 `secrets.env`는 수동 이전이 필요하다. 중간 실패는 해결 후 재실행한다. 설정 초기화로 이관을 대신하지 않는다.

설치 도우미 관리 화면의 **서버 시작 / 업데이트 적용** 또는 `up -d --build --wait --remove-orphans`로 재생성한다. 단순 재시작은 새 환경·이미지를 반영하지 않는다. 같은 프로젝트의 구형 감지 컨테이너가 정리되고 6개만 실행되는지 확인한다. Data 시작 시 DB 마이그레이션이 적용되므로 실패 시 이전 이미지와 일관된 백업을 함께 복원한다.

Windows 제거는 기본 운영 데이터를 보존한다. 사용자 지정 경로라면 먼저 `AI_CCTV_CLI.exe stop --env-file <실제 경로>`로 중지한다.

## 개발과 검증

아래 명령은 소스 저장소에서 실행한다. Windows 설치본은 운영용이며 개발 Compose·테스트 코드·문서 생성기는 포함하지 않는다. 서버 개발·테스트 도구는 컨테이너에 설치한다.

| 위치 | 관리하는 환경 |
|---|---|
| `lib/pyproject.toml` | 공통 라이브러리; Python 서비스 이미지가 빌드할 때 설치 |
| `server/services/*/requirements.txt` | 해당 서비스 실행 패키지 |
| `server/services/*/requirements-dev.txt` | 해당 서비스의 개발·테스트 도구 추가 |
| `tests/runner/` | 서버 공통·통합 테스트 이미지, pytest·Ruff 설정 |
| `server/setup/install_helper/pyproject.toml`, `server/setup/install_helper/uv.lock` | 설치 도우미 개발·빌드와 설정 패키지 의존성 고정 |
| `server/setup/pyproject.toml` | GUI 의존성 없이 사용하는 설정 생성·공통 검증 패키지 |
| `edge/pyproject.toml`, `mobile/pubspec.yaml` | 각 장치 프로그램의 독립 개발 환경 |

### 서버 코드 개발

[소스 배포](#소스-배포)로 **별도 개발 배포**를 먼저 준비한다. 개발용 env의 프로젝트 이름·저장소·포트는 운영 배포와 다르게 지정한다. `compose.dev.yml`은 지정한 env의 데이터를 사용하므로 운영 env에 적용하지 않는다. 아래는 개발 전용 `server/.env`를 가정한다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml up -d --build
```

PC의 코드를 컨테이너에 읽기 전용으로 연결한다. PC에서 편집하면 해당 Python 서비스가 자동으로 재시작한다. 의존성을 변경하면 이미지를 다시 빌드한다. 운영 이미지는 `production`, 개발 이미지는 `development` 단계이며 태그도 분리한다.

서비스별 테스트는 해당 개발 컨테이너에서 실행한다. `data`를 `external`, `preprocessing`, `analysis`로 바꿔 사용할 수 있다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

### 서버 자동 테스트

설정 파일·Firebase·카메라 없이 다음 명령으로 공통·서비스별 테스트를 실행한다. 테스트용 DB·영상은 컨테이너의 임시 폴더에 만들며 운영 저장소를 연결하지 않는다. GUI·Edge 단독 테스트는 여기서 제외하고, Edge/설치 도우미와의 프로토콜 연동 검증은 포함한다.

```powershell
docker compose -f server/compose.test.yml build tests
docker compose -f server/compose.test.yml run --rm tests
docker compose -f server/compose.test.yml run --rm tests python -m ruff check --config tests/runner/ruff.toml --no-cache lib server tests edge
docker compose -f server/compose.test.yml run --rm tests python server/services/external/tools/export_openapi.py --check
```

실제 Data·External 프로세스 간 HTTP 인증·이벤트·분석 작업 완료·로그아웃도 별도로 검증한다.

```powershell
docker compose -f server/compose.test.yml --profile integration up --build --abort-on-container-exit --exit-code-from integration integration
docker compose -f server/compose.test.yml --profile integration down
```

`compose.test.yml`은 **단독 구성**이며 운영 Compose와 합치지 않는다. 외부 접속이 차단된 테스트 네트워크와 임시 저장소를 사용한다. 고정된 테스트 자격 증명은 이 내부 시험 전용이다. 이 검사는 Nginx·MediaMTX·실제 모델·단말 푸시까지 검증하지 않는다.

OpenAPI 갱신 시에만 호스트의 문서 폴더를 쓰기 가능하게 연결한다. PowerShell 예시이며 Linux는 `${PWD}` 대신 `$(pwd)`, `--user`에는 호스트 UID:GID를 사용한다.

```powershell
docker compose -f server/compose.test.yml run --rm --user 0:0 -v "${PWD}/docs:/workspace/docs" tests python server/services/external/tools/export_openapi.py
```

Windows GUI·CLI는 [설치 도우미 README](../server/setup/install_helper/README.md), Edge는 [Edge README](../edge/README.md), 모바일은 [모바일 README](../mobile/README.md), 모의 카메라는 [Mock Edge README](../tests/mock_edge/README.md)를 따른다.

실환경에서는 동시 영상·HD/FHD 변경·권한·녹화 복구·백업 복원·앱 푸시를 확인한다. 단위 테스트는 실제 카메라·영상·단말 검증을 대신하지 않는다. 기존 SQL 마이그레이션은 수정하지 않고 새 버전을 추가한다.

### 패키지 빌드

소스 저장소에서 [Windows 설치 파일](../server/setup/install_helper/README.md#설치-파일-빌드) 또는 [Edge 패키지](../edge/README.md#패키지-빌드)를 만든다. 결과와 체크섬은 각각 `dist/installer/`, `dist/edge/`에 생성된다. 배포 전 실제 장비에서 설치·업데이트·제거를 확인한다. Windows 실행 파일 서명은 별도 준비가 필요하다.

## 배포 조건

프로젝트 소스 코드는 [MIT License](../LICENSE)로 제공한다. PyQt, Ultralytics·모델 가중치, MediaMTX, OpenCV·GStreamer·FFmpeg 등 제3자 구성요소에는 각자의 라이선스와 배포 조건이 적용된다. 실제 영상·비밀키·개인 설정을 커밋하지 않는다. 저장 영상 암호화는 제공하지 않으므로 장비·디스크·백업 접근 권한을 관리한다.

## 상세 설정 부록

[시스템 구조](architecture.md)의 그림에 대응하는 운영 참조다. 설정값을 변경할 때는 다음 경로·인증·저장소 경계를 함께 확인한다. 개발 파일의 위치는 [코드와 설정 위치](#코드와-설정-위치), 실행 명령은 [개발과 검증](#개발과-검증)을 따른다.

### 통신 경로와 포트

| 호출 | 실제 경로·인증 |
|---|---|
| 외부 앱·관리자 브라우저 → API·영상 | Nginx 443(기본), JWT·역할·카메라 접근 권한 검사 |
| Edge → MediaMTX | RTSP 8554/TCP, 카메라별 게시 계정 |
| Preprocessing → MediaMTX | `rtsp://mediamtx:8554`, 전용 읽기 계정 |
| External·처리기·녹화 Hook → Data | `http://nginx:8080/internal/data/v1` → Data 8000, `X-Internal-Token` |
| External → MediaMTX 제어 | Nginx 8080의 `/internal/media` → MediaMTX 9997 |
| MediaMTX → 게시·읽기 인증 | External 8000에 직접 HTTP 요청 |
| External → Edge 상태·제어 | 기본 HTTP 8003, Edge Bearer 토큰 |
| Data → Edge 복구 파일 | 기본 HTTP 8002, Edge Bearer 토큰 |
| Data 내부 복구 작업 → Segment 등록 | 자기 컨테이너의 `127.0.0.1:8000/internal/v1` |
| Pairing 중인 Edge → 설치 도우미 | 신뢰 LAN의 UDP 37020, 연결 키로 서명한 광고 |

표의 `8000`·`8080`·`9997`은 컨테이너 내부 포트다. 내부 중계는 `/internal/data/v1/...`을 Data의 `/internal/v1/...`으로 치환하며 공개 HTTPS에서는 `/internal/`에 접근할 수 없다. Python 서비스의 `/health/live`·`/health/ready`도 내부 주소다. 외부에서는 `/admin/` 또는 관리자 인증이 필요한 `GET /api/v1/system/status`로 상태를 확인하며, 서비스별 의미는 [상태 확인](architecture.md#상태-확인)을 따른다.

여섯 컨테이너는 하나의 Docker bridge 네트워크를 공유한다. 네트워크 이름 `internal`은 외부 통신 차단 설정이 아니며 External의 Firebase 접속도 이 네트워크를 사용한다. 내부 HTTP·RTSP는 암호화하지 않으며 네트워크 자체가 Nginx 경유를 강제하지 않는다. 서버 호스트에는 80/443과 RTSP 8554만 연결한다. 소스 예시의 바인딩 기본값 `127.0.0.1`은 PC 내부 접속용이고, GUI에서 제안한 LAN IP를 선택하면 해당 IP로 설정한다. 외부 HTTPS·RTSP와 Edge 관리·복구 포트는 실제 배포 설정을 따른다.

### 인증 토큰과 계정

Data 내부 API는 External·감지·인물 연결·Analysis·Media·Recovery의 6개 역할 토큰을 구분한다. 각 호출은 해당 역할의 토큰을 `X-Internal-Token` 헤더로 전달하며, 같은 네트워크에 있다는 이유만으로 허용되지 않는다.

| 비밀 파일 | 포함·사용 범위 |
|---|---|
| `data.env` | Data가 검증할 전체 역할 토큰; Data 내부 Recovery 작업도 사용 |
| `preprocessing.env` | `DATA_INFERENCE_TOKEN`, `DATA_IDENTITY_TOKEN`, 영상 읽기 계정 |
| `analysis.env` | `DATA_ANALYSIS_TOKEN` |
| `external.env` | `DATA_EXTERNAL_TOKEN`, 사용자 인증 설정, 영상 읽기·게시 인증 설정 |
| `media.env` | 녹화 Hook의 `DATA_MEDIA_TOKEN` |

위 5개 파일은 `.env`·`compose.env`의 `*_SECRETS_FILE`로 서로 다른 경로를 지정한다. 같은 역할 토큰은 호출자와 Data에서 일치해야 한다. RTSP 읽기 계정은 External과 Preprocessing이 공유하며 사용자 JWT·카메라별 게시 계정과 별개다. Edge의 관리·복구 Bearer 토큰도 서버 내부 역할 토큰과 구분한다. 신규 생성은 [소스 배포](#소스-배포), 기존 토큰을 보존하는 설정 전환은 [업그레이드](#업그레이드)를 따른다.

### 호스트 저장소와 기록 처리

| 호스트 저장소 | 컨테이너의 접근 |
|---|---|
| `DATABASE_DIR` | Data만 읽기·쓰기 |
| `RECORDINGS_DIR` | MediaMTX·Data 읽기·쓰기 |
| `RECOVERED_DIR` | Data만 읽기·쓰기, 내부에서는 `/recordings/recovered` |
| `SNAPSHOTS_DIR` | Preprocessing·Data 읽기·쓰기, Analysis 읽기 전용 |
| `MODELS_DIR` | Preprocessing·Analysis 읽기 전용 |
| `CONFIG_FILE` | Data·External·Preprocessing 읽기 전용 |
| `CERTS_DIR` | Nginx 읽기 전용 |

SQLite는 Data만 직접 열고 다른 서비스는 내부 API를 사용한다. DB·영상·모델·설정은 호스트에 저장하여 컨테이너 재생성과 분리한다. Compose의 마운트 경로와 권한을 유지하고 [전체 백업](#전체-백업)에서 정한 세트를 함께 보존한다.

- MediaMTX 1.9.0은 기본 60초 중앙 녹화를 만들고 완료 Hook으로 Data에 등록한다. Hook 유실은 파일 정합성 점검으로 보완한다. 모델 장애 중에도 영상 송출과 녹화는 별도로 동작한다.
- 중앙 fMP4 재생은 Nginx → MediaMTX, 복구 MPEG-TS 재생은 공개 `/api/v1/recordings/{id}/content`의 Nginx → External → 내부 Nginx → Data 경로를 사용한다.
- Edge는 평소에도 기본 10초 MPEG-TS를 로컬에 저장한다. Data는 중앙 연결 복구 이벤트를 받아 파일 크기·SHA-256을 검증한 뒤 별도 복구 저장소에 등록한다. 자동 복구 작업은 관리자 `GET /api/v1/recovery-jobs`로 확인한다.
- 감지 이벤트를 받으면 Data가 이벤트·객체 작업·해당 푸시 대기열을 같은 DB 트랜잭션에 저장한다. Preprocessing은 Data 수신 전 이벤트를 스냅샷 저장소의 SQLite 송신 대기열에 보존하고 안정적인 이벤트 ID로 재전송 중복을 막는다.
- 객체 작업은 Preprocessing·Analysis가, 푸시 작업은 External이 Data에서 주기적으로 가져간다. 별도 메시지 브로커는 없다. 인물 연결·분석의 완료 순서는 보장되지 않으며 후속 metadata 갱신은 새 이벤트나 추가 푸시를 만들지 않는다.
- 시각은 UTC로 저장하며 녹화 검색은 요청 구간과 겹치는 Segment를 반환한다. 운영 카메라 정보는 Data DB가 기준이며, 초기 설정에서 전달한 Edge 연결 정보의 재적용 조건은 [서버 관리자 화면](#서버-관리자-화면)을 따른다.
