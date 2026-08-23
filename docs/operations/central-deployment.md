# 중앙 서버 Compose 배포

## 목차

- [1. 전제 조건](#1-전제-조건)
- [2. 설정 준비](#2-설정-준비)
- [3. Network bind 검증](#3-network-bind-검증)
- [4. 빌드와 시작](#4-빌드와-시작)
- [5. 정지와 재시작](#5-정지와-재시작)
- [6. Port 검증](#6-port-검증)

운영 설치에서는 Configurator GUI의 `Public HTTPS origin` 또는 CLI `init --public-base-url https://cctv.example.com`을 사용한다. 이 값은 HTTPS Origin으로 검증되어 Compose `.env`에 기록되므로 직접 파일을 편집할 필요가 없다. 아래 수동 절차의 빈 값은 로컬 개발에서만 허용한다.

이 문서는 현재 저장소에서 중앙 서버를 개발·통합 시험용으로 배포하는 절차를 설명한다. PyQt/CLI Configurator가 같은 초기 설정과 서비스 운영 기능을 제공하며, 아래 명령행 절차는 Headless 환경과 개발·장애 진단을 위한 대안이다. 패키징된 Windows Installer의 실제 환경 인수 시험은 P3 검증 항목으로 남아 있다.

## 1. 전제 조건

- Docker Engine 또는 Docker Desktop
- Docker Compose v2
- 초기 파일 생성을 위한 Python 3.11 이상
- 개발 인증서 생성을 위한 OpenSSL

외부 공개 전에는 DNS, 신뢰할 수 있는 TLS 인증서, OS 방화벽과 Router 설정을 별도로 완료한다.

## 2. 설정 준비

저장소 루트에서 실행한다.

```bash
cp server/.env.example server/.env
python server/scripts/init_runtime.py
python server/scripts/generate_secrets.py \
  --camera-id cam-001 \
  --camera-id cam-002
python server/scripts/generate_dev_cert.py
python server/scripts/doctor.py
```

Windows PowerShell에서는 다음 명령을 사용한다.

```powershell
Copy-Item server/.env.example server/.env
py -3.11 server/scripts/init_runtime.py
py -3.11 server/scripts/generate_secrets.py --camera-id cam-001 --camera-id cam-002
py -3.11 server/scripts/generate_dev_cert.py
py -3.11 server/scripts/doctor.py
```

`generate_secrets.py`는 Windows DACL 또는 POSIX `0600`으로 보호한 `data.env`, `external.env`, `inference.env`, `media.env`를 생성하며 기존 파일을 기본적으로 덮어쓰지 않는다. Data에는 서로 다른 `DATA_EXTERNAL_TOKEN`, `DATA_INFERENCE_TOKEN`, `DATA_MEDIA_TOKEN`, `DATA_RECOVERY_TOKEN`을 저장하고 각 호출 서비스에는 자신의 Token 하나만 주입한다. 또한 Inference 전용 `MEDIA_READ_USERNAME`/`MEDIA_READ_PASSWORD`를 한 번 생성하여 External과 Inference 파일에만 같은 값으로 저장한다. `doctor.py`는 필수 Key, Data와 호출자 간 일치, 네 Token의 상호 차이, reader 쌍의 일치와 32자 이상 password, 서비스별 allowlist를 검증한다. 단일 `SECRETS_FILE`/`internal-client.env` 배포는 더 이상 허용하지 않으며, 런타임의 `INTERNAL_SERVICE_TOKEN` fallback은 직접 개발·기존 테스트 호환용이다. `--camera-id`가 만드는 정적 게시 자격증명은 최초 Bootstrap 전용이며 DB에 저장된 운영 자격증명을 회전하지 않는다. 신규 운영 등록에는 Configurator의 `edge-register`를 사용하고, 일회성 게시 자격증명은 지정한 보호 파일을 통해 `ai-cctv-edge setup --publish-credentials-file <file>`로 전달한다.

## 3. Network bind 검증

`server/.env`의 다음 값을 배포 환경에 맞게 설정한다.

- `PUBLIC_BIND_ADDRESS`: 기본값은 `127.0.0.1`이며 외부 제공 시 방화벽 정책과 함께 변경
- `RTSP_BIND_ADDRESS`: 기본값 `127.0.0.1`. 원격 Edge 게시를 받아야 할 때만 중앙 서버의 신뢰 LAN IP를 명시
- `RECORDINGS_DIR`, `RECOVERED_DIR`, `DATABASE_DIR`, `SNAPSHOTS_DIR`, `MODELS_DIR`: 보존할 Host 내부 경로
- `CERTS_DIR`: `tls.crt`, `tls.key`가 있는 경로
- Linux의 `AI_CCTV_UID`, `AI_CCTV_GID`: Runtime Directory를 소유한 Host 사용자
- `PUBLIC_BASE_URL`: 운영 환경의 공개 HTTPS Origin. 설정하면 Live와 Playback API가 Absolute HTTPS URL을 반환
- `DATA_SECRETS_FILE`, `EXTERNAL_SECRETS_FILE`, `INFERENCE_SECRETS_FILE`, `MEDIA_SECRETS_FILE`: 서비스별 최소권한 Secret 파일. 네 경로는 서로 달라야 한다.

Profile 적용 시간 제한의 기본 연쇄는 Edge apply/rollback 최대 60초, External `EDGE_CONTROL_TIMEOUT_SECONDS=75`, Nginx 공개 API 85초, Configurator 90초다. Edge의 `apply_timeout_seconds`를 늘리면 모든 상위 제한도 같은 순서로 더 크게 조정하여 Client가 결과를 모른 채 재시도하지 않게 한다. 상태 Poll은 별도 `EDGE_STATUS_TIMEOUT_SECONDS=5`를 사용한다.

Configurator GUI/CLI도 RTSP bind를 `127.0.0.1`로 생성한다. 원격 Edge가 없다면 그대로 유지한다. 원격 Edge가 있다면 `0.0.0.0` 대신 Edge가 도달할 중앙 서버의 신뢰 LAN IP 하나를 지정하고 OS 방화벽에서 Edge 대역만 8554/TCP로 허용한다. RTSP 8554를 공인 IP 또는 Internet-facing NIC에 Bind하지 않는다. Data, External, Inference, MediaMTX의 8888/9996/9997과 Nginx의 내부 8080은 Host Port로 공개하지 않는다.

Inference의 RTSP URL에는 `MEDIA_READ_USERNAME`과 `MEDIA_READ_PASSWORD`가 percent-encoded userinfo로 런타임에만 결합된다. URL 전체나 password를 로그, 오류 출력, 운영 명령행에 기록하지 않는다. 두 값은 `external.env`와 `inference.env`에만 있어야 하며 Data/Media 파일에 복사하지 않는다.

## 4. 빌드와 시작

```bash
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml build
docker compose --env-file server/.env -f server/compose.yml up -d --wait --wait-timeout 120
docker compose --env-file server/.env -f server/compose.yml ps
```

최초 기동 후 관리자 비밀번호를 안전하게 입력한다. 비밀번호는 Command Line이나 파일에 저장되지 않고 External Container 안에서 Argon2id로 Hash된 뒤 Data Service에 전달된다.

```bash
python server/scripts/bootstrap_admin.py --username admin
```

상태와 설정을 확인한다.

```bash
docker compose --env-file server/.env -f server/compose.yml exec -T nginx nginx -t
curl -kfsS https://127.0.0.1/healthz
```

개발 인증서는 신뢰되지 않으므로 Smoke Test에만 `-k`를 사용한다. 실제 Client에는 인증서 검증을 비활성화한 설정을 배포하지 않는다.

## 5. 정지와 재시작

```bash
docker compose --env-file server/.env -f server/compose.yml down
docker compose --env-file server/.env -f server/compose.yml up -d --wait
```

`down`은 Bind-mounted Runtime Data를 보존한다. `down -v`나 `server/runtime` 삭제를 운영 절차에 사용하지 않는다.

## 6. Port 검증

다음 명령의 결과가 비어 있는지 확인하여 내부 서비스가 Host에 Publish되지 않았는지 검증한다.

```bash
! docker compose --env-file server/.env -f server/compose.yml port data 8000
! docker compose --env-file server/.env -f server/compose.yml port external 8000
! docker compose --env-file server/.env -f server/compose.yml port inference 8000
! docker compose --env-file server/.env -f server/compose.yml port mediamtx 8888
! docker compose --env-file server/.env -f server/compose.yml port mediamtx 9996
! docker compose --env-file server/.env -f server/compose.yml port mediamtx 9997
```

PowerShell에서는 각 `docker compose ... port` 명령의 출력이 비어 있는지 직접 확인한다.
