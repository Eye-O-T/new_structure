# AI_CCTV

다중 Raspberry Pi 카메라의 RTSP 영상을 중앙 서버에 수집하고, 객체 탐지·추적·이벤트 생성·영상 저장·검색·외부 조회를 제공하는 저비용 지능형 CCTV 프로젝트입니다.

> **구현 상태**
> 이 작업본은 `develop` 브랜치의 기존 프로토타입을 보존하면서 SRS와 Architecture의 기준 구조를 구현한 v0.3.0 소스 배포본입니다. Docker/Windows/Raspberry Pi 실환경 인수 시험이 필요한 항목은 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)에 구분했습니다.

## 핵심 목표

- Raspberry Pi를 단순한 카메라 입력 장치로 유지
- 중앙 MediaMTX에서 여러 RTSP 스트림 집약
- 영상 relay·저장과 AI 추론을 분리
- 영상 파일은 파일시스템, 검색 정보는 SQLite에 저장
- 외부 사용자는 JWT 로그인 후 실시간 영상·저장 영상·이벤트 조회
- 일반 사용자는 설치 프로그램과 GUI/CLI 설정만으로 배포
- 클라우드 의존 없이 로컬 우선으로 동작

## 현재 상태

| 영역 | 구현 결과 | 주요 위치 |
| --- | --- | --- |
| Raspberry Pi 카메라 입력 | GStreamer H.264 취득, 10초 MPEG-TS 백업, 자동 재시작 | `edge/` |
| RTSP | 중앙 publish 기본 모드, Camera ID별 인증, 1~4대 구성 | `edge/`, `server/mediamtx/` |
| 중앙 미디어 | MediaMTX v1.9.0 relay·HLS·60초 fMP4 녹화 | `server/mediamtx/` |
| 객체 탐지·추적 | YOLO + ByteTrack 독립 워커, 등장·이탈 이벤트 | `server/services/inference/` |
| 데이터 | SQLite WAL, migration, ACL, 검색·보관·백업·정합성 검사 | `server/services/data/` |
| 외부 조회 | JWT access/refresh/logout, 역할·카메라 ACL, REST API | `server/services/external/` |
| 보호된 영상 | Nginx `auth_request`를 거친 HLS·Playback | `server/nginx/` |
| 장애 복구 | Edge manifest/file API와 중앙 원자 수신·중복 방지 인덱싱 | `edge/`, Data recovery CLI |
| 중앙 배포 | Data·External·Inference·MediaMTX·Nginx 5개 컨테이너 | `server/compose.yml` |
| 설치 | PyQt/CLI Configurator, PyInstaller/Inno·ARM64 deb 빌드 정의 | `configurator/`, `edge/packaging/` |
| 기존 프로토타입 | 참고·회귀용 legacy 코드로 보존 | `client_code/`, `rtspv1.0/` |

## 기준 기술 스택

| 구분 | 기준 |
| --- | --- |
| Python | 3.11.9 고정, 기존 코드는 3.11.x 호환 |
| RTSP | RTSP/1.0. RTSP 2.0은 현재 범위에서 제외 |
| 영상 코덱 | H.264 |
| 엣지 파이프라인 | GStreamer 1.x |
| 중앙 미디어 서버 | MediaMTX. 현재 검증 기준 v1.9.0, Release에서는 시험한 image tag/digest 고정 |
| 사용자 영상 | HLS over HTTP/HTTPS |
| 추론 | Ultralytics YOLO 호환 모델 + 추적기, VLM 선택 |
| API | FastAPI |
| DB | SQLite 3, WAL mode |
| 인증 | JWT |
| Reverse Proxy | Nginx |
| 중앙 배포 | Docker Compose v2 |
| 서버 설치 UI | PyQt 기반 Configurator + PyInstaller 번들 |
| Windows Installer | Inno Setup 또는 동등 도구 |
| Edge 서비스 | systemd |

## 시스템 구성

```mermaid
flowchart LR
    E1["Raspberry Pi / Camera 1"]
    E2["Raspberry Pi / Camera 2"]
    EN["Raspberry Pi / Camera N"]

    MTX["MediaMTX\nRTSP ingest / relay / HLS / recording"]
    INF["Inference Service\nYOLO / Tracking / optional VLM"]
    DATA["Data Service\nSQLite / metadata / search"]
    EXT["External Service\nFastAPI / JWT / query API"]
    NGX["Nginx\nReverse Proxy / protected HLS"]
    FS["Persistent Storage\nrecordings / snapshots / models"]
    USER["Authenticated User"]

    E1 -->|RTSP/H.264| MTX
    E2 -->|RTSP/H.264| MTX
    EN -->|RTSP/H.264| MTX

    MTX -->|RTSP| INF
    MTX -->|recording segments| FS
    USER -->|HTTPS + JWT| NGX
    NGX -->|public /api + auth subrequest| EXT
    NGX -->|public /hls + /playback| MTX
    INF -->|internal event HTTP| NGX
    EXT -->|internal query / command HTTP| NGX
    NGX -->|internal /data routes| DATA
```

### 논리 서비스와 실제 컨테이너

애플리케이션 책임은 최대 4개로 구분합니다.

1. **Media Service**: MediaMTX, RTSP, HLS, recording
2. **Inference Service**: YOLO, tracking, optional VLM
3. **Data Service**: SQLite, 영상·이벤트 메타데이터, 검색
4. **External Service**: 로그인, JWT, 사용자 API

Nginx는 공통 인프라입니다. 따라서 기준 Docker Compose에는 애플리케이션 4개와 Nginx를 합쳐 총 5개 컨테이너가 존재할 수 있습니다.

Nginx는 외부용 `80/443` 진입점과 Docker Network 전용 내부 listener를 분리합니다. Inference와 External의 Data Service 요청은 내부 listener의 `/internal/data/` 경로를 통해 중계하며, 이 내부 listener는 Host에 publish하지 않습니다.

## 주요 데이터 흐름

### 실시간 영상

```text
Raspberry Pi
    -> RTSP/H.264
Central MediaMTX
    -> HLS
Nginx authentication boundary
    -> HTTPS
Authenticated user
```

### AI 이벤트

```text
MediaMTX RTSP
    -> Inference Service
YOLO / tracking / optional VLM
    -> event metadata + snapshot
Nginx internal route
    -> Data Service / SQLite
```

### 저장 영상 검색

```text
User query: camera + time range
    -> External Service
    -> Data Service
    -> matching recording segments
    -> protected MediaMTX Playback (fMP4/MP4)
```

실시간 사용자 영상은 HLS로 제공합니다. 저장 영상은 초기 릴리스에서 MediaMTX Playback의 fMP4/MP4 응답을 사용하고, 저장 영상까지 HLS VOD가 필요할 때 playlist 생성기나 제한된 FFmpeg 작업을 별도 확장으로 도입합니다.

### 네트워크 장애 복구

```text
Edge loses central connection
    -> local ring-buffer recording
Connection restored
    -> missing segments uploaded in order
Central server
    -> duplicate check
    -> persistent storage
    -> SQLite index
```

## 저장 원칙

영상 파일은 SQLite에 BLOB으로 넣지 않습니다.

```text
/data/
├── database/
│   └── ai_cctv.db
├── recordings/
│   └── cam-001/2026/08/22/08/
│       └── 20260822T080000.000Z_000001.mp4
├── snapshots/
├── models/
├── hls-cache/
└── logs/
```

SQLite는 다음 정보를 저장합니다.

- 사용자와 권한
- 카메라 ID와 MediaMTX 경로
- 영상 시작·종료 시각, 경로, 크기, 체크섬, 상태
- 이벤트 유형, 발생 시각, confidence, 추적 ID, 스냅샷
- 이벤트와 영상 구간의 연결 관계

모든 내부 시각은 UTC로 저장하고 UI에서 로컬 시간대로 표시합니다.

## 일반 사용자 설치

### 중앙 서버

Windows 빌드 절차로 생성하는 배포물:

```text
AI_CCTV_Server_Setup_<version>.exe
```

설치 흐름:

1. Installer 실행
2. Docker Engine과 Docker Compose 상태 검사
3. 데이터 저장 위치 선택
4. 기본 AI 모델 자동 설치 또는 사용자 모델 선택
5. 관리자 계정 생성
6. 포트와 외부 접속 여부 설정
7. 설정 검증
8. Docker image pull 및 Compose 시작
9. 서비스 상태와 접속 주소 표시

사용자는 기본 설치에서 다음 파일을 직접 수정하지 않습니다.

- `.env`
- `compose.yml`
- `mediamtx.yml`
- `nginx.conf`
- JWT secret
- SQLite 경로

### Raspberry Pi Edge

릴리스 배포물 예시:

```text
ai-cctv-edge_<version>_arm64.deb
```

설치 흐름:

```bash
sudo apt install ./ai-cctv-edge_<version>_arm64.deb
sudo ai-cctv-edge setup
```

설정 항목:

- 장치 ID와 카메라 ID
- 중앙 서버 주소
- MediaMTX RTSP publish path
- 해상도, FPS, 비트레이트
- 로컬 장애 백업 경로와 최대 보관량

설정 완료 후 systemd가 서비스를 자동 시작합니다.

```bash
sudo ai-cctv-edge status
sudo ai-cctv-edge doctor
sudo ai-cctv-edge logs
```

## 기존 프로토타입 실행 참고

아래 코드는 새 서비스 구조와 별도로 보존된 legacy 프로토타입입니다. 새 배포에는 [중앙 서버 실행](#중앙-서버-실행)을 사용하십시오.

### 중앙 PC

```powershell
py -3.11 -m venv venv311
.\venv311\Scripts\Activate.ps1
pip install -r requirements.txt
# PyTorch는 CPU/GPU 환경에 맞는 wheel을 별도로 설치
python client_code/main.py
```

### Raspberry Pi

```bash
cd rtspv1.0
sudo apt update
# rtspv1.0/readme.md에 기재된 GStreamer·libcamera 패키지 설치
python3 run_rtsp_server.py
```

`rtspv1.0/` 흐름은 Edge 측 MediaMTX를 사용하는 이전 방식이므로 참고·회귀 확인용으로만 유지합니다. 새 `edge/` 패키지는 중앙 publish를 기본값으로 사용합니다.

## 구현 저장소 구조

```text
AI_CCTV/
├── README.md
├── SRS.md
├── ARCHITECTURE.md
├── pyproject.toml
│
├── edge/
│   ├── src/
│   ├── packaging/
│   ├── systemd/
│   └── tests/
│
├── server/
│   ├── compose.yml
│   ├── .env.example
│   ├── mediamtx/
│   ├── nginx/
│   └── services/
│       ├── inference/
│       ├── data/
│       └── external/
│
├── configurator/
│   ├── config_core.py
│   ├── gui.py
│   ├── cli.py
│   └── packaging/
│
├── docs/
└── tests/
```

## 개발 환경 시작 예시

### 요구사항

- Git
- Docker Desktop 또는 Docker Engine
- Docker Compose v2
- 개발용 Python 3.11.9
- Raspberry Pi 테스트 시 Raspberry Pi OS 64-bit와 GStreamer 1.x

### 저장소 복제

```bash
git clone https://github.com/Eye-O-T/AI_CCTV.git
cd AI_CCTV
git switch develop
```

### 설정 생성

```bash
cp server/.env.example server/.env
cp server/config/config.example.yaml server/config/config.yaml
python server/scripts/init_runtime.py
python server/scripts/generate_secrets.py --camera-id cam-001
python server/scripts/generate_dev_cert.py
```

개발 환경에서는 Configurator 대신 예제 설정을 사용할 수 있습니다. 실제 secret은 Git에 커밋하지 않습니다.
`server/runtime/models/default.pt`에는 검증할 YOLO 모델을 배치합니다. 운영 설치에서는 Configurator가 선택한 모델을 원자적으로 복사하고 `.env`의 `MODEL_FILE`을 생성합니다.

Configurator는 `--model <custom.pt>`와 `--model-manifest <manifest.json>` 중 하나를 받습니다. Manifest 다운로드는 HTTPS·버전·라이선스·SHA-256·최대 크기를 검증합니다. 저장소의 example Manifest는 배포 모델의 URL·hash·license가 확정되기 전까지 의도적으로 활성화되지 않습니다.

### 중앙 서버 실행

```bash
docker compose -f server/compose.yml up -d --build
python server/scripts/bootstrap_admin.py
```

### 상태 확인

```bash
docker compose -f server/compose.yml ps
docker compose -f server/compose.yml logs --tail=200
```

### 종료

```bash
docker compose -f server/compose.yml down
```

`down`은 영속 볼륨의 DB와 영상 파일을 삭제해서는 안 됩니다. 개발자가 `-v`를 사용할 때는 데이터 삭제 가능성을 확인해야 합니다.

## 설정 파일

권장 서버 경로:

```text
C:\ProgramData\AI_CCTV\
├── config\config.yaml
├── secrets\secrets.env
├── models\
├── database\
├── recordings\
├── snapshots\
└── logs\
```

`config.yaml` 예시:

```yaml
schema_version: 1

server:
  public_http_port: 80
  public_https_port: 443
  rtsp_bind_address: 127.0.0.1
  rtsp_port: 8554
  timezone: Asia/Seoul

recording:
  root: /recordings
  recovery_root: /recovered
  segment_seconds: 60
  retention_days: 7
  warning_free_percent: 15
  encryption_at_rest: false

inference:
  enabled: true
  model_path: /models/default.pt
  device: auto
  confidence_threshold: 0.5
  analysis_fps: 5
  disappear_seconds: 3

cameras:
  - camera_id: cam-001
    name: Entrance
    stream_path: cam-001
    enabled: true
```

`secrets.env` 예시:

```dotenv
JWT_SECRET=<installer-generated-secret>
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD_HASH=<generated-argon2id-hash>
```

실제 비밀번호나 secret을 예제 파일에 넣지 않습니다.

## 카메라 등록

카메라 설정은 Data Service의 `cameras` 테이블과 Configurator/API에서 관리합니다. API로 카메라를 추가하면 publish credential을 응답에서 한 번 반환하고 Argon2 hash만 Data Service에 저장합니다.

예시:

```text
ID: cam-001
Name: Entrance
MediaMTX path: cam-001
Edge publish URL: rtsp://<central-server>:8554/cam-001
```

카메라 추가 시 다음을 검사합니다.

- `camera_id` 중복
- MediaMTX path 중복
- RTSP 연결 가능 여부
- 지원 코덱 여부
- 저장 경로 쓰기 가능 여부
- 추론 모델 준비 여부

## 사용자 API 개요

```text
POST   /api/v1/auth/login
POST   /api/v1/auth/refresh
GET    /api/v1/cameras
GET    /api/v1/cameras/{camera_id}/live
GET    /api/v1/recordings
GET    /api/v1/recordings/{recording_id}/playback
GET    /api/v1/events
GET    /api/v1/events/{event_id}
```

대표 검색 예시:

```text
GET /api/v1/recordings?camera_id=cam-001&from=2026-08-22T08:00:00Z&to=2026-08-22T09:00:00Z
```

```text
GET /api/v1/events?camera_id=cam-001&event_type=person_detected&from=2026-08-22T08:00:00Z
```

API 상세 계약은 OpenAPI 문서와 별도 API specification으로 관리합니다.

## 영상 제공 정책

- 실시간 영상은 MediaMTX가 HLS로 생성하고 Nginx가 인증 경계에서 전달합니다.
- 사용자는 JWT 인증 없이 HLS playlist 또는 segment에 접근할 수 없습니다.
- 저장 영상은 SQLite 검색 결과와 MediaMTX Playback을 연결하여 fMP4 또는 MP4로 제공합니다.
- 저장 영상 HLS VOD가 필수 요구사항으로 확정되면 playlist 생성기 또는 요청 구간 변환 작업을 별도 기능으로 추가합니다.
- FFmpeg는 현재 프로토타입과 기본 live 경로의 필수 CLI가 아닙니다. VOD HLS, 클립 추출, 썸네일 생성 또는 코덱 변환에 필요한 경우에만 제한적으로 포함합니다.

## 인증과 보안

- 외부 API와 HLS는 JWT 기반 인증을 요구합니다.
- 최소 역할은 `admin`, `viewer`입니다.
- 비밀번호는 평문 저장하지 않습니다.
- Nginx만 외부 HTTP/HTTPS 포트를 공개합니다.
- Data Service와 Inference Service는 Docker 내부 네트워크에서만 접근합니다.
- 외부 운영은 HTTPS를 전제로 합니다.
- Tailscale은 사용하지 않습니다.
- 저장 영상 암호화는 현재 범위에 포함되지 않습니다.
- 내부 RTSP는 신뢰된 LAN을 전제로 하므로 인터넷에 직접 노출하지 않습니다.

## 서비스 관리

서버 CLI:

```bash
ai-cctv-server start
ai-cctv-server stop
ai-cctv-server restart
ai-cctv-server status
ai-cctv-server doctor
ai-cctv-server logs
```

Edge 장애 구간은 별도 상시 컨테이너를 추가하지 않고 Data 컨테이너의 one-shot Coordinator로 복구합니다. Token은 명령행이 아닌 환경 또는 보호된 파일로 전달합니다.

```bash
EDGE_RECOVERY_TOKEN="$(< /secure/cam-001-recovery.token)" \
docker compose --env-file server/.env -f server/compose.yml exec -T \
  -e EDGE_RECOVERY_TOKEN data python -m app.recovery_coordinator \
  --edge-url http://192.0.2.41:8002 --camera-id cam-001 \
  --start 2026-08-22T08:00:00Z --end 2026-08-22T09:00:00Z
```

자세한 절차는 `docs/operations/`를 확인합니다.

`doctor` 출력 예시:

```text
[OK] Docker Engine
[OK] Docker Compose
[OK] MediaMTX
[OK] Data Service
[OK] SQLite storage
[OK] Inference model
[OK] Nginx
[OK] Camera cam-001
[WARN] Camera cam-002 disconnected
```

## 테스트

최소 테스트 범위:

```text
Unit
├── config schema and validation
├── filename normalization
├── recording overlap query
├── event validation
├── JWT issuance and expiry
└── retention policy

Integration
├── MediaMTX -> Inference
├── Media record hook -> Nginx internal -> Data index
├── Inference event -> Nginx internal -> Data Service
├── External Service -> Nginx internal -> Data Service
├── JWT -> protected HLS
└── Edge recovery upload

End-to-end
├── installer/configurator smoke test
├── 4-camera relay test
├── live HLS playback
├── recording search and playback
└── network disconnect and recovery
```

개발 기준 명령 예시:

```bash
pytest

docker compose -f server/compose.yml config
docker compose -f server/compose.yml build
```

## 로그와 장애 진단

모든 서비스 로그는 최소한 다음 필드를 포함합니다.

```text
timestamp_utc
service
severity
camera_id
operation
message
error_code
```

자주 확인할 항목:

- MediaMTX path publish 상태
- 카메라 코덱과 해상도
- 녹화 volume 쓰기 권한
- SQLite migration 상태
- 모델 파일 존재 여부와 체크섬
- Nginx upstream 상태
- JWT secret 존재 여부
- Edge와 중앙 서버 시각 동기화

## 데이터 보관과 삭제

- 보관 기간은 설정 가능해야 합니다.
- 기준 예시는 7일이며 운영 환경에 맞게 변경합니다.
- 삭제 순서는 파일 삭제와 DB 상태 변경이 불일치하지 않도록 처리합니다.
- 삭제 중 실패한 항목은 재시도 가능한 상태로 남깁니다.
- 컨테이너 삭제와 영상 데이터 삭제는 별개 동작이어야 합니다.

## 릴리스 상태

### v0.1 기반

- 기존 코드 정리
- Python 3.11.9 기준 통일
- 설정 외부화
- 테스트 기반 마련

### v0.2 기반

- 중앙 MediaMTX
- 멀티카메라
- 중앙 녹화
- SQLite 메타데이터
- 추론 서비스 분리

### v0.3 현재 구현

- JWT 로그인
- HLS
- 외부 조회 API
- Nginx
- 이벤트·저장 영상 검색

### v1.0 인수·배포 단계

- Windows Installer와 ARM64 `.deb`를 목표 운영체제에서 빌드
- 실제 Raspberry Pi 카메라와 1~4개 동시 스트림 인수 시험
- CPU/GPU별 모델 성능 측정과 배포 Manifest·라이선스 확정
- 신뢰 TLS 인증서와 방화벽을 적용한 운영 인수 시험

## 기여

기여 전 다음 원칙을 따릅니다.

1. 현재 동작하는 기능을 검증 없이 전면 재작성하지 않습니다.
2. 요청된 범위 밖의 리팩터링과 포맷 변경을 같은 PR에 포함하지 않습니다.
3. 서비스 경계와 API 변경은 `ARCHITECTURE.md` 또는 ADR에 먼저 기록합니다.
4. DB schema 변경에는 migration과 rollback 또는 복구 절차를 포함합니다.
5. 영상·계정·token·개인 IP가 포함된 테스트 데이터를 커밋하지 않습니다.

자세한 절차는 향후 `CONTRIBUTING.md`에 작성합니다.

## 라이선스

최종 오픈소스 배포 전 다음을 함께 검토하여 `LICENSE`를 확정해야 합니다.

- 프로젝트 코드
- PyQt 또는 대체 GUI toolkit
- Ultralytics 및 사용 모델 weight
- MediaMTX
- OpenCV·GStreamer·FFmpeg
- 설치 프로그램에 포함하거나 자동 다운로드하는 제3자 구성 요소

라이선스가 확정되기 전에는 README에 특정 라이선스를 단정적으로 표시하지 않습니다.

## 문서

- [SRS.md](SRS.md): 검증 가능한 기능·비기능 요구사항
- [ARCHITECTURE.md](ARCHITECTURE.md): 서비스 경계, 데이터 흐름, 저장·보안·배포 구조
