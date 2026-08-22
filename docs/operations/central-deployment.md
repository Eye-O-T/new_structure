# 중앙 서버 Compose 배포

이 문서는 현재 저장소에서 개발·통합 시험용 중앙 서버를 시작하는 절차다.
Windows Installer와 Configurator가 완성되기 전까지만 명령줄 절차를 사용한다.

## 1. 전제 조건

- Docker Engine 또는 Docker Desktop
- Docker Compose v2
- 초기 파일 생성용 Python 3.11 이상
- 개발 인증서 생성용 OpenSSL

외부 공개 전에 DNS, 신뢰 인증서, OS 방화벽과 Router 설정을 별도로 완료한다.

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

Windows PowerShell에서는 `cp` 대신 다음을 사용할 수 있다.

```powershell
Copy-Item server/.env.example server/.env
py -3.11 server/scripts/init_runtime.py
py -3.11 server/scripts/generate_secrets.py --camera-id cam-001 --camera-id cam-002
py -3.11 server/scripts/generate_dev_cert.py
py -3.11 server/scripts/doctor.py
```

`generate_secrets.py`는 기존 파일을 기본적으로 덮어쓰지 않는다. Camera
credential을 바꾸면 해당 Edge 설정도 함께 변경해야 한다.

## 3. Network bind 검토

`server/.env`에서 다음을 배포 환경에 맞게 바꾼다.

- `PUBLIC_BIND_ADDRESS`: 기본값은 `127.0.0.1`; 외부 제공 시 방화벽 적용 후 변경
- `RTSP_BIND_ADDRESS`: 중앙 서버의 신뢰 LAN 주소
- `RECORDINGS_DIR`, `RECOVERED_DIR`, `DATABASE_DIR`, `SNAPSHOTS_DIR`,
  `MODELS_DIR`: 보존할 Host 절대 경로
- `CERTS_DIR`: `tls.crt`, `tls.key`가 있는 경로
- Linux에서는 `AI_CCTV_UID`, `AI_CCTV_GID`: runtime directory를 만든 Host 사용자

RTSP 8554를 공인 IP 또는 인터넷-facing NIC에 bind하지 않는다. Data,
External, Inference, MediaMTX 8888/9996/9997, Nginx 8080은 Host port가 없다.

## 4. 빌드와 시작

```bash
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml build
docker compose --env-file server/.env -f server/compose.yml up -d --wait --wait-timeout 120
docker compose --env-file server/.env -f server/compose.yml ps
```

최초 기동 후 관리자 비밀번호를 안전하게 입력한다. 비밀번호는 command line이나
파일에 저장되지 않고 External container 안에서 Argon2id로 hash된 뒤 Data
Service에 전달된다.

```bash
python server/scripts/bootstrap_admin.py --username admin
```

상태와 설정을 확인한다.

```bash
docker compose --env-file server/.env -f server/compose.yml exec -T nginx nginx -t
curl -kfsS https://127.0.0.1/healthz
```

개발 인증서는 신뢰되지 않으므로 위 smoke test만 `-k`를 사용한다. 실제 client에
인증서 검증 비활성화를 배포하지 않는다.

## 5. 정지와 재시작

```bash
docker compose --env-file server/.env -f server/compose.yml down
docker compose --env-file server/.env -f server/compose.yml up -d --wait
```

`down`은 bind-mounted runtime data를 보존한다. `down -v` 또는
`server/runtime` 삭제는 이 절차에 사용하지 않는다.

## 6. Port 검증

다음 명령은 내부 서비스가 Host에 publish되지 않았음을 확인한다.

```bash
! docker compose --env-file server/.env -f server/compose.yml port data 8000
! docker compose --env-file server/.env -f server/compose.yml port external 8000
! docker compose --env-file server/.env -f server/compose.yml port inference 8000
! docker compose --env-file server/.env -f server/compose.yml port mediamtx 8888
! docker compose --env-file server/.env -f server/compose.yml port mediamtx 9996
! docker compose --env-file server/.env -f server/compose.yml port mediamtx 9997
```

PowerShell에서는 `docker compose ... port` 출력이 비어 있는지 확인한다.
