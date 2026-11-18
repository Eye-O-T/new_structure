# Pi Camera Wide 중앙 영상 미수신 조사 보고서

> 이 문서는 11:20 KST까지의 중간 조사 기록이다. 이후 Pi의 잔존 소켓과 `.sock.0` 경로 불일치를 확인했고, 소켓 정리 후 사용자가 영상 복구를 확인했다. 최신 상태·해결방안은 [종합 보고서](pi-camera-wide-stream-incident-report-2026-11-18.md)를 따른다.

- 조사 시각: 2026-11-18 10:46~10:48 KST. 아래 로그 원문의 `Z` 시각은 UTC이며 KST는 9시간을 더한다.
- 대상: 운영 서버 `C:\ProgramData\AI_CCTV`, Docker의 MediaMTX·External·Nginx, `cam-001` (`raspi_01`, `edge-001`).
- 방법: 서버 로그·설정·DB 읽기 전용 조회, 등록된 Edge의 인증된 상태 GET, MediaMTX 내부 경로 GET, TCP 연결 및 RTSP DESCRIBE 확인.
- 최초 조사에서는 운영 설정, 소스 코드, DB 및 서비스 실행 상태를 변경하지 않았다. 이후 변경은 마지막 절을 따른다.

## 결론

**Pi의 카메라 캡처와 송출 시작 요청은 활성 상태지만, 중앙 서버에는 Pi 영상이 도착하지 않는다. 서버 RTSP 수신 포트가 Pi 직결 이더넷 주소가 아닌 VirtualBox 주소에만 바인딩된 설정 문제를 확인했다.**

운영 설정과 실제 컨테이너 모두 `192.168.1.10:8554`에서만 수신한다. Pi가 연결된 물리 이더넷 주소 `192.168.50.1:8554`는 TCP 연결이 거절된다. 따라서 Pi가 직결 주소로 송출하면 현재 서버는 영상을 받을 수 없다.

다만 이번에는 Pi의 현재 `config.toml`과 Publisher 원문 로그를 읽지 않았다. **서버 수신 바인딩 문제는 확정했지만, Pi의 실제 송출 목적지가 현재도 `192.168.50.1`인지와 추가 Publisher 오류의 유무는 미확인이다.** 이 주소 문제 하나가 장애의 유일한 원인이라고 단정하지 않는다.

## 확인 결과

| 확인 항목 | 실제 결과 | 의미 |
|---|---|---|
| 서버 컨테이너 | MediaMTX·External·Data·Nginx 모두 healthy | 프로세스 가동과 Pi 영상 수신은 별개 |
| 송출·녹화 시작 API | 10:37:13, 10:40:18, 10:43:04 KST에 `POST /api/v1/cameras/cam-001/recording/start` HTTP 200 | 요청 자체는 처리됨 |
| 서버 DB의 요청 | `enabled=1`, `recording_enabled=1`, `stream_requested=1` | 사용자가 송출을 중지해 둔 상태가 아님 |
| Edge 상태 API | `http://192.168.50.2:8003/internal/v1/status` HTTP 200 | 서버에서 Pi 관리 통신 가능 |
| Edge 실시간 상태 | `camera_input_status=online`, `stream_enabled=true`, `recording_enabled=true`, `central_connection_status=offline` | 카메라 입력 정상, 중앙 연결 실패 |
| MediaMTX `cam-001` | 두 번의 조회에서 `ready=false`, `tracks=[]`, `bytesReceived=0`, 최초 조회 `source=null` | 중앙 게시 스트림 없음 |
| Pi HLS 요청 | `/hls/cam-001/index.m3u8` 반복 HTTP 404 | UI가 재생할 영상 없음 |
| 대조군 `local-camera` | `ready=true`, H264, 수신량 약 194.9 MB → 213.9 MB 증가 | 같은 MediaMTX의 영상 수신 기능은 동작 |
| 녹화 DB | Pi는 `edge_recovery/ready` 11건, Pi의 `central` 녹화 없음. 로컬 카메라는 `central/ready` 10건 | Pi 복구 녹화 파일의 존재가 중앙 실시간 수신 성공을 의미하지 않음 |

`cameras.status`는 코드에서 구형 호환 필드로 취급하므로, 이 필드의 `offline`만으로 판단하지 않았다. Edge의 현재 상태와 MediaMTX의 실제 수신 상태를 사용했다.

## 주소·포트 문제의 증거

운영 `config\compose.env`:

```text
RTSP_BIND_ADDRESS=192.168.1.10
RTSP_PORT=8554
RTSP_PUBLIC_HOST=192.168.1.10
RTSP_PUBLIC_PORT=8554
```

실제 MediaMTX 컨테이너의 Docker 포트 매핑도 동일하다.

```text
8554/tcp -> HostIp=192.168.1.10, HostPort=8554
```

Windows 어댑터 및 연결 검사:

| 주소 | 인터페이스 | TCP 8554 검사 |
|---|---|---|
| `192.168.50.1` | 이더넷 / Realtek PCIe GbE Family Controller | `ConnectionRefusedError`, Windows 오류 `10061` |
| `192.168.1.10` | 이더넷 3 / VirtualBox Host-Only Ethernet Adapter | 연결 성공 |

Windows의 실제 LISTEN 소켓도 `192.168.1.10:8554` 하나였다. 단순히 설정 파일에만 남은 값이 아니라 현재 서비스에 적용된 값이다. 이 검사는 Windows 호스트에서 수행했으며 Pi에서의 경로·방화벽 검사를 대신하지 않는다.

MediaMTX와 External 컨테이너는 10:24:05 KST에 생성되고 10:24:11 KST에 시작됐다. 설정 파일의 수정 시각은 10:23:57 KST였다. **누가 어떤 경로로 이 주소를 선택했는지는 조사 범위에서 확인하지 않았다.**

## 로그와 UI 문구의 연결

Nginx는 시작 요청을 성공으로 처리했다.

```text
2026-11-18T01:43:04.276178904Z ... "POST /api/v1/cameras/cam-001/recording/start HTTP/1.1" 200
```

이후에도 MediaMTX에는 다음 로그가 반복됐다.

```text
2026-11-18T01:46:46.429854820Z ... closed: no one is publishing to path 'cam-001'
2026-11-18T01:46:46.941021751Z ... [HLS] [muxer cam-001] destroyed: no one is publishing to path 'cam-001'
```

Nginx도 플레이리스트를 제공하지 못했다.

```text
2026-11-18T01:48:01.129976027Z ... "GET /hls/cam-001/index.m3u8 HTTP/1.1" 404
```

관리 앱은 RTSP가 Not Found이고 HLS도 404이면 `CameraOfflineError`로 처리하고 해당 안내 문구를 표시한다. 따라서 이 문구는 영상 부재를 나타내며, 카메라 하드웨어 고장이나 송출 요청 비활성화를 확정하는 진단 메시지는 아니다.

또한 시작 API는 Edge의 송출·녹화 요청 적용과 MediaMTX 설정을 확인하지만, 실제 영상 도착까지 기다린 뒤 성공하는 구조는 아니다. 경로가 준비되지 않으면 응답 상태는 `waiting`이 될 수 있다. HTTP 200과 실제 영상 재생 성공을 구분해야 한다.

## 인증 경고의 해석

External 로그에는 `action=read`, `reason=credential_mismatch`가 반복됐다. 이를 Pi 게시 자격증명 오류로 단정하지 않았다. 현재 서버의 읽기 계정으로 RTSP DESCRIBE를 수행한 결과는 다음과 같다. 계정 값은 출력·기록하지 않았다.

| 대상 | 인증 없이 요청 | 현재 읽기 계정으로 요청 |
|---|---|---|
| `cam-001` | 401 Unauthorized | 404 Not Found |
| `local-camera` | 401 Unauthorized | 200 OK |

현재 읽기 계정은 동작하며, Pi 경로는 인증 후에도 영상이 없다. 인증 없는 첫 요청에서 발생하는 경고와도 일치한다. 조사한 현재 External 로그에는 `cam-001`의 `action=publish` 인증 거부가 없었다. 따라서 Pi 게시 비밀번호 불일치를 입증할 근거는 확보되지 않았다.

## 권고 조치와 완료 기준

1. Pi의 현재 `[rtsp].central_host`, `central_port`, `mode`, 카메라 ID와 Publisher 최신 로그를 확인한다. 비밀번호를 재발급하기 전에 실제 목적지와 연결 오류부터 확인한다.
2. 직결 구성이라면 서버 `RTSP_BIND_ADDRESS`와 `RTSP_PUBLIC_HOST`, Pi의 송출 목적지를 접근 가능한 `192.168.50.1:8554`로 일치시킨다. 서버의 Docker 포트 매핑과 External 공개 RTSP 주소에도 적용돼야 한다. 재시작 시 기존 로컬 카메라 송출의 재연결 영향도 확인한다.
3. Pi에서 해당 TCP 포트 접속을 확인한 뒤 MediaMTX의 `cam-001.ready=true`, H264 트랙, 시간차를 둔 `bytesReceived` 증가를 확인한다.
4. 관리 화면의 실제 영상 재생, HLS 200, Pi의 `central_connection_status=online`, 신규 `source=central` 녹화 생성·DB 등록까지 확인한다.

최초 조사 시점에는 복구 작업을 수행하지 않았다. 이후 실행 결과는 마지막 절을 따른다.

## 관련 코드

- [Compose RTSP 포트 바인딩](../server/compose.yml): 169~173행.
- [관리 앱 영상 연결·오프라인 처리](../server/desktop/admin/video_worker.py): 76~95행, 614~621행.
- [녹화 시작 처리·상태 계산](../server/services/external/app/api/cameras.py): 124~155행, 234~298행.
- [미디어 읽기·게시 인증 분기](../server/services/external/app/api/media_auth.py): 355~395행.

## 재조사 및 적용 결과: 11:10~11:20 KST

### IP 변경이 반영되지 않은 직접 원인

사용자 세션의 제어판에는 RTSP Bind IP가 `192.168.50.1`로 입력돼 있었지만 실제 저장값과 Docker 매핑은 `192.168.1.10`이었다. 사용자의 명시적 적용 승인 후 버튼을 실행하자 다음 오류가 발생했다.

```text
TLS certificate does not exist or is not a file: C:\ProgramData\AI_CCTV\certs\tls.crt
```

현재는 HTTP 모드다. `validate_network_settings()`는 HTTP에 인증서를 요구하지 않지만, 이후 `apply_network_settings()`가 `_replace_tls_material()`을 무조건 호출해 제어판의 기본 TLS 경로를 검증했다. 없는 인증서 때문에 실패하면서 env와 YAML을 이전 값으로 롤백했다. 화면의 입력값은 새 IP로 남아 실제 적용 여부를 오인할 수 있었다.

`server/operations/network.py`에서 TLS 파일 스냅샷·교체를 HTTPS에만 수행하도록 수정했다. 누락된 기본 TLS 경로를 가진 HTTP 적용 회귀 테스트를 추가했다. 수정 전 동일 오류로 실패하는 것을 확인했고, 수정 후 네트워크 테스트 7개(HTTPS 필수 검증과 Compose 실패·시작 실패 롤백 포함)가 통과했다. `git diff --check`도 통과했다.

### 승인된 변경과 실제 검증

- 수정된 적용 함수를 새 프로세스에서 실행했다. 서버 제어판과 관제 앱도 새 설정을 읽도록 재실행했다.
- `RTSP_BIND_ADDRESS`와 `RTSP_PUBLIC_HOST`는 `192.168.50.1`, 포트는 `8554`로 저장됐다. 실제 MediaMTX Docker 바인딩도 `192.168.50.1:8554`로 변경됐고 TCP 접속에 성공했다.
- 웹 주소·웹 바인딩은 기존 `192.168.1.10`을 유지했다. Pi 게시 자격증명은 변경하지 않았다.
- 최종 상태에서 서버의 6개 서비스는 healthy였다. 이는 영상 수신 성공과 별개다.
- 서버 재구성으로 기존 로컬 Publisher 연결도 끊겼다. 사용자가 별도로 승인한 기존 송출·녹화 재연결을 관제 앱에서 실행했다. 11:19:50 KST에 `local-camera`의 H264 게시가 재개됐고, 11:19:56 KST 조회에서 `ready=true`, 수신량 `3,027,471`바이트를 확인했다.
- 같은 최종 조회에서 **Pi `cam-001`은 여전히 `ready=false`, 트랙 없음, 수신 0바이트**였다. 따라서 서버 주소 적용 오류를 고친 것만으로 Pi 장애가 해결되지는 않았다.

### 남은 확인 사항

사용자가 허용한 `phoenix@bull-sa-jo.local`에 호스트 키 검증을 유지한 SSH 접속을 시도했지만 `Permission denied (publickey,password)`로 자동 인증이 실패했다. 사용자 계정의 `.ssh`에는 known_hosts만 있고 사용할 키 파일은 확인되지 않았다. 비밀번호 추측이나 인증 우회는 수행하지 않았다.

등록된 인증 토큰을 이용한 Edge 상태 API 조회는 성공했다. 11:12:39 KST에 `capture_state=running`, `camera_input_status=online`, 송출·녹화 요청 true, `central_connection_status=offline`이었다. 현재 Pi의 실제 송출 목적지와 Publisher 오류는 아직 확보하지 못했다.

사용자에게 연결된 Pi 터미널에서 다음 두 명령의 결과를 요청했다. 비밀번호·토큰은 필요하지 않다.

```sh
sudo journalctl -u ai-cctv-edge -n 60 --no-pager
sudo grep -E '^(mode|central_host|central_port|camera_id)\s*=' /etc/ai-cctv-edge/config.toml
```

이 결과로 Pi의 목적지 불일치, RTSP 연결·인증 실패, GStreamer Publisher 오류 중 실제 원인을 구분해야 한다. 현재 상태는 **서버 설정 적용 결함 수정 완료 / Pi 송출 장애 조사 미완료**다.
