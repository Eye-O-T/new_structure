# Pi Camera Wide 영상 미수신 재발 조사 — 2026-11-18

- 조사 시각: 12:12 KST 이후(UTC+9). 사용자 Pi 출력 시각 12:14:44 포함.
- 상태: **Pi의 카메라 미검출 확인. 서버 주소와 Pi 목적지는 정상. 미검출의 하위 원인은 커널 로그 확인 대기.**
- 범위: 읽기 전용 서버 로그·컨테이너·DB·등록된 Edge 상태 API 조회 및 관련 소스 확인. 이번 재발 조사에서는 설정 변경·서비스 재시작·소켓 삭제를 수행하지 않았다.
- 이전 기록: [최초 장애 및 복구 종합 보고서](pi-camera-wide-stream-incident-report-2026-11-18.md)

## 1. 현재 결론

관제 화면의 영상 부재는 재발했지만, **현재 관측 상태는 이전의 Publisher 단독 실패와 다르다.**

이전에는 Pi가 카메라 입력 online과 중앙 연결 offline을 보고했고, 잔존 `.sock`과 활성 `.sock.0`의 경로 불일치를 확인했다. 이번에는 Edge가 `capture_state=error`, `last_error_code=PIPELINE_START_FAILED`를 보고한다. 사용자 traceback으로 `GstCapture.__init__()`의 PLAYING 전환 실패를 확인했고, 서비스 계정과 일반 사용자 양쪽의 `rpicam-hello --list-cameras`가 모두 `No cameras available!`을 반환했다.

**이번 직접 장애는 Pi의 libcamera/rpicam 경로에서 카메라가 검출되지 않아 캡처를 시작하지 못하는 상태다.** 소켓 파일과 LISTEN 소켓도 없으므로 이전에 확인한 잔존 소켓 경로 불일치 상태는 이번 출력에서 관측되지 않는다. 서버 및 Pi의 목적지 주소는 모두 `192.168.50.1:8554`로 확인됐다. 카메라 미검출을 일으킨 케이블·센서 연결, 부팅 시 드라이버·장치 설정 등의 하위 원인은 아직 확정하지 않았다.

## 2. 서버에서 직접 확인한 사실

| 항목 | 결과 | 판단 |
|---|---|---|
| 서버 서비스 | 6개 컨테이너 healthy | 서버 프로세스 가동 중 |
| RTSP 실제 포트 매핑 | `192.168.50.1:8554 -> 8554/tcp` | 이전 주소로 되돌아간 상태가 아님 |
| `cam-001` 경로 | `ready=false`, `tracks=[]`, `bytesReceived=0`, `source=null` | Pi 게시 영상 없음 |
| `local-camera` 경로 | H264, `ready=true`, 수신량 33,643,707바이트 | 같은 MediaMTX가 로컬 영상은 수신 중 |
| Pi 관리 API | HTTP 200 | Pi 관리 서비스는 서버에서 접근 가능 |
| Pi 12:12:40 상태 | `capture_state=error`, `camera_input_status=offline`, `central_connection_status=unknown`, `PIPELINE_START_FAILED` | Edge 파이프라인 실패 상태 |
| Pi 12:13:30 상태 | 동일 오류 유지, 상태 파일 갱신 시각 12:13:11 | 일회성 화면 오류로 볼 근거 부족 |

Edge 상태 API의 `camera_input_status=offline`은 캡처 상태가 error일 때 보정되는 값이기도 하다. 이 값만으로 카메라 케이블 분리나 센서 고장을 확정할 수 없다.

## 3. 로그·요청 타임라인

아래 시각은 서버 로그의 UTC를 KST로 변환했다.

| 시각(KST) | 관측 |
|---|---|
| 12:07:23 | 이미 `no one is publishing to path 'cam-001'` 기록 |
| 12:07:52 | `POST /api/v1/cameras/cam-001/recording/start` HTTP 200 |
| 12:09:07 | External이 Data 연결 실패를 기록하고 종료 과정으로 진입 |
| 12:09:17 | MediaMTX 시작 시각. 현재 조회 당시 서비스들은 재시작 후 가동 중 |
| 12:09:20 | External 재시작·기동 완료 로그 |
| 12:10:45 | Pi 카메라 녹화·송출 시작 요청 HTTP 200 |
| 12:10:50 | 로컬 카메라 H264 게시 재개 |
| 12:12:11 | Pi 상태 파일에 파이프라인 실패 상태 갱신 |
| 12:12:41 | Pi 카메라 중지 요청 HTTP 200 |
| 12:13:11 | Pi 카메라 시작 요청 HTTP 200. 이후 요청값 true 확인 |
| 12:13:30 | Pi는 여전히 `capture_state=error`, `PIPELINE_START_FAILED` |

12:12 첫 DB 조회에서는 송출·녹화 요청이 false였고, 다음 조회에서는 true였다. 이 사이 실제 stop/start API 요청이 있었으므로 서로 다른 시점의 값으로 구분한다. 마지막 시작 요청 이후에도 파이프라인 오류가 유지됐다.

Data 연결 실패는 서버 재시작 구간에서 관측됐으며, Pi 게시자 부재는 그보다 앞선 12:07부터 기록돼 있다. 이 서버 재시작만을 전체 장애의 시작 원인으로 단정하지 않는다.

## 4. 현재 가설과 필요한 증거

| 후보 | 현재 근거 | 확정에 필요한 증거 |
|---|---|---|
| 잔존 소켓·경로 불일치 재발 | 앞선 장애에서 확인됐고 Edge 영구 수정은 아직 없음 | Pi 소켓 파일 목록, 실제 LISTEN 경로, 최초 GStreamer 오류 |
| 카메라 장치 점유·입력 초기화 실패 | 이번에는 캡처 상태 자체가 error | journal의 libcamera·장치 관련 원문 오류. 필요 시 그 후 점유 프로세스 확인 |
| GStreamer 요소·설정·녹화 경로 등 시작 오류 | `PIPELINE_START_FAILED`는 시작 중 일반 예외를 묶는 코드 | Python traceback의 실제 예외 메시지와 해당 요소·경로 |
| 서버 RTSP 주소 회귀 | 현재 포트 매핑은 정상 주소 | 현재 증거는 지지하지 않음. Pi 목적지는 별도 확인 필요 |
| Pi 게시 자격증명 불일치 | 이번 조사 로그에서 Pi의 publish 인증 거부는 확인되지 않음 | Pi RTSP 오류와 같은 시각의 서버 publish 인증 거부. 현재 확정 근거 없음 |

관련 소스에서 [Runner](../edge/src/ai_cctv_edge/runner.py)는 시작 중 일반 예외를 `PIPELINE_START_FAILED`로 보고한다. [캡처 파이프라인](../edge/src/ai_cctv_edge/pipeline.py)과 [Publisher](../edge/src/ai_cctv_edge/publisher.py)는 원래 `.sock` 경로를 사용하고 있으며, 이전에 계획한 잔존 소켓 자동 정리는 아직 구현되지 않았다. 다만 이것이 이번 실패에도 실제로 작용했는지는 Pi 출력으로 확인해야 한다.

## 5. 사용자에게 요청한 Pi 확인 명령

**재시작하거나 소켓을 삭제하기 전에** 현재 상태를 아래 명령으로 보존한다. 명령은 조회용이며 설정을 바꾸지 않는다.

```bash
date -Is
sudo journalctl -u ai-cctv-edge -n 120 --no-pager -o short-iso
sudo ls -la /run/ai-cctv-edge/
sudo ss -xlpn | grep -F 'cam-001.h264.sock'
sudo grep -E '^(mode|central_host|central_port|camera_id)\s*=' /etc/ai-cctv-edge/config.toml
```

요청한 자료는 오류·파일 경로·목적지 주소다. 전체 설정 파일이나 비밀번호·토큰은 필요하지 않다. `ss` 결과가 없더라도 해당 명령이 실패했다기보다 일치하는 LISTEN 소켓이 없는 결과일 수 있으므로, 출력이 없었다는 사실도 함께 전달한다.

위 명령 결과는 이후 확보했으며 다음 절에 반영했다. 기존의 소켓 삭제 절차를 원인 확인 없이 반복 실행하도록 안내하지 않는다.

## 6. 사용자 제공 자료로 확인한 실패 지점

### 6.1 캡처 생성 중 실패하며 Publisher 시작 이전에 중단됨

사용자가 제공한 12:11:11~12:14:41 KST 로그는 같은 Runner PID 1789에서 다음 경로로 반복 실패한다.

```text
runner.py: run -> _start_children
runner.py: self.capture = GstCapture(...)
capture.py: __init__
RuntimeError: GStreamer capture pipeline could not start
```

이는 앞선 장애의 `publisher.py: GStreamer publisher could not enter PLAYING state`와 다른 실패 지점이다. 소스상 캡처를 생성한 뒤 Publisher를 시작하므로, 이번 traceback의 시도에서는 Publisher 시작 단계까지 도달하지 못한다. 재시도 간격은 1·2·4·8·16초 이후 약 30초로 늘어나는 감독 루프와 일치한다.

현재 캡처 코드도 초기 상태 전환 실패 시 GStreamer bus의 상세 원인을 읽기 전에 일반 RuntimeError를 발생시킨다. 따라서 traceback만으로 센서·권한·인코더 등의 하위 실패 요소를 특정하기 어렵다는 진단상 결함은 남아 있다.

### 6.2 이전의 잔존 소켓과 주소 문제는 현재 관측되지 않음

12:14:44 Pi 출력에서 `/run/ai-cctv-edge/`에는 `cam-001.lock`만 있었으며 `.sock`과 `.sock.0`은 없었다. `ss`에도 해당 LISTEN 출력이 없었다. 캡처가 시작되지 않으면 공유 메모리 소켓도 준비되지 않을 수 있으므로, 소켓 부재를 별개의 확정 원인으로 취급하지 않는다.

현재 Pi 설정은 다음과 같이 확인됐다.

```toml
camera_id = "cam-001"
mode = "central_publish"
central_host = "192.168.50.1"
central_port = 8554
```

따라서 이번에는 IP를 다시 바꾸거나 기존 소켓을 삭제할 근거가 없다.

### 6.3 서비스 계정·일반 사용자 모두 카메라 미검출

추가로 사용자가 실행한 결과:

```text
sudo -u ai-cctv-edge rpicam-hello --list-cameras
No cameras available!

rpicam-hello --list-cameras
No cameras available!
```

카메라 미검출은 Edge 서비스 계정만의 현상이 아니다. 서버 UI나 RTSP 송출 이전의 Pi 카메라 탐지 계층부터 정상화해야 한다. 다만 이 결과만으로 카메라 모듈 자체의 고장, 케이블 불량, 부팅 설정 변경 중 하나를 확정할 수는 없다.

## 7. 다음 확인과 조치 판단

사용자에게 다음 조회 명령 하나의 결과를 요청했다.

```bash
sudo dmesg -T | grep -Ei 'imx708|imx219|camera|csi|cfe|unicam|i2c|pisp' | tail -n 100
```

- 센서 probe·I2C 통신 실패가 보이면 해당 오류와 실제 카메라 연결 상태를 대조한다.
- 카메라 관련 로그가 없거나 설정 문제가 의심되면 다음 단계에서 부팅 설정의 `camera_auto_detect`·카메라 overlay 적용 여부를 확인한다. 아직 임의로 설정을 변경하지 않는다.
- 물리 연결 확인이 필요하면 Pi를 정상 종료하고 전원을 분리한 뒤 케이블·잠금 상태를 점검한다. 전원이 연결된 상태에서 카메라 케이블을 분리하지 않는다.
- 연결·설정 조치 후 두 사용자 계정에서 카메라가 목록에 나타나는지 먼저 확인하고, 이후 Edge 캡처·중앙 영상·녹화 순서로 검증한다.

카메라 탐지와 연결 점검의 참고 자료: [Raspberry Pi 공식 카메라 문서](https://www.raspberrypi.com/documentation/accessories/camera-software.html). 이 자료는 후속 점검 방향의 근거이며, 현재 장치의 구체적인 미검출 원인을 대신 증명하지 않는다.
