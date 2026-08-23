# SRS 구현 정합성 점검

## 목차

- [1. 점검 기준](#1-점검-기준)
- [2. 이번 점검에서 수정한 사항](#2-이번-점검에서-수정한-사항)
- [3. 요구사항 영역별 상태](#3-요구사항-영역별-상태)
- [4. 인수 시나리오 상태](#4-인수-시나리오-상태)
- [5. 남은 구현 및 검증 Gap](#5-남은-구현-및-검증-gap)
- [6. 검증 결과와 재검증 명령](#6-검증-결과와-재검증-명령)

## 1. 점검 기준

이 문서는 `SRS.md`의 `0.3.0-draft`, 기준일 2026-08-23을 현재 저장소와
대조한 2026-08-24 점검 결과다. 자동 테스트 통과는 실제 Raspberry Pi, Windows VM,
Docker MediaMTX와 2~4개 실시간 Stream의 인수 시험을 대신하지 않는다.

상태 의미는 다음과 같다.

| 상태 | 의미 |
| --- | --- |
| 자동 검증 | 해당 계약을 실행하는 Unit/Integration Test가 존재하고 통과함 |
| 구성 검증 | 코드·Compose·설정은 존재하지만 실제 장비/Container Black-box 시험이 필요함 |
| 부분 구현 | 핵심 경로는 있으나 SRS의 일부 기능 또는 운영 Adapter가 없음 |
| 미검증 | 구현 완료를 선언할 실환경 증거가 없음 |

## 2. 이번 점검에서 수정한 사항

| SRS 기준 | 발견한 차이 | 수정 결과 |
| --- | --- | --- |
| `FR-STORAGE-002~003` | MediaMTX 중앙 Segment가 60초로 고정되어 Configurator 설정이 전달되지 않음 | GUI/CLI에 10~300초 설정을 추가하고 `config.yaml`, Compose, MediaMTX와 Data Reconciliation에 같은 값을 전달 |
| `FR-STORAGE-011~012` | 보관 일수와 저장공간 경고 기준을 초기 GUI/CLI에서 선택할 수 없음 | GUI/CLI 입력과 공통 Config 생성에 추가 |
| `FR-MODEL-007~008` | CPU 실행 모드를 설치 시 명시적으로 선택할 수 없음 | `auto`, `cpu`, `cuda`, `cuda:<index>` 계약과 GUI/CLI 선택을 추가하고 잘못된 Device를 시작 전에 거부 |
| `NFR-MAINT-004` | 설치 Version, Image Tag와 설치 Model Hash를 묶은 Manifest가 없음 | `config/release-manifest.json`을 원자 생성하고 Application/Image/Model SHA-256을 기록 |
| `FR-UPDATE-002` | 수동 배포 예시와 Compose 기본값에 개발용 Application Image Tag가 남아 있음 | Application Image 기본값과 `.env.example`을 Release `0.3.0`으로 고정하고 MediaMTX·Nginx 등 외부 Image도 Manifest에 기록 |
| `NFR-UX-003` | 등록 전에 선택한 Edge Endpoint와 광고 Identity를 확인하는 명시적 UI 동작이 없음 | HMAC 검증 후 선택한 Edge의 관리 Health와 Device/Camera Identity를 확인하는 **Test selected Edge connection** 추가 |
| `FR-EDGE-019`, 운영 Event 계약 | Mock Edge가 `power_disconnected`와 `ac`를 사용함 | 실제 Edge와 같은 `external_power_lost/restored`, `power_source=external`, `storage_critical` 및 Metadata 계약으로 수정 |
| Profile 운영 Event 계약 | Mock Edge의 Profile 거부가 `video_profile_change_failed`를 남기지 않음 | 지원 불가와 Pipeline 실패 모두 안정된 `reason_code` Event를 기록 |
| SRS 제17장 | 추적성 범위가 뒤에 추가된 요구사항 번호를 포함하지 않음 | INSTALL·RECOVERY·MEDIA·NGINX 범위를 최신 ID까지 수정 |

## 3. 요구사항 영역별 상태

| 요구사항 영역 | 상태 | 구현·검증 근거 | 추가로 필요한 검증 |
| --- | --- | --- | --- |
| `FR-INSTALL-001~031` | 구성 검증 | Configurator Core/GUI/CLI, Inno/PyInstaller Script, Discovery·Pairing Test | 산출된 Installer를 사용한 깨끗한 Windows 11 VM 설치·제거 |
| `FR-EDGE-001~021` | 구성 검증 | Edge Pipeline, Capability/Profile Rollback, Status/Event, systemd와 Edge Test | Raspberry Pi 4B + Camera Module 3에서 HD/FHD Bitrate·재부팅·Watchdog 시험 |
| `FR-RECOVERY-001~013` | 자동 검증 | Recovery Manifest/Hash/Range, 장애 병합, Retry, 멱등 Index Test | 실제 LAN 단절 후 MPEG-TS 회수와 중앙 재생 |
| `FR-MEDIA-001~013` | 구성 검증 | MediaMTX 1.9.0 고정 설정, Camera별 Auth, HLS/Playback/Nginx 계약 Test | MediaMTX Container의 RTSP→HLS→Recording Black-box 시험 |
| `FR-STORAGE-001~013` | 자동 검증 | 파일/DB 분리, Hook, Retention, Reconciliation, 삭제 보상 Test | 비정상 종료된 실제 fMP4 재생과 장기 Disk 임계치 시험 |
| `FR-DATA-001~012` | 자동 검증 | 단일 Data API Writer, WAL/FK/Migration/Index/Search/Backup Test | Container 재생성 및 대규모 기준 Dataset 성능 시험 |
| `FR-AI-001~006`, `009~012` | 자동 검증 | RTSP Worker, YOLO/ByteTrack Adapter, Track 전이, Model 실패 격리, Event 연결 Test | 실제 Model과 2~4개 RTSP Stream 처리량 시험 |
| `FR-AI-007~008`, `013` | 부분 구현 | Event Metadata는 확장 가능하고 Legacy Discord 구현은 분리됨 | 중앙 Inference Service용 선택 VLM/Notification Adapter 구현과 실패 격리 Test |
| `FR-MODEL-001~008` | 구성 검증 | 확장자/크기/SHA-256 원자 복사, Read-only Mount, CPU/GPU Device 설정 | Windows GPU Container와 오류 진단 인수 시험 |
| `FR-AUTH-001~015` | 자동 검증 | Argon2, JWT Claim, Refresh Rotation/철회, Cookie/Bearer, RBAC, Login Backoff Test | 공개 TLS 배포에서 Cookie 속성 확인 |
| `FR-USER-001~010` | 자동 검증 | Camera ACL, 검색 Pagination, Event/Recording/Live/Playback API Test | 실제 HLS Player와 MPEG-TS/fMP4 Client 호환 시험 |
| `FR-NGINX-001~012` | 자동·구성 검증 | Public/Internal Listener 분리, Auth Subrequest, URI 혼동 방지 Test | 실제 Nginx TLS와 Range/Streaming Black-box 시험 |
| `FR-OPS-001~012` | 자동·구성 검증 | Health, Doctor, Status Collector, 운영 Event 전이 Test | Docker 장애, DB 손상과 저장소 접근 불가 운영 훈련 |
| `FR-UPDATE-001~006` | 구성 검증 | DB Backup, Migration, 보존형 Installer/Uninstaller와 Edge Rollback 문서 | 이전 Release부터 실제 Upgrade/Rollback 시험 |
| `NFR-PERF-*` | 미검증 | 설정상 4개 활성 Camera 제한과 독립 Worker 존재 | 기준 Hardware에서 HD/FHD 4 Stream, HLS 시작, 30일 Dataset Benchmark |
| `NFR-REL-*` | 구성 검증 | Restart Policy, Volume, 재접속, SQLite 단일 Writer | 강제 종료·Container 재생성 Black-box 시험 |
| `NFR-SEC-*` | 자동·구성 검증 | TLS 경계, Secret 분리, Path/URI Validation, Argon2/JWT/ACL Test | 운영 인증서·방화벽·Windows DACL 인수 시험 |
| `NFR-MAINT-*` | 부분 구현 | Service 경계, OpenAPI, Dependency Pin, Release Manifest, DB Migration | Schema Version 1 이전 Config가 생기면 명시적 Config Migration 추가 |
| `NFR-UX-*`, `NFR-PORT-*` | 구성 검증 | GUI/CLI, Windows Path, 연결 시험, ARM64 Package Script | 비개발자 사용성 시험과 실제 ARM64 `.deb` 설치 |

## 4. 인수 시나리오 상태

| ID | 현재 판정 | 남은 조건 |
| --- | --- | --- |
| `AT-001` | 미검증 | Windows VM에서 Installer→GUI→Compose Ready |
| `AT-002` | 미검증 | ARM64 `.deb` 설치와 재부팅 후 자동 RTSP |
| `AT-003` | 미검증 | 2개 실제/Mock Edge 동시 HLS 재생 |
| `AT-004` | 부분 검증 | 4개 활성 등록 제한은 자동 검증, 동시 Stream은 미검증 |
| `AT-005` | 부분 검증 | Hook/DB 계약은 자동 검증, 실제 60초 fMP4는 미검증 |
| `AT-006` | 자동 검증 | 겹침 검색 Test 통과 |
| `AT-007` | 부분 검증 | Track Event/Segment 연결은 자동 검증, 실제 사람 영상은 미검증 |
| `AT-008` | 자동 검증 | HLS/Playback/Event 미인증 차단 Test 통과 |
| `AT-009` | 자동 검증 | Viewer 관리 API 403 Test 통과 |
| `AT-010` | 부분 검증 | 복구 API/Coordinator는 자동 검증, 실제 LAN 단절은 미검증 |
| `AT-011` | 미검증 | 실제 Compose `down`/`up` 영속성 시험 |
| `AT-012` | 자동·구성 검증 | Model 실패 Worker 격리 Test 통과, 실제 HLS 병행 확인 필요 |
| `AT-013` | 자동 검증 | Edge/중앙 저장 임계 전이 Test 통과 |
| `AT-014` | 자동 검증 | Secret 비반사·파일 분리·허용목록 Test 통과 |
| `AT-015` | 미검증 | Raspberry Pi 실영상의 720p/30fps/약 2Mbps 측정 |
| `AT-016` | 부분 검증 | Profile 적용/확정/Rollback Test 통과, 실영상 FHD 측정 필요 |
| `AT-017` | 자동 검증 | 기존 HD 유지와 `UNSUPPORTED_VIDEO_PROFILE` Test 통과 |

## 5. 남은 구현 및 검증 Gap

우선순위가 높은 순서는 다음과 같다.

1. Mock Edge 2~4개 또는 실제 Edge를 사용한 MediaMTX→HLS→Recording Docker
   Black-box Test를 자동화한다.
2. Windows 11 VM에서 실제 Installer와 Uninstaller를 실행하고 Runtime Data 보존을
   확인한다.
3. Raspberry Pi 4B에서 `.deb`, systemd, Camera Frame Watchdog, HD/FHD Bitrate와 장애
   복구를 검증한다.
4. 4개 HD/FHD Stream과 30일분 Metadata 기준 Dataset의 성능 결과를 Hardware 정보와
   함께 기록한다.
5. 새 중앙 Inference Service에 선택 VLM/Notification Adapter가 필요하면 Versioned
   `metadata.attributes` 계약과 실패 격리 Test를 구현한다.
6. Version 1 이전 Config를 실제로 지원해야 하는 시점에 Config Migration 입력·출력과
   Rollback 규칙을 추가한다.

## 6. 검증 결과와 재검증 명령

2026-08-24 현재 저장소에서 다음 결과를 확인했다.

| 검증 | 결과 |
| --- | --- |
| Python 전체 Test Suite | `201 passed` |
| Ruff 정적 검사 | `All checks passed` |
| Compose YAML 기본 구조 | PyYAML Parse 통과, `data`·`external`·`inference`·`mediamtx`·`nginx` 확인 |
| Docker Compose 해석 및 Container Black-box | 현재 점검 환경에 Docker CLI가 없어 미실행 |

Docker가 설치된 인수 환경에서는 아래 명령을 모두 다시 실행한다.

```powershell
uv run pytest -q
uv run ruff check .
docker compose -f server/compose.yml config --quiet
```

실환경 인수 시험에서는 자동 테스트 결과와 별도로 SRS `AT-001~017`의 실행 날짜,
Hardware/OS, Image Tag, 설치 Model SHA-256과 결과물을 보관해야 한다. 설치 시 생성되는
`config/release-manifest.json`을 해당 시험 기록에 함께 첨부한다.
