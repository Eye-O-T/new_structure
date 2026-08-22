# AI_CCTV 구현·검증 상태

기준 소스는 `Eye-O-T/AI_CCTV`의 `develop` 브랜치 commit `09db3ed`이며, 루트의 `SRS.md`, `ARCHITECTURE.md`, `README.md`를 구현 기준으로 사용했습니다. 기존 PyQt/RTSP 프로토타입은 삭제하지 않고 legacy 경로에 보존했습니다.

## 구현 결과

| 영역 | 결과 |
| --- | --- |
| 중앙 배포 | Data, External, Inference, MediaMTX, Nginx의 5-container Compose와 영속 bind mount 구현 |
| Edge | H.264 취득, 중앙 RTSP publish, 10초 MPEG-TS ring backup, 장애 격리 재연결, systemd·ARM64 deb recipe 구현 |
| Media | Camera ID path, publish 인증, HLS, 60초 fMP4 녹화, Playback, 완료 hook 구현 |
| Data | SQLite WAL/FK/migration, 카메라·사용자·ACL, recording/event M:N, 검색, retention, backup, reconciliation 구현 |
| Inference | 최대 4개 독립 RTSP worker, YOLO/ByteTrack, 출현·이탈·네트워크 이벤트와 snapshot 구현 |
| External | JWT access/refresh rotation/logout, Argon2, RBAC/ACL, 카메라·녹화·이벤트 API, 보호 영상 인증 구현 |
| Recovery | Edge Bearer manifest/file API와 중앙 순차 다운로드·SHA-256·원자 이동·idempotent `edge_recovery` 인덱싱 구현 |
| Configurator | 공용 schema, GUI/CLI, secret·카메라 credential 생성, custom/manifest model 설치, Compose 제어·doctor 구현 |
| Packaging | PyInstaller/Inno Setup 정의와 Raspberry Pi OS ARM64 `.deb` 빌드 정의 구현 |
| 운영 | TLS, 중앙 배포, 저장소/DB backup·restore, Edge recovery 절차 및 ADR 제공 |

## 자동 검증 결과

- `pytest`: **63 passed**
- Ruff lint: 구현 경로 전체 통과
- Python `compileall`: 통과
- `git diff --check`: 통과
- Root/Configurator wheel 및 Edge wheel 빌드: 통과, 예상 package 포함 확인
- Docker Compose v2.29.7 정적 render: 통과, Host 공개 포트는 Nginx 80/443과 MediaMTX RTSP 8554로 제한
- MediaMTX v1.9.0 실제 binary config load 및 RTSP/HLS/Playback/API listener 시작: 통과
- 설정 example의 strict schema load와 shell script 구문 검사: 통과

## 목표 환경에서 추가해야 하는 인수 시험

아래 항목은 소스나 build recipe의 누락이 아니라 이 작업 환경에 해당 운영체제·장비·Docker daemon이 없어 실행하지 못한 검증입니다.

1. Windows에서 PyInstaller와 Inno Setup으로 `.exe`를 만들고 설치·업데이트·제거 UI를 확인합니다.
2. Docker daemon이 있는 중앙 서버에서 5개 image를 build/up하고 `nginx -t`, 전체 health, 실제 HLS/Playback을 확인합니다.
3. ARM64 Raspberry Pi에서 `.deb`를 만들고 libcamera, GStreamer, systemd, 재부팅 자동 시작을 확인합니다.
4. 실제 카메라 1~4대에서 해상도/FPS/bitrate, 24시간 안정성, 네트워크 단절 중 backup 및 복구를 측정합니다.
5. CPU/GPU별 YOLO 모델의 FPS·지연·메모리를 측정하고 운영 모델 Manifest의 URL, SHA-256, license를 확정합니다. 현재 example Manifest는 이 release 결정 전까지 의도적으로 비활성입니다.
6. 운영 DNS·신뢰 TLS 인증서·LAN 방화벽·Router 정책을 적용한 외부 접근과 보안 시험을 수행합니다.

## 보안 인계

기존 브랜치에 추적되던 `.proj_env`는 배포본에서 제거하고 `.proj_env.example`로 대체했습니다. 과거 Git 이력에 실제 Discord Bot token을 넣은 적이 있다면 이 배포 여부와 무관하게 Discord에서 해당 token을 폐기·재발급해야 합니다.

실행 순서는 `README.md`와 `docs/operations/`에 정리되어 있습니다.
