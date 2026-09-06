# AI CCTV

Raspberry Pi 카메라의 영상을 중앙에서 수신·녹화하고, 객체 이벤트와 모바일 관제를 제공하는 프로젝트입니다. 중앙은 Docker 6개 서비스로 실행하며 Edge·모바일·Configurator는 별도 프로그램입니다.

## 저장소 구조

```text
new_structure/
├── server/
│   ├── services/
│   │   ├── data/                  # SQLite·이벤트·작업·저장소
│   │   │   └── app/               # api, database, storage, workers
│   │   ├── external/              # 인증·공개 API·푸시
│   │   │   └── app/               # api, security, clients, notifications, workers
│   │   ├── preprocessing/         # 감지·추적·인물 연결
│   │   │   ├── app/               # 영상 수신·결과 전달·상태 확인
│   │   │   └── processors/        # detection, identity 연결부
│   │   └── analysis/              # metadata 분석
│   │       ├── app/               # 작업 수신·결과 반환·상태 확인
│   │       └── processors/        # 분석 블랙박스 연결부
│   ├── mediamtx/                  # 영상 수신·녹화·재생
│   ├── nginx/                     # HTTPS·API·영상 중계
│   ├── config/
│   ├── secrets/                   # 서비스별 비밀 설정 예시
│   ├── scripts/                   # 초기화·진단·백업·설정 이관
│   ├── compose.yml
│   └── compose.push.yml
├── edge/                          # Raspberry Pi 프로그램·자체 테스트
├── mobile/                        # Android 우선 Flutter 관제 앱·테스트
├── configurator/                  # Windows 설치·설정 GUI/CLI·패키징
├── src/ai_cctv_core/
│   ├── contracts/                 # 공유 입출력 계약
│   ├── processing/                # 객체 작업 수신·검증·결과 전달
│   └── ...                        # 설정·식별자·UTC
├── tests/                         # 공통·서비스 간 통합 테스트
├── tools/mock_edge/               # MP4 기반 모의 Edge
├── docs/
│   ├── design/                    # SRS·SDS·ARCHITECTURE
│   ├── operations/                # 설치·운영·배포
│   ├── adr/                       # 설계 결정
│   └── ...                        # API·객체·모바일·검증 계약
├── pyproject.toml
├── uv.lock
├── requirements.txt
└── requirements-dev.txt
```

Python 서비스별 `tests/`, `Dockerfile`, `requirements.txt`, `README.md`는 각 서비스 폴더에 있습니다. 이전 프로토타입은 작업 폴더에서 삭제했고 Git 기록에서 참고합니다.

## 서비스와 컨테이너

| 컨테이너 | 책임·개발 안내 |
|---|---|
| `data` | [SQLite 단독 관리, 이벤트·관측·작업·결과·복구 저장](server/services/data/README.md) |
| `external` | [사용자 인증·카메라 ACL·공개 API·Edge 수집·FCM](server/services/external/README.md) |
| `preprocessing` | [감지·추적·박스·크롭과 전역 인물 연결](server/services/preprocessing/README.md) |
| `analysis` | [객체 입력에 대한 metadata 분석 인터페이스](server/services/analysis/README.md) |
| `mediamtx` | RTSP 수신·HLS·녹화·Playback |
| `nginx` | HTTPS 진입점·인증된 영상/API·내부 요청 중계 |

Preprocessing은 기존 YOLO/ByteTrack을 교체 가능한 기본 감지기로 제공합니다. 전역 인물 연결과 metadata 분석은 **`unconfigured` 블랙박스**이며, 담당 개발자가 [객체 처리 계약](docs/object-processing.md)에 맞춰 구현합니다. 감지와 인물 연결은 같은 컨테이너 안에서 독립 작업기로 실행합니다.

## 시작 방법

- **설치 담당자:** [Windows 설치·Configurator](docs/operations/windows-installer.md) → [Raspberry Pi 설치·Pairing](docs/operations/edge-deployment.md)
- **중앙 개발·배포:** [환경 준비·비밀 설정 생성·Compose 실행](docs/operations/central-deployment.md)
- **모바일:** [Flutter 실행·Android 설정](mobile/README.md), [Firebase 설정·실기기 수신 확인](docs/mobile-push.md)
- **알고리즘 담당자:** [Preprocessing](server/services/preprocessing/README.md), [Analysis](server/services/analysis/README.md)의 계약·플러그인·테스트
- **모의 카메라 시험:** [MP4 Mock Edge](tools/mock_edge/README.md)
- **기존 설치 업데이트:** [설정·서비스 이관](docs/object-processing.md#배포). 기존 토큰을 보존한 뒤 컨테이너를 재생성합니다.

저장소 루트에서 Python 개발 환경과 테스트를 실행합니다.

```sh
uv sync --extra test
uv run pytest -q -p no:cacheprovider
uv run ruff check . --no-cache
```

모델·인증서·배포 설정을 준비한 뒤 중앙 서비스를 시작합니다.

```sh
docker compose --env-file server/.env -f server/compose.yml config --quiet
docker compose --env-file server/.env -f server/compose.yml up -d --build --remove-orphans
```

운영 설정은 Configurator가 생성한 실제 `compose.env` 경로를 사용합니다. `INFERENCE_*` 환경변수와 설정 파일의 `inference` 구역은 기존 감지 설정 호환성을 위해 유지합니다. 상세 명령은 [종합 안내](docs/operations/project-guide.md)에 보관했습니다.

## 현재 검증 경계

모바일 박스는 최신 객체 좌표를 HLS 위에 표시하므로 영상과 시차가 생길 수 있습니다. 정확한 프레임 동기화는 구현하지 않았습니다. Firebase 설정 파일이 없어 실제 FCM 발송·수신은 미검증이며, Android 실기기·Docker·Raspberry Pi 실환경 인수도 별도 확인이 필요합니다. 모델과 두 블랙박스의 실제 정확도는 자동 계약 테스트로 보장되지 않습니다.

설계 정본은 [SRS](docs/design/SRS.md), [SDS](docs/design/SDS.md), [Architecture](docs/design/ARCHITECTURE.md)입니다. [공개 API](docs/external-app-integration.md), [사람 식별자](docs/person-identifiers.md), [인수 점검](docs/srs-compliance-audit.md)을 함께 참고합니다.

라이선스와 제3자 배포 검토 사항은 [종합 안내의 라이선스 절](docs/operations/project-guide.md#라이선스)에 남겼습니다.
