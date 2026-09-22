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

`PUBLIC_BASE_URL`은 `http://` 또는 `https://서버주소[:포트]`로 지정한다. HTTP는 암호화되지 않으므로 신뢰할 수 있는 LAN에서만 사용한다. `PUBLIC_BIND_ADDRESS`·`RTSP_BIND_ADDRESS`의 기본값 `127.0.0.1`을 원격 장치가 접근할 서버 IP로 바꾼다. HTTPS를 사용할 때는 실제 서버 이름과 일치하고 단말이 신뢰하는 인증서가 필요하다. 사용자 지정 포트는 주소에 직접 포함한다.

HTTPS는 기본 443, HTTP는 기본 80, Edge 송출은 신뢰 LAN의 RTSP 8554를 사용한다. 내부 서비스 포트는 외부에 공개하지 않는다.

```powershell
python -m server.setup.install_helper.doctor --env-file server/.env --skip-runtime
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --wait --remove-orphans
python server/services/external/tools/bootstrap_admin.py --username admin
```

마지막 명령은 새 관리자 비밀번호를 숨김 입력으로 받는다. 기동 후 로그인·영상·이벤트를 확인한다.

## Edge 연결

Pi 설치·Pairing은 [Edge README](../edge/README.md#설치와-연결), 수동 등록은 [수동 연결](../edge/README.md#수동-연결)을 따른다.

## 모바일과 푸시

앱 설치·로그인은 [모바일 README](../mobile/README.md)를 따른다. FCM을 사용할 경우 같은 Firebase 프로젝트의 Android 설정 파일과 서버 서비스 계정 JSON을 준비한다.

실제 배포 env 옆에 [push.env](../server/push.env.example)를 만들고 `PUSH_ENABLED=true`, `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE`을 설정한다. 서비스 계정 파일은 서버에만 둔다.

```powershell
docker compose --env-file C:/path/to/compose.env --env-file C:/path/to/push.env -f server/compose.yml -f server/compose.push.yml up -d --build --wait --remove-orphans
```

이후 관리 명령에도 같은 env·Compose 조합을 사용한다. 단말 알림 권한을 허용하고 새 이벤트의 푸시 수신을 확인한다.

## 개발과 검증

서비스 코드 변경 후에는 의존성 변경 여부에 따라 이미지를 재빌드하고, 설치 도우미의 `doctor --skip-runtime`으로 배포 파일·인증·Compose 설정을 점검한다. API 변경 시에는 `server/services/external/tools/export_openapi.py`로 OpenAPI 명세를 갱신한다.

배포 후 관리·업데이트·백업은 [운영 안내](operations.md)를 따른다.
