# 배포 안내

명령은 저장소 루트의 PowerShell에서 실행한다. 예시 주소·경로는 실제 값으로 바꾼다.

## Windows 설치

Windows 10/11 x64, Linux 컨테이너 모드의 Docker Desktop·Compose v2, 사람 감지 모델, OSNet ONNX, 서버 주소에 맞는 TLS 인증서·PEM 개인키를 준비한다.

1. 배포받은 설치 EXE를 실행하고 서버 설치 도우미를 연다.
2. 모델·인증서를 선택하고 관리자 비밀번호(12자 이상), 접속 주소, 저장소를 설정한다.
3. **설치 및 시작** 후 서버 상태를 확인한다.
4. **카메라 연결**에서 Edge를 등록하고 앱을 연결한다.

기본 저장소는 `C:\ProgramData\AI_CCTV`, 배포 env는 그 아래 `config\compose.env`다. 재실행 시 기존 설정을 사용한다. CLI·설치 파일 빌드는 [설치 도우미 README](../server/setup/install_helper/README.md)를 따른다.

## 소스 배포

Python 3.11, Docker·Compose v2, 개발 인증서 생성용 OpenSSL이 필요하다. 다음 초기화는 새 배포에 사용한다.

```powershell
Copy-Item server/.env.example server/.env
Copy-Item server/config/config.example.yaml server/config/config.yaml
python server/setup/tools/init_runtime.py
```

Pairing·관리자 화면에서 카메라를 등록할 경우 `config.yaml`의 예시 목록을 `cameras: []`로 바꾸고 인증값을 생성한다.

```powershell
python server/setup/tools/generate_secrets.py
python server/setup/tools/generate_dev_cert.py
```

설정 파일로 카메라를 미리 등록한다면 목록을 수정하고 `generate_secrets.py --camera-id cam-001`처럼 각 ID를 지정한다. 같은 ID를 Pairing으로 다시 등록하지 않는다.

| 설정 위치 | 필수 확인 |
|---|---|
| `server/.env` | 프로젝트 이름, 공개 주소·포트, 호스트 경로 |
| `server/config/config.yaml` | 카메라 목록, 감지 설정, 보관 기간 |
| `server/secrets/*.env` | Data·External·Preprocessing·Analysis·Media의 역할별 인증값 |
| `MODELS_DIR` | 감지 모델 `MODEL_FILE`(기본 `default.pt`), OSNet `osnet_x0_25_msmt17.onnx` |
| `CERTS_DIR` | `tls.crt` 인증서 체인과 `tls.key` 개인키 |

OSNet 준비는 [모델 도구](../server/tools/README.md)를 따른다. 서비스는 모델을 자동 다운로드하지 않는다. `.env`의 상대 경로는 `server/` 기준이며, Linux에서는 저장소 권한을 `AI_CCTV_UID/GID`와 맞춘다.

`PUBLIC_BASE_URL`은 `https://서버주소[:포트]`로 지정한다. `PUBLIC_BIND_ADDRESS`·`RTSP_BIND_ADDRESS`의 기본값 `127.0.0.1`을 원격 장치가 접근할 서버 IP로 바꾼다. 개발 인증서는 localhost용이므로 원격 앱에는 실제 서버 이름과 일치하고 단말이 신뢰하는 인증서가 필요하다. 사용자 지정 HTTPS 포트는 주소에 직접 포함한다.

HTTPS는 기본 443, Edge 송출은 신뢰 LAN의 RTSP 8554를 사용한다. 내부 서비스 포트는 외부에 공개하지 않는다.

```powershell
python -m server.setup.install_helper.doctor --env-file server/.env --skip-runtime
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --wait --remove-orphans
python server/services/external/tools/bootstrap_admin.py --username admin
```

마지막 명령은 새 관리자 비밀번호를 숨김 입력으로 받는다. 기동 후 로그인·영상·이벤트를 확인한다.

## Edge 연결

Pi 설치·Pairing은 [Edge README](../edge/README.md#설치와-연결), 수동 등록은 [수동 연결](../edge/README.md#수동-연결)을 따른다. 장비 없이 시험하려면 [Mock Edge](../tests/mock_edge/README.md)를 사용한다.

## 모바일과 푸시

앱 설치·로그인은 [모바일 README](../mobile/README.md)를 따른다. FCM을 사용할 경우 같은 Firebase 프로젝트의 Android 설정 파일과 서버 서비스 계정 JSON을 준비한다.

실제 배포 env 옆에 [push.env](../server/push.env.example)를 만들고 `PUSH_ENABLED=true`, `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE`을 설정한다. 서비스 계정 파일은 서버에만 둔다.

```powershell
docker compose --env-file C:/path/to/compose.env --env-file C:/path/to/push.env -f server/compose.yml -f server/compose.push.yml up -d --build --wait --remove-orphans
```

이후 관리 명령에도 같은 env·Compose 조합을 사용한다. 단말 알림 권한을 허용하고 새 이벤트의 푸시 수신을 확인한다.

## 개발과 검증

### 서버 코드 개발

프로젝트 이름·포트·DB·영상·인증 파일을 운영과 분리한 개발용 `server/.env`를 준비한다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml up -d --build
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

서비스 코드 변경은 자동 반영되며 의존성 변경은 재빌드한다. 다른 서비스는 위 명령의 `data`와 테스트 경로를 바꾼다.

### 서버 자동 테스트

`compose.test.yml`은 운영 Compose와 합치지 않는 독립 테스트 환경이다.

```powershell
docker compose -f server/compose.test.yml build tests
docker compose -f server/compose.test.yml run --rm tests
docker compose -f server/compose.test.yml run --rm tests python -m ruff check --config tests/runner/ruff.toml --no-cache lib server tests edge
docker compose -f server/compose.test.yml run --rm tests python server/services/external/tools/export_openapi.py --check
```

Data·External HTTP 통합 검증:

```powershell
docker compose -f server/compose.test.yml --profile integration up --build --abort-on-container-exit --exit-code-from integration integration
docker compose -f server/compose.test.yml --profile integration down
```

API 변경 후 명세 갱신:

```powershell
docker compose -f server/compose.test.yml run --rm --user 0:0 -v "${PWD}/docs:/workspace/docs" tests python server/services/external/tools/export_openapi.py
```

배포 후 관리·업데이트·백업은 [운영 안내](operations.md)를 따른다.
