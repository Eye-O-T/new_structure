# AI_CCTV 구현·검증 상태

기준일은 2026-08-23이다. 이 문서는 현재 작업 트리의 소스 구현과 아직 필요한 실환경 인수시험을 구분한다. 기준 요구사항과 계약은 [SRS.md](SRS.md), [ARCHITECTURE.md](ARCHITECTURE.md), [README.md](README.md), [외부 애플리케이션 연동 문서](docs/external-app-integration.md)를 따른다.

## 구현 상태

| 영역 | 현재 소스 구현 |
| --- | --- |
| 영상 Profile | 기본 `hd` 1280×720@30fps·2Mbps, 선택 `fhd` 1920×1080@30fps·4Mbps, 지원 Profile 검증과 원자적 선택 상태 저장 |
| Edge 영상 | GStreamer H.264 취득, 중앙 RTSP publish 기본, 10초 MPEG-TS 로컬 백업, 재시작 Backoff |
| Edge 관측성 | CPU·메모리·저장장치 사용률, Linux power-supply 기반 배터리·외부 전원 상태, 마지막 프레임 기반 Camera Input Watchdog |
| Edge 이벤트 | `camera_input_lost/restored`, `central_connection_lost/restored`, `external_power_lost/restored`, `battery_low/critical`, Profile 변경 성공·실패 Journal |
| Edge 서비스 분리 | Capture, 상태·제어 API, Recovery API를 별도 systemd Service로 분리하여 Capture 장애 중에도 상태·복구 경로 유지 |
| Edge HTTP | 상태·Capability·Profile·Event는 인증된 관리 API 8003, Manifest/File 복구는 인증된 Recovery API 8002 |
| 중앙 상태 | 주기적 Status Collector, Camera/Edge Runtime 상태 저장, 전원·입력·연결·저장장치 전이 Event 생성 |
| 중앙 Profile 제어 | 현재·요청·지원 Profile 분리 저장, Edge Capability 확인, 적용 성공 후 현재값 확정, 오류 코드와 실패 Event 저장 |
| 자동 복구 | 중앙 연결 중단·복구 구간을 Recovery Job으로 저장하고 제한된 재시도, SHA-256 검증, 원자 이동, `edge_recovery` 중복 방지 인덱싱 수행 |
| 중앙 미디어 | 최대 4개 활성 Camera ID 경로, MediaMTX RTSP 집약, 내부 Inference RTSP, 보호된 fMP4 HLS, 60초 fMP4 녹화와 Playback |
| Data | SQLite WAL/FK/Migration, Camera·Edge·Runtime·Profile·Recovery 상태, Recording/Event/ACL 검색, 중단 삭제 재수렴, Hook 유실 Segment 멱등 인덱싱 |
| External | JWT Access/Refresh 회전·로그아웃, RBAC/Camera ACL, Camera·Live·Recording·Event·Status·Profile·게시 자격증명 재발급 API |
| 공개 계약 | `/api/v1/openapi.json`, `/api/v1/docs`, `PUBLIC_BASE_URL` 기반 Absolute HTTPS Live/Playback URL, UTC RFC 3339, `Range`/`If-Range`, 안정된 Edge 오류 코드 |
| HLS 인증 | Browser/HLS는 HttpOnly Secure Access Cookie, REST는 Bearer, Manifest와 모든 Segment에 Nginx `auth_request`, 모호한 원본 URI 선차단 적용 |
| 내부 인증 | External·Inference·Media·Recovery별 상호 구별 Token과 Route allowlist, 내부 HTTP 환경 Proxy·Redirect 차단 |
| Configurator | GUI/CLI 공통 Config Core, 로컬 모델 경로 검증·SHA-256 원자 복사, TLS 인증서/개인키 검증·보호 복사, Docker·설정·Secret·모델 사전 점검, 초기 설치·Compose 운영과 Edge 관리 제공 |
| Windows 설치 프로그램 | PyInstaller GUI/CLI 실행 파일과 Inno Setup 정의·빌드 스크립트 제공, Program Files 코드와 ProgramData 운영 데이터 분리, 선택적 바탕 화면·PATH 등록, 업그레이드·제거 시 운영 데이터 보존 |
| Edge 설치 패키지 | ARM64 `.deb` 재현 빌드·검증 스크립트, 고정 의존성, 세 systemd Service, 인증 Token 안전 반출과 중앙 등록 절차 제공 |
| UI 범위 | Configurator는 설치·운영 설정 전용, 기존 PyQt 관제 UI는 Legacy, 모바일/Web 사용자 앱은 별도 프로젝트 |

## Edge 등록 계약

Configurator의 신규 `edge-register`는 다음 값을 모두 요구한다.

- `edge_device_id`
- `edge_management_url`: 상태·제어·이벤트 API, 기본 8003
- `edge_recovery_url`: `/v1/recovery` API, 기본 8002
- 32자 이상의 Edge Bearer Token

두 URL은 서로 독립적이며 Port를 추론하지 않는다. Token은 명령행 값으로 직접 전달하지 않고 숨김 입력 또는 권한이 제한된 `--edge-auth-token-file`을 사용한다.

```bash
ai-cctv-server edge-register cam-001 \
  --server-url https://cctv.example.com \
  --name Entrance \
  --edge-device-id edge-001 \
  --management-url http://192.0.2.41:8003 \
  --recovery-url http://192.0.2.41:8002 \
  --edge-auth-token-file /secure/edge-001-control.token \
  --publish-credentials-output /secure/cam-001-publish.json
```

일반 Camera Bootstrap은 Edge Metadata 전체를 생략할 수 있지만 일부만 포함한 신규 등록은 거부한다. 업그레이드 전 DB의 불완전 Metadata는 읽을 수 있으나 상태 제어 또는 복구가 `CAPABILITY_UNKNOWN`/`failed`이며 `edge-update`로 완성해야 한다. Edge Token과 내부 URL은 일반 Camera·Status·Profile 응답, Configurator 결과와 로그에 노출하지 않는다. 등록·재발급 응답의 일회성 RTSP 게시 자격증명은 해당 Camera만 포함한 제한 권한 JSON 파일에 원자 저장하고 화면·콘솔에는 마스킹한다. 관리자는 이 파일을 일치하는 Edge setup에 전달한 뒤 불필요한 복사본을 제거한다.

```bash
ai-cctv-server edge-rotate-credentials cam-001 \
  --server-url https://cctv.example.com \
  --publish-credentials-output /secure/cam-001-publish-rotated.json
```

## 자동 검증 상태

2026-08-23 현재 합쳐진 작업 트리에서 다음 자동 검증을 완료했다.

- `.venv\Scripts\python.exe -m pytest -q`: **181 passed**
- `.venv\Scripts\python.exe -m ruff check .`: 통과
- Python `compileall`: 통과
- `server/compose.yml`, `server/mediamtx/mediamtx.yml`, `server/config/config.example.yaml` YAML 파싱: 통과
- Edge·MediaMTX 관련 셸 스크립트 `bash -n`: 통과
- PyInstaller GUI/CLI 번들 및 Inno Setup 6.7.3 컴파일: 통과
- 패키지된 `AI_CCTV_CLI.exe --help`, GUI 번들의 PyQt 포함 여부와 Installer SHA-256 재검산: 통과
- `git diff --check`: 오류 없음(Windows CRLF 변환 안내만 출력)

현재 최종 검증 환경에는 Docker가 없다. 따라서 `docker compose config`, 실제 컨테이너 기동과 Nginx·MediaMTX 실행 검증은 수행하지 않았으며 아래 P3 운영환경 인수시험에 포함한다.
Windows `AI_CCTV_Server_Setup_0.3.0_x64.exe`와 `.sha256` 파일은 실제 생성했지만 코드 서명과 깨끗한 Windows VM 설치·업그레이드·제거 시험은 아직 수행하지 않았다. ARM64 Edge `.deb`도 빌드 정의와 정적 검증까지 완료했으며 Raspberry Pi/ARM64 빌드는 P3에 남긴다.

자동 검증은 다음 경계를 포함한다.

- HD/FHD 설정 Schema와 미지원 Profile 거부
- Edge 상태·전원·Camera Input Watchdog 상태 전이
- 관리 API 8003과 Recovery API 8002의 인증 및 생명주기 분리
- Status Collector, Runtime 상태, Event와 Recovery Job 저장
- Profile 성공 확정, 실패 오류 코드와 Rollback 유지
- Configurator의 별도 관리/복구 URL, 32자 이상 Token, Secret 마스킹
- OpenAPI 공개 경로, `PUBLIC_BASE_URL`, JWT Cookie/Bearer, HLS ACL
- 서비스별 Data Token Scope, 내부 Proxy/Redirect 차단, URI 정규화 우회 거부
- 게시 자격증명 재발급·재등록과 Camera Lifecycle fail-closed 동작
- `Range`/`If-Range`, 중단된 삭제 재수렴, Hook 유실 중앙 Segment 인덱싱
- UTC Query/응답과 Recovery 중복 방지

## P3 실환경·수동 인수시험 — 미검증

다음 항목은 실제 장비·운영 네트워크·Docker/OS 환경이 필요하며 현재 작업 환경에서는 검증 완료로 간주하지 않는다.

1. Raspberry Pi Camera에서 기본 HD 30fps·약 2Mbps와 선택 FHD 30fps·약 4Mbps의 실제 Encoder 출력, 프레임 안정성, CPU·메모리 사용률을 측정한다.
2. Camera 4대가 동시에 RTSP publish, 중앙 녹화, 내부 Inference RTSP와 외부 HLS를 유지하는지 측정한다.
3. UPS HAT의 실제 Linux power-supply 노출값을 확인하고 외부 전원 차단·복구, 충전, 저전압, 배터리 Low/Critical과 지속시간을 검증한다.
4. 카메라 케이블을 물리적으로 분리해 5초 Frame Watchdog, `camera_input_lost/restored`, Pipeline 재시작과 네트워크 장애 구분을 검증한다.
5. Ethernet 단선, Switch 전원 차단, 중앙 서버 중단을 각각 시험해 `central_connection_lost/restored`와 Edge 생존·배터리 상태가 올바르게 구분되는지 확인한다.
6. 장애 구간 자동 Recovery Job이 `detected → waiting_for_recovery → downloading → indexing → completed/failed`로 전이하고 재시도·SHA-256·중복 방지가 동작하는지 검증한다.
7. FHD 미지원 장치, Encoder 부재, Pipeline 시작 실패와 Rollback 실패를 주입해 기존 HD 유지와 Configurator 오류 표시를 확인한다.
8. 실제 HLS Player에서 로그인 Cookie, Manifest/Segment ACL, Access Token 만료 중 Refresh와 재생 복구를 검증한다.
9. 생성된 Windows Installer를 깨끗한 Windows VM에서 설치·업데이트·제거하고 신뢰 TLS 인증서를 적용한 Configurator HTTPS 연결을 검증한다.
10. ARM64 Raspberry Pi에서 `.deb`, 세 systemd Service, 권한, 재부팅 자동 시작과 Capture 장애 중 Recovery API 지속을 검증한다.
11. 운영 저장장치에서 4대 HD/FHD 연속 녹화량, 10~20% 여유, 보관·삭제·Storage Warning/Critical을 장시간 검증한다.
12. 운영 DNS·TLS·방화벽 환경에서 외부 REST/HLS/Playback, OpenAPI, Camera ACL과 내부 Port 비노출을 보안 시험한다.

## 현재 범위 밖

- 모바일 네이티브 애플리케이션과 Web UI 구현
- MQTT Broker, Telemetry, Event, Availability/LWT, retained 상태와 Command/Result
- HD/FHD 동시 Adaptive HLS
- 저장 영상 HLS VOD와 저장 파일 암호화

현재 Edge 상태·제어·복구는 인증된 HTTP를 사용하고, 영상은 Edge→중앙 및 내부 추론에 RTSP, 외부 사용자에게 HTTPS HLS/Playback을 사용한다.
