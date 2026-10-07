# AI CCTV 문서 정상화 및 Edge 등록 계약 정리 계획

## 목적과 기준

이 계획은 현재 실행 코드가 기준이다. 문서는 구현보다 앞서 새 동작을 사실처럼
기록하지 않는다. Edge 자동 등록 계약을 먼저 확정·구현·검증한 뒤, 설치·사용·구조
문서를 그 계약으로 통합한다.

이번 범위는 새 제품 기능을 넓히는 일이 아니라 현재 코드·배포물·사용자 문서가 서로
모순되지 않게 만드는 일이다. 실제 장비, Docker Desktop, NVIDIA GPU, Raspberry Pi,
공개 GitHub Release 자산은 이 저장소만으로 검증할 수 없으므로 실행한 사실로 보고하지
않는다.

## 현재 구현 검증 결과

| 주제 | 확인된 사실 | 계획에 반영할 판단 |
| --- | --- | --- |
| UDP 발견 | `edge/src/ai_cctv_edge/pairing.py`의 광고는 device ID, camera ID, MAC, IP, 두 포트, profile을 JSON으로 보낸다. HMAC 또는 토큰 필드는 없다. | 발견은 신뢰 LAN의 무인증 메타데이터 교환으로 문서화한다. |
| HMAC 잔재 | 위 파일과 `server/setup/install_helper/edge_discovery.py`의 주석은 서명 검증을 언급하지만 실제 필드·검증에는 서명이 없다. `_canonical_payload`도 발견 검증에 쓰이지 않는다. | 주석·문서·불필요 코드 후보를 제거하거나 실제 의미에 맞게 고친다. HMAC 기능을 새로 추가하지 않는다. |
| 발견 테스트 | `edge/tests/test_network.py`는 삭제된 `pairing_key` 인수를 전달하고, 인터페이스가 필수인 현재 구현과도 맞지 않는다. | 문서 변경 전에 이 테스트를 현재 무서명 발견 계약으로 복구한다. |
| 신규 Edge 등록 | Edge Panel은 발견 모드와 수동 모드 모두 사용자가 32자 이상 `edge_auth_token`을 입력하게 한다. 같은 값을 Camera API와 Edge의 `PairingCompletion`에 보낸다. | 붙여넣은 목표의 “신규 자동 등록 시 사용자 토큰 입력 없음”은 아직 구현되지 않았으므로 코드·테스트 작업이 선행된다. |
| 초기 프로비저닝 | `PairingCompletion` 엔드포인트는 설정 전 Edge가 무인증으로 수락한다. 전달된 token은 완료 후 control/recovery token 파일에 저장된다. | 생성한 운영 토큰 자체는 최초 설정 요청을 인증하지 않는다. 신뢰 LAN·선택한 Edge의 ID/MAC/발신 주소 확인이라는 위협 모델을 명시하거나, 별도 일회성 bootstrap 인증을 제품 결정으로 추가해야 한다. |
| 기존 Edge 수동 등록 | `export-auth-token`과 CLI `edge-register --edge-auth-token-file`이 있다. | 기존 설정 Edge는 운영 토큰을 안전한 파일로 전달하는 수동 경로를 보존한다. |
| 토큰 보호 | UI는 password 입력, API schema는 `SecretStr`, Edge는 token 파일 mode `0600`을 사용한다. | 평문 로그·일반 응답·URL 전달을 금지하는 기존 원칙을 유지하고 회귀 테스트로 확인한다. |
| 설치 환경 파일 | 설치 도우미의 기본 배포 파일은 `C:\ProgramData\AI_CCTV\config\compose.env`이며 개발용 `server/.env`는 자동 생성되지 않는다. | 모든 사용자 문서의 소스 실행·설치 완료 후 관리 명령을 이 구분에 맞춘다. |
| 문서 구조 | Root README와 architecture에는 `Current ...` 추가 블록이 남아 있고, `deployment-guide.md`, `operations.md`, `docs/tmp/`, SVG 자산 및 component README 링크가 서로 얽혀 있다. | 내용 이관 후에만 구 문서를 삭제하고, 패키징 입력 목록을 같은 변경에서 갱신한다. |

## 확정이 필요한 보안 경계

새 Edge가 아직 비밀값을 갖지 않는다면, “서버가 발급한 운영 토큰을 Edge에 전달”하는
요청은 그 자체로 초기 요청의 송신자를 인증하지 못한다. 따라서 구현 시작 전에 아래 중
하나를 명시적으로 선택한다.

1. **신뢰 LAN 초기 설정(최소 변경 권장):** 사용자는 발견된 장치를 화면에서 선택하고,
   도우미는 새 CSPRNG 운영 토큰을 생성해 Camera API 등록과 선택한 Edge의 완료 요청에
   같은 값으로 사용한다. 최초 완료 엔드포인트는 무인증임을 문서화하고, ID·camera ID·MAC·UDP
   발신 IP와 health 응답을 대조한다.
2. **암호학적으로 인증된 초기 설정:** Edge 출고/설치 시 생성한 bootstrap secret 또는 화면에
   표시되는 일회성 코드 같은 별도 신뢰 앵커를 정의한다. 이 경우에만 최초 완료 API 인증을
   주장할 수 있으며 패키징·UX·복구 정책·키 회전 범위가 추가된다.

이 계획은 요구사항의 “현재 구현을 기준으로 최소 변경”에 맞춰 1번을 전제로 한다. 2번은
별도 기능 설계 없이는 이번 범위에 넣지 않는다.

## 목표 Edge 계약

### 신규 미설정 Edge

1. `ai-cctv-edge pair`는 토큰 없이 UDP 광고를 시작한다.
2. Server Desktop은 같은 LAN에서 광고를 받고 구조·시간·ID·MAC·발신 IP를 확인한다.
3. 사용자는 발견한 Edge와 카메라 이름·필요한 영상 설정을 확인한다. 운영 토큰 입력란은
   신규 발견 경로에 표시하지 않는다.
4. Install Helper가 `secrets.token_urlsafe(...)`로 32자 이상 운영 토큰을 생성한다.
5. 동일 토큰을 관리자 인증된 Camera API의 `edge_auth_token`과 선택 Edge의
   `PairingCompletion.edge_auth_token`에 단 한 번씩 전달한다.
6. Edge는 보호된 token 파일에 저장하고 이후 Control/Recovery Bearer 인증에 사용한다.
7. 중앙 등록 후 Edge 완료 전달이 실패하면 카메라 재등록을 유도하지 않는다. 현재의 안전한
   publish-credential handoff/재발급 안내를 유지하되, 운영 토큰을 잃지 않게 복구 절차를
   설계·테스트한다.

### 기존 설정 Edge

- 자동 발견은 재설정 수단이 아니다.
- 관리자는 `export-auth-token`으로 기존 운영 토큰을 보호된 파일에 내보내고, 수동 등록/복구
  경로에서만 해당 값을 제공한다.
- Device ID + 정규화 MAC은 Edge 식별자다. IP와 management/recovery URL은 변경 가능하며,
  MAC은 인증 비밀값이 아니다.

## 실행 순서

### 1. Edge 계약과 회귀 테스트를 먼저 정리

- `edge/src/ai_cctv_edge/pairing.py`, `server/setup/install_helper/edge_discovery.py`,
  `edge_panel.py`, `edge_pairing.py`, `server_api.py`, Camera API/Data schema의 실제 데이터
  흐름을 하나의 계약으로 기록한다.
- 신규 발견 경로에서만 Helper가 토큰을 생성하도록 UI/작업 스냅샷을 바꾸고, 수동 경로의
  명시적 token 입력·파일 기반 CLI는 유지한다.
- 서버 등록 성공/Edge 전달 실패, 전달 전 네트워크 실패, 재시도, publish credential handoff를
  테스트로 고정한다. 토큰은 로그·상태 메시지·일반 UI·URL에 포함하지 않는다.
- 무서명 광고에 맞춰 HMAC/pairing key 주석과 테스트 잔재를 제거한다. 함수·모듈의 `pairing`
  명명은 호환성에 문제가 없으면 일괄 rename하지 않는다.

**검증:** 발견 패킷 필드·파서, 토큰 없는 발견, 생성 토큰의 형식/길이, 동일 토큰의 API/완료
전달, Edge token 파일 권한, Control/Recovery Bearer 인증, 수동 등록, 실패 handoff 테스트.

### 2. 설치·릴리스 사실을 확정

- Server source의 지원 조건은 Windows 10/11 x64, Python 3.11.x, Git, `uv`, Docker Desktop
  Linux-container mode, Compose v2로 한 번만 정의한다.
- `uv sync --project server/setup/install_helper --locked`와 GUI/CLI 진입점을 실제 명령으로
  제시한다. 개발용 `server/.env`와 설치본 `config/compose.env`를 혼동하지 않는다.
- Release asset의 존재 여부는 문서 작성 시 GitHub Release 화면 또는 `gh release view`로
  확인한다. Server installer, Edge ARM64 DEB+checksum, signed APK가 모두 있는 릴리스에만
  바이너리 설치 경로를 노출하고, 없으면 검증된 source/build 경로를 우선한다.
- Edge DEB build/verify script와 Mobile Flutter README의 실제 SDK·JDK·산출물 경로를 그대로
  사용한다.

**검증:** CLI `--help`, source 환경 preflight, Edge package build/verify(ARM64 환경), Flutter
analyze/test/build(해당 SDK 환경). 실제 설치·GPU·하드웨어 결과는 별도 수동 검증으로 남긴다.

### 3. 문서를 단일 사용자 흐름으로 재작성

- Root `README.md`를 부분 append가 아닌 단일 문서로 재작성한다. 프로젝트 개요, 지원 환경,
  릴리스/소스 설치, Server·Edge·Mobile 첫 실행, 선택 의존성, 문서 링크, license만 둔다.
- `docs/user-guide.md`에 운영자가 실제 수행하는 서버 상태/로그, Desktop 로그인, 신규·수동
  Edge, 로컬 카메라, live/BBox, event media, Mobile, CPU/GPU, backup/restore/update/recovery,
  retention, troubleshooting을 통합한다.
- 실제 UI 명칭만 사용한다. Desktop은 RTSP 우선·인증 HLS fallback, Mobile은 HLS, BBox는
  클라이언트 overlay이며 RTSP/녹화 burn-in이 아님을 명시한다.
- Event의 Snapshot, Person Crop, 선택적 Annotated Snapshot, Related Recording 및 Loading/403/404
  의미를 API와 UI에 맞춰 설명한다.
- `docs/README.md`는 최종 문서 색인으로 재작성한다. 설치 도우미·Edge·Mobile·component
  README의 상대 링크도 새 목적지로 바꾼다.

**검증:** Markdown 링크 검사와 사람이 수행하는 신규 사용자 시나리오 검토. 소스 명령은 현재
package/module 구조로 실행 확인한다.

### 4. 구 문서 이관 및 구조 문서 현행화

- `deployment-guide.md`의 아직 유효한 설치/설정 내용은 Root README, Install Helper README,
  component README로 이관한다.
- `operations.md`의 사용자 운영 절차는 User Guide로 이관한다. 서비스별 진단 세부 사항은
  해당 component README에 남긴다.
- `docs/tmp/`의 실제 교체/인수 계약은 preprocessing·analysis README 및 architecture에 흡수한다.
- `docs/architecture.md`를 단일 최종 문서로 다시 쓰고 Mermaid를 canonical diagram으로 둔다.
  최소 구성은 Edge, Local Camera, Nginx, External, Data, MediaMTX, Preprocessing, Analysis,
  Server Desktop, Mobile이다. live, event/AI, discovery/control/recovery, recording/recovery
  흐름을 분리해 표현한다.
- Mermaid가 기존 SVG의 정보를 완전히 대체하고 모든 참조가 사라진 뒤에만 SVG를 삭제한다.
- 이관과 링크 수정, installer packaging 수정이 모두 끝난 뒤 `deployment-guide.md`,
  `operations.md`, `docs/tmp/`와 대체된 SVG만 삭제한다.

**검증:** 삭제 전에 역방향 참조 검색, 패키징 manifest/required-file 검사, 렌더 가능한 Mermaid
문법 검사, 링크 검사.

### 5. API 산출물·전체 검증과 인수

- 공개 API 변경이 확정된 경우에만 `python server/services/external/tools/export_openapi.py`로
  `docs/openapi.yaml`을 재생성하고 `--check`를 실행한다. UI 내부 변경만이면 명세를 임의로
  바꾸지 않는다.
- 저장소 전체에서 pairing key, discovery HMAC/signature, 삭제한 문서 경로, `Current
  installation contract`, `Current architecture`를 검색한다. 역사적 설명·테스트 fixture를
  제외하고 사용자 노출 잔재를 없앤다.
- Python 단위 테스트, 변경된 Edge/Install Helper 테스트, OpenAPI check, Markdown link check,
  가능한 source smoke를 실행한다. 실패·미실행 항목과 하드웨어 미검증 항목을 최종 보고에
  명확히 적는다.

## 삭제 및 수정 대상 목록

| 분류 | 대상 | 처리 조건 |
| --- | --- | --- |
| 재작성 | `README.md`, `docs/user-guide.md`, `docs/architecture.md`, `docs/README.md` | 구현과 명령 검증 후 단일 문서화 |
| 이관 후 삭제 | `docs/deployment-guide.md`, `docs/operations.md`, `docs/tmp/` | 유효 정보와 모든 링크·packaging 참조 이관 후 |
| 조건부 삭제 | `docs/assets/architecture/*.svg` | Mermaid 대체와 참조 0건 확인 후 |
| 반드시 수정 | `server/setup/install_helper/packaging/AI_CCTV_Server.iss`, `build_windows_installer.ps1` | 삭제 문서를 더 이상 설치본 필수 입력으로 요구하지 않게 변경 |
| 링크 갱신 | service README, Install Helper README, Edge/Mobile README | 삭제 문서를 새 canonical 문서로 연결 |
| 생성물 | `docs/openapi.yaml` | API 계약이 바뀐 경우 generator 결과만 커밋 |

## 완료 기준

- 신규 Edge 경로는 사용자의 운영 토큰 입력 없이 같은 토큰을 서버와 Edge에 저장하며, 기존
  Edge 수동 경로는 계속 작동한다.
- 발견은 token/HMAC 없이 동작하고 이 사실·신뢰 LAN 경계·초기 무인증 완료의 한계가 문서에
  명확하다.
- 사용자는 Root README만으로 실제 릴리스 상태에 맞는 설치 경로를 선택하고, User Guide만으로
  주요 기능과 운영 절차를 수행할 수 있다.
- 삭제 대상의 유효 정보는 새 문서에 있으며 링크·installer packaging·component README에
  죽은 참조가 없다.
- `docs/openapi.yaml`은 generator check를 통과하고, 변경 범위의 테스트/검증 결과와 미검증
  하드웨어 항목이 최종 보고에 구분되어 있다.

