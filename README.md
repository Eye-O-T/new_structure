# AI CCTV

Raspberry Pi 영상을 중앙에서 녹화·분석하고 Android 앱으로 확인한다. 중앙은 Docker 컨테이너 6개다. 감지는 YOLO/ByteTrack을 사용하며 **전역 인물 연결과 metadata 분석은 아직 블랙박스**다.

## 문서

| 문서 | 용도 |
|---|---|
| [architecture.md](docs/architecture.md) | 전체 구성, 통신·영상·이벤트 흐름, 저장소와 현재 구현 범위 |
| [SRS_interface_preprocessing.md](docs/SRS_interface_preprocessing.md) | Preprocessing 컨테이너 교체 규약: 영상·감지·추적·전역 인물 연결 |
| [SRS_interface_analysis.md](docs/SRS_interface_analysis.md) | Analysis 컨테이너 교체 규약: 객체 입력·metadata·작업 처리 |
| [openapi.yaml](docs/openapi.yaml) | 모바일 교체용 공개 API 명세와 인증·영상·알림 사용 규칙 |

서비스를 통째로 교체할 때는 HTTP·데이터·저장소·상태 규약을 유지한다. 기존 Python 플러그인은 참고 구현이며 다른 언어를 금지하지 않는다. 다른 중앙 컨테이너의 교체 규약은 관리하지 않는다.

```text
server/
├── services/
│   ├── data/             # SQLite·이벤트·작업·복구
│   ├── external/         # 인증·공개 API·푸시
│   ├── preprocessing/    # 감지·추적·인물 연결
│   ├── analysis/         # metadata 분석
│   ├── mediamtx/         # 영상 수신·녹화·재생
│   └── nginx/            # HTTPS·중계
├── config/               # 설정 예시
├── secrets/              # 인증 설정 예시
├── scripts/              # 초기화·진단·백업·이관·명세 생성
├── compose.yml           # 운영
├── compose.dev.yml       # 개발용 코드 연결
├── compose.test.yml      # 독립 테스트 환경
└── compose.push.yml
edge/                     # Raspberry Pi
mobile/                   # Flutter 앱
configurator/             # Windows GUI·CLI, pyproject.toml·uv.lock·tests/
lib/                      # pyproject.toml과 ai_cctv_core 공통 패키지
tests/                    # 개발·검증용 코드
├── automated/            # 공통·통합 자동 테스트
├── runner/               # 테스트 이미지·pytest·Ruff 설정
└── mock_edge/            # MP4 모의 카메라
docs/                     # 위 네 문서
```

## 설치 선택

Windows 설치 파일을 사용하면 다음 절을, 소스를 실행하면 [소스 배포](#소스-배포)를 따른다. 중앙 기동 후 [Edge](#edge-연결), [모바일·푸시](#모바일과-푸시)를 연결한다. 예시 주소·파일 경로는 실제 값으로 바꾼다.

### Windows 설치

Windows 10/11 x64와 실행 중인 Docker Desktop·Compose v2가 필요하다.

1. `AI_CCTV_Server_Setup_<version>_x64.exe` 설치 후 **AI CCTV Configurator**를 연다.
2. **Downloaded AI model**에서 호환 로컬 모델을 선택한다. 모델은 설치 프로그램에 포함되지 않는다.
3. **TLS certificate / TLS private key**에 신뢰 인증서와 암호화되지 않은 PEM 키를 선택한다.
4. 저장 경로·관리자 계정·공개 HTTPS origin·RTSP bind·감지 장치·녹화 정책을 입력한다.
5. **Validate and create configuration** → **Start services** → **Show service status**를 실행한다.

설정 생성에는 관리자 권한이 필요하다. 기본 데이터 위치는 `C:\ProgramData\AI_CCTV`, 배포 env는 그 아래 `config\compose.env`다. 코드와 별도로 DB·영상·모델·인증 파일을 보존한다.

GUI 대신 관리자 PowerShell에서 사용할 수 있다. 비밀번호는 숨김 입력으로 받는다.

```powershell
AI_CCTV_CLI.exe preflight
AI_CCTV_CLI.exe install --model 'D:\Models\person.pt' --tls-certificate 'D:\TLS\tls.crt' --tls-private-key 'D:\TLS\tls.key' --public-base-url 'https://cctv.example.com'
AI_CCTV_CLI.exe status
```

### 소스 배포

저장소 루트에서 초기 설정 스크립트용 Python 3.11, Docker/Compose v2, 개발 인증서용 OpenSSL을 준비한다. 아래는 **새 개발 배포**용이다. 운영 설정을 예제로 덮어쓰지 않는다.

```powershell
Copy-Item server/.env.example server/.env
Copy-Item server/config/config.example.yaml server/config/config.yaml
python server/scripts/init_runtime.py
python server/scripts/generate_secrets.py --camera-id cam-001
python server/scripts/generate_dev_cert.py
```

Linux에서는 `Copy-Item`을 `cp`, `python`을 Python 3.11의 `python3` 명령으로 바꾼다. `init_runtime.py`는 폴더만 만든다. `config.yaml`을 실제 카메라·정책에 맞추고 호환 모델을 `MODELS_DIR/MODEL_FILE`(기본 `server/runtime/models/default.pt`)에 둔다.

`server/.env`에서 다음을 확인한다.

| 설정 | 확인 사항 |
|---|---|
| `PUBLIC_BASE_URL` | 앱이 접속할 HTTPS origin, `/api/v1` 제외 |
| `PUBLIC_BIND_ADDRESS` | 기본 loopback; 외부 제공 시 인터페이스·방화벽 설정 |
| `RTSP_BIND_ADDRESS` | 원격 Edge가 접근할 중앙 신뢰 LAN IP |
| `CONFIG_FILE`, `*_DIR` | 설정·DB·녹화·복구·스냅샷·모델·인증서 실제 경로 |
| `*_SECRETS_FILE` | 생성한 역할별 env 5개, 서로 다른 경로 |
| `AI_CCTV_UID/GID` | Linux 호스트 저장소 소유자와 일치 |
| `RECORDING_SEGMENT_SECONDS` | 10~300초, 기본 60초 |
| `COMPOSE_PROJECT_NAME` | 운영·업데이트 때 유지할 프로젝트 이름 |

8554는 신뢰 LAN에서만 사용한다. 내부 8000·8080·8888·9996·9997은 외부에 공개하지 않는다. 운영은 도메인에 맞는 CA 전체 체인 `CERTS_DIR/tls.crt`와 개인키 `tls.key`를 사용한다. 개발용 자체 서명 인증서 때문에 앱의 검증을 끄지 않는다.

```powershell
python server/scripts/doctor.py
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --wait --remove-orphans
python server/scripts/bootstrap_admin.py --username admin
```

`bootstrap_admin.py`는 `server/.env`를 쓰는 소스 배포용이다. Configurator 설치에서는 설정 생성 시 관리자를 준비한다.

## Edge 연결

Raspberry Pi 설치·Pairing·업데이트는 [Edge 안내](edge/README.md#설치와-연결)를 따른다. 실제 장비 없이 MP4로 시험하려면 [Mock Edge](tests/mock_edge/README.md)를 사용한다.

### 수동 연결

자동 검색을 사용할 수 없다면 [토큰 내보내기·수동 등록](edge/README.md#수동-연결)을 따른다. 관리·복구 주소는 중앙 컨테이너에서도 접근할 수 있어야 한다.

## 모바일과 푸시

앱 실행·서명은 [모바일 README](mobile/README.md)를 따른다. 대체 앱 개발자는 [OpenAPI](docs/openapi.yaml)를 사용한다. 기본 앱 식별자는 `com.example.app`이며 중앙 HTTPS origin으로 로그인한다.

푸시를 켜려면 같은 기존 Firebase 프로젝트의 두 파일을 준비한다.

- Android: `mobile/android/app/google-services.json`. 등록 package name과 applicationId가 같아야 한다.
- 서버: FCM 발송 권한의 서비스 계정 JSON. 저장소 밖에 보관하고 External이 읽게 한다. APK에는 넣지 않는다.

실제 `compose.env` 또는 `.env`와 같은 폴더에 [push.env 예시](server/push.env.example)를 바탕으로 작성한다.

```dotenv
PUSH_ENABLED=true
FIREBASE_PROJECT_ID=your-existing-project-id
FIREBASE_SERVICE_ACCOUNT_FILE='C:/ProgramData/AI_CCTV/secrets/firebase-service-account.json'
```

Configurator **Start services**로 적용한다. 수동 실행은 기본 env 다음에 push env를 지정한다.

```powershell
docker compose --env-file C:/path/to/compose.env --env-file C:/path/to/push.env `
  -f server/compose.yml -f server/compose.push.yml up -d --build --wait --remove-orphans
```

이후 상태·로그·중지에도 같은 추가 구성을 사용한다. Android 알림 권한을 허용하고 실제 이벤트 → 알림 → 상세 → 녹화를 확인한다. 기본은 모든 이벤트 수신이다. 키가 없으면 일반 앱 기능은 유지하되 실제 푸시는 검증할 수 없다.

## 운영과 백업

아래 env는 실제 설치 경로로 바꾼다. FCM을 쓰면 위의 추가 env·Compose 파일도 포함한다.

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml ps
docker compose --env-file C:/path/to/compose.env -f server/compose.yml logs --tail 100
```

`healthy`와 Nginx `/healthz`만으로 전체 성공을 판단하지 않는다. 로그인·각 카메라 영상·새 이벤트·중앙/복구 녹화 재생을 확인한다. 블랙박스 `unconfigured`는 모델 미구현이며 [상태 의미](docs/architecture.md#상태-확인)를 참고한다.

전체 백업은 Edge 로컬 백업을 확인하고 중앙 서비스를 정상 정지한 뒤 다음을 **같은 시점의 세트**로 복사한다.

| 대상 | 포함 경로 |
|---|---|
| 설정·인증 | 실제 env, `CONFIG_FILE`, `release-manifest.json`, 서비스별 비밀 파일 5개, 선택적 push.env·Firebase 계정 |
| DB·미디어 | `DATABASE_DIR`, `RECORDINGS_DIR`, **별도의 `RECOVERED_DIR`**, `SNAPSHOTS_DIR` |
| 모델·TLS | `MODELS_DIR`, `CERTS_DIR` |
| 복원 기준 | 코드·이미지 버전, 모델·파일 해시, 실제 경로와 파일 권한 |

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml down
# 실제 경로의 백업을 완료한다.
docker compose --env-file C:/path/to/compose.env -f server/compose.yml up -d --wait
```

호스트의 복구 영상은 중앙 녹화와 별도 폴더다. 실행 중 SQLite 파일을 개별 복사하지 않는다. DB만 온라인 백업하는 `python server/scripts/backup_database.py`는 `server/.env` 배포용이며 결과는 `DATABASE_DIR/backups`다. 영상·설정은 포함하지 않는다.

복원할 때 서비스를 중지하고 현재 자료를 별도 보존한다. 같은 백업 세트의 코드·이미지·DB·미디어·설정·키를 복원하고 무결성·권한·실제 재생을 확인한다. **자동 DB downgrade는 없다.**

인증서 갱신은 새 `tls.crt`·`tls.key` 배치 → 동일 Compose 명령의 `exec -T nginx nginx -t` → `exec -T nginx nginx -s reload` 순서로 한다. 실제 단말 검증과 만료일을 확인한다.

자동 복구가 놓친 구간은 Edge에 원본이 남아 있을 때 수동으로 가져온다. 아래 토큰은 [수동 연결](#수동-연결)에서 내보낸 해당 Edge의 보호 파일이며, 명령에는 값 대신 환경변수 이름을 전달한다. 기간은 UTC로 한 번에 최대 24시간을 지정한다.

```powershell
$env:EDGE_RECOVERY_TOKEN = (Get-Content -Raw -LiteralPath 'C:\secure\edge-001-control.token').Trim()
try {
  docker compose --env-file C:/path/to/compose.env -f server/compose.yml exec -T -e EDGE_RECOVERY_TOKEN data `
    python -m app.workers.recovery --edge-url http://192.0.2.41:8002 --camera-id cam-001 `
    --start 2026-09-07T00:00:00Z --end 2026-09-07T01:00:00Z
} finally {
  Remove-Item Env:EDGE_RECOVERY_TOKEN
}
```

실제 배포의 추가 Compose 설정도 포함한다. 이 명령은 크기·해시를 검사해 중앙에 등록하며 Edge 원본을 삭제하지 않는다. 자세한 인자는 Data 내부의 `python -m app.workers.recovery --help`로 확인한다.

## 업그레이드

먼저 백업하고 기존 프로젝트 이름·데이터 경로를 유지한다. Windows 설치 프로그램은 코드를 교체하지만 운영 설정을 자동 이관하지 않는다. 이전 inference·identity 구성에서 전환할 때만 Python 3.11로 실행한다.

```powershell
python server/scripts/enable_object_processing.py --server-dir server --env-file C:/path/to/compose.env
```

설치 패키지는 실제 설치의 스크립트·server 경로를 지정한다. 기존 토큰을 보존하며 오래된 단일 `secrets.env`는 수동 이전이 필요하다. 중간 실패는 해결 후 재실행한다. 설정 초기화로 이관을 대신하지 않는다.

Configurator **Start services** 또는 `up -d --build --wait --remove-orphans`로 재생성한다. 단순 Restart는 새 환경·이미지를 반영하지 않는다. 같은 프로젝트의 구형 감지 컨테이너가 정리되고 6개만 실행되는지 확인한다. Data 시작 시 DB 마이그레이션이 적용되므로 실패 시 이전 이미지와 일관된 백업을 함께 복원한다.

Windows 제거는 기본 운영 데이터를 보존한다. 사용자 지정 경로라면 먼저 `AI_CCTV_CLI.exe stop --env-file <실제 경로>`로 중지한다.

## 개발과 검증

아래 명령은 소스 저장소에서 실행한다. Windows 설치본은 운영용이며 개발 Compose·테스트 코드·문서 생성기는 포함하지 않는다. 서버 개발·테스트 도구는 컨테이너에 설치한다.

| 위치 | 관리하는 환경 |
|---|---|
| `lib/pyproject.toml` | 공통 라이브러리; Python 서비스 이미지가 빌드할 때 설치 |
| `server/services/*/requirements.txt` | 해당 서비스 실행 패키지 |
| `server/services/*/requirements-dev.txt` | 해당 서비스의 개발·테스트 도구 추가 |
| `tests/runner/` | 서버 공통·통합 테스트 이미지, pytest·Ruff 설정 |
| `configurator/pyproject.toml`, `configurator/uv.lock` | Windows GUI·CLI 개발 및 빌드 |
| `edge/pyproject.toml`, `mobile/pubspec.yaml` | 각 장치 프로그램의 독립 개발 환경 |

### 서버 코드 개발

[소스 배포](#소스-배포)로 **별도 개발 배포**를 먼저 준비한다. 개발용 env의 프로젝트 이름·저장소·포트는 운영 배포와 다르게 지정한다. `compose.dev.yml`은 지정한 env의 데이터를 사용하므로 운영 env에 적용하지 않는다. 아래는 개발 전용 `server/.env`를 가정한다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml up -d --build
```

PC의 코드를 컨테이너에 읽기 전용으로 연결한다. 파일은 PC에서 편집하고 Python 서비스는 변경 시 재시작한다. 의존성을 변경하면 이미지를 다시 빌드한다. 운영 이미지는 `production`, 개발 이미지는 `development` 단계이며 태그도 분리한다.

서비스별 테스트는 해당 개발 컨테이너에서 실행한다. `data`를 `external`, `preprocessing`, `analysis`로 바꿔 사용할 수 있다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

### 서버 자동 테스트

설정 파일·Firebase·카메라 없이 다음 명령으로 공통·서비스별 테스트를 실행한다. 테스트용 DB·영상은 컨테이너의 임시 폴더에 만들며 운영 저장소를 연결하지 않는다. GUI·Edge 단독 테스트는 여기서 제외하고, Edge/Configurator와의 프로토콜 연동 검증은 포함한다.

```powershell
docker compose -f server/compose.test.yml build tests
docker compose -f server/compose.test.yml run --rm tests
docker compose -f server/compose.test.yml run --rm tests python -m ruff check --config tests/runner/ruff.toml --no-cache lib server tests configurator edge
docker compose -f server/compose.test.yml run --rm tests python server/scripts/export_openapi.py --check
```

실제 Data·External 프로세스 간 HTTP 인증·이벤트·분석 작업 완료·로그아웃도 별도로 검증한다.

```powershell
docker compose -f server/compose.test.yml --profile integration up --build --abort-on-container-exit --exit-code-from integration integration
docker compose -f server/compose.test.yml --profile integration down
```

`compose.test.yml`은 **단독 구성**이며 운영 Compose와 합치지 않는다. 외부 접속이 차단된 테스트 네트워크와 임시 저장소를 사용한다. 고정된 테스트 자격 증명은 이 내부 시험 전용이다. 이 검사는 Nginx·MediaMTX·실제 모델·단말 푸시까지 검증하지 않는다.

OpenAPI 갱신 시에만 호스트의 문서 폴더를 쓰기 가능하게 연결한다. PowerShell 예시이며 Linux는 `${PWD}` 대신 `$(pwd)`, `--user`에는 호스트 UID:GID를 사용한다.

```powershell
docker compose -f server/compose.test.yml run --rm --user 0:0 -v "${PWD}/docs:/workspace/docs" tests python server/scripts/export_openapi.py
```

Windows GUI·CLI는 [Configurator README](configurator/README.md), Edge는 [Edge README](edge/README.md), 모바일은 [모바일 README](mobile/README.md), 모의 카메라는 [Mock Edge README](tests/mock_edge/README.md)를 따른다.

실환경에서는 동시 영상·HD/FHD 변경·권한·녹화 복구·백업 복원·앱 푸시를 확인한다. 단위 테스트는 실제 카메라·영상·단말 검증을 대신하지 않는다. 기존 SQL 마이그레이션은 수정하지 않고 새 버전을 추가한다.

### 패키지 빌드

소스 저장소에서 [Windows 설치 파일](configurator/README.md#설치-파일-빌드) 또는 [Edge 패키지](edge/README.md#패키지-빌드)를 만든다. 결과와 체크섬은 각각 `dist/installer/`, `dist/edge/`에 생성된다. 배포 전 실제 장비에서 설치·업데이트·제거를 확인한다. Windows 실행 파일 서명은 별도 준비가 필요하다.

## 배포 조건

프로젝트 라이선스는 아직 확정하지 않았다. PyQt, Ultralytics·모델 가중치, MediaMTX, OpenCV·GStreamer·FFmpeg 등 포함 구성요소의 배포 조건을 확인한다. 실제 영상·비밀키·개인 설정을 커밋하지 않는다. 저장 영상 암호화는 제공하지 않으므로 장비·디스크·백업 접근 권한을 관리한다.
