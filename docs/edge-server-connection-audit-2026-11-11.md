# Edge–Server 연결·동작 조사

조사일: 2026-11-11 (KST)  
대상 커밋: `48c187b`  
판정: **연결·동작 일부에 수정과 실기 확인이 필요하다. GUI 계획·구현은 병행할 수 있다.**

## 조사 범위와 제한

`edge/src/ai_cctv_edge`, Server의 `edge_onboarding`, Edge HTTP 클라이언트·상태 수집기·MediaMTX 인증 경로, 현재 관제 GUI의 등록 경로를 대조했다. 애플리케이션 코드는 수정하지 않았다.

현재 실행 환경은 Windows의 MSYS Python 3.11.7이며 FastAPI, pytest, httpx, PyGObject/GStreamer가 없다. 기존 Server 가상환경의 원래 Python 실행 파일도 사용할 수 없는 상태였다. 따라서 실제 FastAPI HTTP 서비스, Pi 카메라, RTSP 송출, systemd, ARM64 패키지 설치에 대한 종단 간 시험은 수행하지 않았다. 운영 중인 Server나 실제 Edge에 접속하거나 등록 정보를 변경하지 않았다.

검사는 다음을 구분해 수행했다.

- **실제 함수 실행:** 기본 discovery 호출, 생성 파이프라인 문자열, 프로필 적용 대기, 파일명 기반 복구 시각 계산.
- **격리 계약 검사:** 외부 프레임워크 import를 제외하고 원본 함수·클래스를 실행했다. Pydantic 요청 검증 및 ASGI 라우팅은 포함하지 않는다.
- **대역 사용:** HTTP 응답, 센서, GStreamer 요소 이름 생성은 대역으로 교체했다. 이 결과를 실제 네트워크·하드웨어 성공으로 해석하지 않는다.
- **소스 대조:** 양쪽 경로·필드·인증값 전달과 GStreamer 공식 구현을 확인했다.

## 확인된 결함

아래 우선순위는 수정·검증의 순서이며 시스템 전체의 치명도를 뜻하지 않는다. P1인 캡처 항목도 실제 Pi의 GStreamer 실행은 미검증이다. 옵션 누락·GUI 입력·복구 시각의 문제까지 모두 높은 우선순위로 분류했던 초기 평가는 범위에 맞게 조정했다.

| ID | 우선순위 | 문제와 영향 | 근거 |
|---|---|---|---|
| F1 | P1 | 캡처 파이프라인에 셸용 인용을 적용해 `!`가 `'!'`로 전달된다. 최초 캡처 시작부터 실패하는 코드로 판단된다. | `edge/src/ai_cctv_edge/capture.py:32` |
| F2 | P1 | 자동 생성 이름 `splitmuxsink0`, `watchdog0`, `videoconvert0`에 의존한다. 같은 프로세스에서 캡처를 다시 만들 때 요소 조회가 실패한다. | `edge/src/ai_cctv_edge/capture.py:42`, `:47` |
| F3 | P2 | CLI는 네트워크 인터페이스 생략을 허용하지만 광고 함수는 필수로 요구한다. 기본 `pair` 명령에서 검색 광고가 중단된다. `--interface` 지정으로 우회할 수 있다. | `edge/src/ai_cctv_edge/cli.py:237`, `pairing.py:128` |
| F4 | P2 | 자동 연결 패널이 현재 GUI에 연결되지 않았고, 노출된 수동 등록 폼은 필수 MAC 주소가 없어 API 클라이언트 호출부터 실패한다. | `server/desktop/admin/settings_window.py:275`, `management.py:55`, `:163`, `server/edge_onboarding/server_api.py:243` |
| F5 | P2 | 등록 전 연결 확인이 응답의 장치·카메라 ID를 비교하지 않는다. 다른 장치가 응답해도 선택한 장치를 확인한 것처럼 성공한다. | `server/edge_onboarding/pairing.py:80` |
| F6 | P2 | 송출을 의도적으로 중지한 상태에서도 프로필 적용 완료를 위해 중앙 연결 `online`을 요구한다. 화질 변경·롤백 확인이 타임아웃된다. | `edge/src/ai_cctv_edge/control.py:426` |
| F7 | P2 | 복구 세그먼트의 시각을 캡처 시작 시각과 순번만으로 계산한다. 녹화를 늦게 켜거나 중지·재개하면 실제 촬영 시각과 어긋나 장애 구간 복구가 누락될 수 있다. | `edge/src/ai_cctv_edge/runner.py:245`, `pipeline.py:48`, `recovery.py:32` |
| F8 | P2 | Edge 테스트가 현재 discovery 계약을 따라가지 못한다. 핵심 캡처·재시작·페어링·복구 경로를 검증하는 Edge 전용 테스트도 없다. | `edge/tests/test_network.py:37`, `:56`, `.github/workflows/ci.yml:45` |

### F1 — 파이프라인 문자열

현재 코드가 만드는 실제 문자열의 앞부분은 다음과 같다.

```text
libcamerasrc '!' video/x-raw,width=1280,height=720,framerate=30/1 '!' watchdog timeout=5000 ...
```

기본 publish 파이프라인에서 연결 연산자 12개가 모두 셸용 작은따옴표로 감싸졌다. 이 문자열은 셸 실행이 아니라 `Gst.parse_launch(description)`에 직접 들어간다. GStreamer 어휘 문법의 연결 연산자는 인용되지 않은 `!` 또는 `:`이다. 이 때문에 시작 실패로 판단한다. 실제 Pi에서의 GStreamer 예외 메시지는 이번 환경에서 수집하지 못했다. [공식 파서 구현](https://github.com/GStreamer/gstreamer/blob/main/subprojects/gstreamer/gst/parse/parse.l)

수정 방향은 GStreamer 파이프라인 문법과 셸 인용을 분리하는 것이다. 연산자는 그대로 두고 경로 같은 속성값을 해당 문법으로 처리하거나 요소 속성으로 설정해야 한다.

### F2 — 재생성 시 요소 이름

GStreamer 1.22의 기본 이름 생성 코드는 타입별 카운터를 증가시킨다. 파이프라인을 해제해도 다음 요소가 항상 `...0`이 되지 않는다. [공식 이름 생성 구현](https://github.com/GStreamer/gstreamer/blob/1.22/subprojects/gstreamer/gst/gstobject.c)

이 동작을 모사한 대역으로 원본 `GstCapture`를 두 번 생성했다. 첫 생성은 성공했고 두 번째 생성은 `GStreamer capture output controls were not created`로 실패했다. 실제 파서와 카메라는 대역으로 제외한 결과다.

영향받는 경로는 장애 재시도, HD/FHD 변경, UTC 날짜 변경에 따른 캡처 회전이다. `splitmuxsink`와 프레임 감시 요소에 명시적인 이름을 부여하고 그 이름으로 조회해야 한다.

### F3 — 기본 페어링 명령

`advertise_until_stopped(..., interface=None)`를 원본 함수로 호출하면 즉시 다음 예외가 발생한다.

```text
ValueError: an IPv4 interface is required for discovery
```

CLI는 광고를 daemon 스레드로 실행하므로 초기 설정 HTTP 서버는 남고 광고만 사라질 수 있다. 사용자는 페어링 대기 문구를 보지만 중앙 검색에서는 장치를 찾지 못하게 된다.

인터페이스 생략을 지원하려면 선택 정책과 실패 처리가 필요하다. 필수로 유지한다면 CLI가 서버를 띄우기 전에 누락을 거절하고 문서에도 `--interface`를 포함해야 한다.

### F4 — 현재 GUI 등록 경로

`server/desktop/admin/edge_panel.py`에는 검색·확인·설정 전달 구현이 남아 있다. 그러나 현재 화면에서는 이 클래스를 생성하는 호출이 없으며, 설정 화면은 `CameraManagement`만 연다. 문서의 “카메라 연결 → Edge 찾기”는 현재 UI와 일치하지 않는다.

또한 `CameraManagement`가 수집하는 필드에는 `edge_mac_address`가 없다. 실제 `ServerApiClient.register_edge` 시그니처에 해당 필드 집합을 바인딩하면 아래 예외가 발생한다.

```text
TypeError: missing a required argument: 'edge_mac_address'
```

이는 HTTP 요청 이전의 실패다. Server의 `CameraCreate`도 Edge 메타데이터와 MAC 주소를 함께 요구하므로, 클라이언트 인수만 선택적으로 바꾸는 방식으로 해결할 수 없다. 폼과 등록 데이터가 현재 계약을 충족해야 한다.

### F5 — 장치 식별 검증 누락

선택된 광고가 `audit-edge / audit-cam`인 상황에서 HTTP 응답을 `other-edge / other-cam`으로 교체했다. 원본 `probe_edge_connection`은 예외를 발생시키지 않고 광고에 있던 `audit-edge / audit-cam`을 성공 결과로 반환했다.

따라서 IP 재할당 등으로 다른 장치에 접속한 경우를 사전 확인에서 걸러내지 못한다. 실제 설정 전달에서는 Edge의 ID 검증에 걸려 중앙 등록만 끝난 상태가 남을 수 있다. 응답의 두 ID를 광고와 대조하고 불일치 시 등록 전에 중단해야 한다.

### F6 — 송출 중지 상태의 프로필 변경

캡처 상태 `running`, 입력 `online`, 요청 프로필·세대 일치, 송출 의도 `False`, 중앙 연결 `unknown`을 넣어 원본 `wait_for`를 실행했다. 결과는 `timeout / CONTROL_TIMEOUT`이었다.

`central_publish` 모드에서 송출 중지 시 중앙 연결을 `unknown`으로 두는 runner 동작과 적용 완료 조건이 충돌한다. 송출을 요청한 경우에만 중앙 연결 확인을 적용하고, 중지 상태에서도 캡처·요청 세대의 유효성을 확인할 수 있어야 한다.

### F7 — 복구 시각의 기준 오류

파일명은 캡처 파이프라인 시작 때 고정하고, 녹화는 valve로 나중에 켜거나 끌 수 있다. 그런데 `_segment_start`는 파일명 시각에 `순번 × 10초`를 더하므로 녹화가 꺼져 있던 시간을 반영하지 못한다.

예를 들어 00:00에 캡처를 시작하고 01:00에 첫 녹화를 켠 상황의 `...T000000..._000000.ts`는 실제 01:00 구간임에도 00:00으로 계산된다. 해당 파일명을 사용한 계산을 재현했다. 실제 TS 미디어를 생성·분석한 시험은 아니다.

복구 manifest가 이 계산값으로 요청 구간을 필터링하므로, 실제 영상이 있어도 해당 장애 구간에서 제외될 수 있다. 완료 세그먼트마다 실제 시작·종료 시각을 기록하고 이를 복구 manifest의 기준으로 사용해야 한다.

## 호환성이 확인된 부분

아래 결과는 격리 계약 검사에서 확인했으며 실기 성공을 뜻하지 않는다.

1. Edge가 만든 discovery v2 광고를 Server의 원본 parser가 수용했다. 카메라 ID, MAC 주소, 관리·복구 주소가 이어진다.
2. Server의 원본 `complete_edge_pairing`이 생성한 PUT 경로와 JSON 필드가 Edge 요청 모델의 선언과 일치했다. 유효한 입력을 원본 `PairingSession.apply`에 전달하고 임시 설정을 다시 읽었을 때 ID, 중앙 호스트·포트, 게시 비밀번호, Control/Recovery 토큰이 보존됐다. Pydantic 검증은 실행하지 않았다.
3. Edge의 정상 상태·지원 화질 응답을 Server 상태 수집기가 수용했다.
4. 오래된 캡처 heartbeat를 Edge가 `stale / camera_input=offline / CAPTURE_STATUS_STALE`로 낮추고 Server 수집기가 이를 수용했다. 관리 API의 생존과 카메라 입력 상태는 구분된다.

추가 소스 대조에서는 Control·Recovery Bearer 토큰과 게시용 계정의 전달 경로, `GET /internal/v1/status`, `PUT /internal/v1/config/capture`, 프로필·이벤트·복구 경로 사이에 이름 불일치를 발견하지 못했다. 중앙과 Edge의 기본 송출·녹화 의도는 중지이며, 등록 성공 직후 영상이 없는 현상만으로 연결 실패라고 판정해서는 안 된다.

## 검증 기록

| 검사 | 결과 | 해석 |
|---|---|---|
| Edge Python 구문 분석 | 소스·테스트 17개 통과 | import·장치 실행 검증은 아님 |
| 기존 Edge 테스트의 일반 실행 | FastAPI import에서 중단 | 전체 테스트 통과 주장 불가 |
| 기존 네트워크 테스트를 관련 함수만 격리해 실행 | 4개 중 2개 성공, 2개 `pairing_key` TypeError | 현재 구현과 테스트 불일치 |
| 기존 Edge 진단 스크립트 | 기본 discovery 예외, 인용 문자열, 재생성 실패, 프로필 타임아웃, 복구 시각 오류 재현 | 위 각 항목의 제한 범위 적용 |
| 추가 양방향 계약 검사 | 호환성 검사 4개 확인, 결함 재현 2개 확인 | 실제 HTTP 서버·카메라 미사용 |

로컬 재현 스크립트는 Git에서 제외되는 `.review/edge_review_checks.py`와 `.review/edge_server_contract_checks.py`에 있다. 이들은 관측된 결함을 기대 결과로 확인하는 진단이며, 애플리케이션의 정상 동작을 보장하는 회귀 테스트가 아니다.

```powershell
python .review/edge_review_checks.py
python .review/edge_server_contract_checks.py
```

## 병행할 수정·검증 항목

이 문서는 조사 보고서다. 후속 요청에 따라 [Server GUI 수정 계획](server-edge-gui-interface-plan.md)과 [페어링 절차서](edge-server-pairing-guide.md)를 별도로 작성했다. 아래 항목은 GUI 계획 작성을 막는 조건이 아니라 실제 영상·복구 동작을 확인할 검증 과제다.

다음 검증을 함께 진행한다.

1. F1·F2를 수정하고 실제 GStreamer에서 최초 시작과 반복 재생성을 확인한다. 장애 재시도·화질 변경·날짜 회전도 포함한다.
2. F3·F5를 수정하고 인터페이스 누락·ID 불일치가 중앙 등록 전에 검출되는지 확인한다. 정상 discovery → 등록 → 설정 전달 → 서비스 시작도 Pi에서 확인한다.
3. F6·F7을 수정하고 송출 중지 중 화질 변경, 녹화 지연 시작·중지·재개 후 구간별 복구와 해시 검증을 확인한다.
4. 테스트를 현재 계약에 맞추고 관리 API·RTSP·로컬 백업·중앙 상태 갱신을 함께 검증한다. 등록 완료와 영상 수신 완료를 각각 확인한다.

## 현재 가능한 짧은 점검 절차

서버는 현재처럼 Server Manager에서 실행하고 ready 상태를 확인한다. 신규 미등록 Pi에서만 다음 명령을 실행한다. `eth0`는 실제 IPv4 인터페이스 이름으로 바꾼다.

```bash
sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --interface eth0
```

서버 PC의 별도 PowerShell에서 검색한다.

```powershell
.\ai_cctv.ps1 cli edge discover --timeout 5
```

이미 등록된 Edge는 `pair`를 다시 실행하지 않는다. Pi에서는 `sudo ai-cctv-edge status`, `doctor`, `logs`로 점검하고, PC에서는 다음 명령으로 관제 화면을 연다.

```powershell
.\ai_cctv.ps1 cli admin
```

관제 화면의 **설정 → 로그인 → 카메라 등록 · 운영 설정 → 목록 새로고침 → 카메라 선택 → 선택한 카메라 상태 · 지원 화질 조회**를 사용한다. `edge_online`, `camera_input`, `central_connection_status`, `last_seen_at`, `last_error_code`를 함께 확인한다. 송출을 중지했다면 중앙 연결 `unknown`은 정상일 수 있다.

이 절차는 검색·상태 점검까지이며, 현재 결함이 해결된 연결 완료 절차로 해석해서는 안 된다. 실제 페어링과 영상 수신 성공 여부는 수정 후 대상 Pi에서 별도로 검증해야 한다.
