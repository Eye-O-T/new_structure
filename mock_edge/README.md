# MP4 Mock Edge

이 도구는 Windows 한 대에서 AI_CCTV 중앙 서버와 Edge 연동을 시험하기 위한
테스트 전용 Edge입니다. 상위 폴더의 `mp4_rtsp2_loop_sender`에서 사용한
`FFmpeg -stream_loop -1 -re` 방식을 참고하여 MP4를 실시간 속도로 반복 재생합니다.
다만 기존 Loop Sender처럼 자체 RTSP 서버를 열지 않고, 현재 아키텍처에 맞게 중앙
MediaMTX의 `rtsp://<central>:8554/<camera_id>`로 H.264 영상을 직접 Publish합니다.

## 목차

- [지원 범위](#지원-범위)
- [준비 사항](#준비-사항)
- [Configurator Pairing 방식](#configurator-pairing-방식)
- [직접 설정 방식](#직접-설정-방식)
- [장애와 복구 시험](#장애와-복구-시험)
- [API와 포트](#api와-포트)
- [저장 파일과 초기화](#저장-파일과-초기화)
- [제한과 보안](#제한과-보안)

## 지원 범위

- Configurator와 호환되는 HMAC-SHA256 UDP 발견 광고(기본 UDP 37020)
- `PUT /internal/v1/pairing/complete` 1회 Pairing
- MP4 반복 재생 및 중앙 MediaMTX RTSP/1.0 TCP Publish
- `hd`(1280×720, 30fps, 2Mbps)와 `fhd`(1920×1080, 30fps, 4Mbps)
- 상태, 영상 Capability, Profile 변경, Event Journal API
- 중앙 연결 장애 중에도 별도 FFmpeg 프로세스로 10초 MPEG-TS 로컬 조각 생성
- Recovery Manifest, SHA-256, 조각 다운로드 API
- 중앙 연결·카메라 입력·전원·배터리·저장 공간 장애 주입

사람 검출이나 의상·성별·나이 추론은 Mock Edge가 하지 않습니다. Mock Edge가
MediaMTX에 게시한 영상을 중앙 Inference Service가 읽어 추론합니다.

## 준비 사항

Windows 10/11, Python 3.11, FFmpeg가 필요합니다. FFmpeg 빌드에는 `libx264` Encoder가
포함되어야 합니다.

```powershell
ffmpeg -hide_banner -encoders | Select-String libx264
py -3.11 -m venv .venv-mock-edge
.\.venv-mock-edge\Scripts\Activate.ps1
python -m pip install -r mock_edge\requirements.txt
```

32자 이상의 Pairing Key를 UTF-8 텍스트 파일 하나에 저장합니다. 앞뒤 공백은 넣지
말고 마지막 줄바꿈만 허용합니다. 이 값은 Configurator에 입력할 값과 같아야 합니다.

테스트 영상은 아무 MP4나 지정할 수 있습니다. 이전에 언급한 Loop Sender의 영상이
상위 폴더에 있다면 다음 경로를 그대로 사용할 수 있습니다.

```text
..\mp4_rtsp2_loop_sender\testvideofile.mp4
```

중앙 서버의 MediaMTX TCP 8554 포트가 Windows Host에 공개되어 있어야 합니다.
중앙 서버와 Mock Edge가 같은 PC에 있으면 Edge가 접근할 중앙 Host는 보통
`127.0.0.1`입니다.

## Configurator Pairing 방식

저장소 루트에서 다음 명령을 실행합니다. `pairing-key.txt`는 실제 Key 파일 경로로
바꿉니다.

```powershell
python -m mock_edge `
  --video "..\mp4_rtsp2_loop_sender\testvideofile.mp4" `
  --pairing-key-file ".\pairing-key.txt" `
  --device-id "mock-edge-001" `
  --camera-id "cam-001" `
  --discovery-destination "127.0.0.1"
```

같은 PC의 Configurator에서는 다음 순서로 연결합니다.

1. `Central RTSP host for Edge`에 `127.0.0.1`을 입력합니다.
2. Mock Edge와 같은 Pairing Key를 입력합니다.
3. **Discover Edge on trusted LAN**을 누릅니다.
4. `mock-edge-001 / cam-001`을 선택합니다.
5. `hd` 또는 `fhd`를 선택하고 **Register Edge and camera**를 누릅니다.
6. **Query Edge status**에서 Camera Input과 Central Connection을 확인합니다.

다른 PC에서 시험할 때는 `--discovery-destination`을 생략해 Broadcast를 사용하고,
중앙 Host에는 Mock Edge가 접근 가능한 중앙 PC의 LAN IPv4 주소를 입력합니다. Windows
방화벽에서 UDP 37020, TCP 8002/8003과 중앙 RTSP TCP 8554가 허용되어야 합니다.

Configurator가 전달하는 Linux용 `backup_root` 값은 계약 검증에는 사용하지만 Windows
파일 경로로 쓰지 않습니다. 실제 Mock Edge 조각은 `--backup-dir`에 저장됩니다.

## 직접 설정 방식

이미 Camera를 중앙 서버에 등록했고 Configurator가 만든 일회성 Publish 자격증명
파일이 있다면 UDP 발견과 Pairing을 생략할 수 있습니다. 파일 형식은 다음과 같습니다.

```json
{
  "camera_id": "cam-001",
  "username": "cam-001",
  "password": "<16자 이상의 중앙 발급 비밀번호>"
}
```

```powershell
python -m mock_edge `
  --video "..\mp4_rtsp2_loop_sender\testvideofile.mp4" `
  --pairing-key-file ".\pairing-key.txt" `
  --camera-id "cam-001" `
  --central-host "127.0.0.1" `
  --central-port 8554 `
  --publish-credentials-file ".\cam-001-publish-credentials.json" `
  --no-discovery
```

직접 설정은 “중앙 Camera 등록”을 대신하지 않습니다. 중앙 MediaMTX에 이미 같은
Camera ID와 Publish 자격증명이 등록되어 있어야 합니다.

## 장애와 복구 시험

먼저 Pairing Key를 Header로 준비합니다.

```powershell
$pairingKey = (Get-Content -Raw ".\pairing-key.txt").TrimEnd([char[]]"`r`n")
$headers = @{ Authorization = "Bearer $pairingKey" }
```

중앙 Publish만 중단하고 로컬 MPEG-TS 기록은 계속하려면 다음 요청을 보냅니다.

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8003/mock/v1/simulate" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"action":"central_connection_lost"}'
```

30초 이상 기다린 뒤 다음 요청으로 Publish를 복구합니다.

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8003/mock/v1/simulate" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"action":"central_connection_restored"}'
```

Event Journal과 상태는 다음처럼 확인합니다.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8003/internal/v1/status" -Headers $headers
Invoke-RestMethod -Uri "http://127.0.0.1:8003/internal/v1/events?limit=100" -Headers $headers
```

지원하는 `action`은 다음과 같습니다.

- `central_connection_lost`, `central_connection_restored`
- `camera_input_lost`, `camera_input_restored`
- `battery_low`, `battery_critical`
- `power_disconnected`, `power_restored`
- `storage_warning`

복구 Manifest는 UTC ISO 8601 시간 범위로 조회합니다. 최대 범위는 24시간입니다.

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8002/v1/recovery/manifest?start=2026-08-24T00:00:00Z&end=2026-08-24T01:00:00Z" `
  -Headers $headers
```

현재 Recorder가 쓸 수 있는 가장 최신 `.ts` 파일은 Manifest와 다운로드에서 제외됩니다.
파일이 닫힌 다음 조각이 만들어지면 이전 파일이 복구 대상으로 나타납니다.

## API와 포트

| 구분 | 기본 주소 | 인증 |
| --- | --- | --- |
| UDP 발견 광고 | `UDP 37020` | Pairing Key HMAC-SHA256 |
| Pairing/Management | `http://0.0.0.0:8003` | Pairing Key Bearer |
| Recovery | `http://0.0.0.0:8002` | Pairing Key Bearer |
| 중앙 RTSP Publish | `rtsp://<central>:8554/<camera_id>` | Camera별 Publish 자격증명 |

공식 Edge 호환 Route는 다음과 같습니다.

- `GET /health/live`
- `PUT /internal/v1/pairing/complete`
- `GET /internal/v1/status`
- `GET /internal/v1/capabilities/video`
- `PUT /internal/v1/config/video-profile`
- `GET /internal/v1/events?after=<event_id>&limit=<1..1000>`
- `GET /v1/recovery/manifest?start=<UTC>&end=<UTC>`
- `GET /v1/recovery/files/<relative_path>`

`POST /mock/v1/simulate`만 실제 Edge에는 없는 테스트 전용 Route입니다.

## 저장 파일과 초기화

기본 저장 위치는 다음과 같습니다.

```text
.mock-edge/
├── state/
│   ├── .configured
│   ├── mock-edge.json
│   ├── publish.password
│   └── events-cam-001.jsonl
└── recordings/
    └── cam-001/YYYY/MM/DD/*.ts
```

재실행하면 저장된 중앙 주소와 자격증명을 읽어 같은 Camera로 자동 Publish합니다.
다른 Camera로 다시 Pairing하려면 Mock Edge를 먼저 종료하고 `.mock-edge`를 별도 이름으로
옮겨 보관한 뒤 새 상태 디렉터리로 실행합니다.

## 제한과 보안

이 도구는 개발·통합 시험 전용이며 운영 Edge로 사용하면 안 됩니다.

- Camera 장치, GStreamer, Raspberry Pi 하드웨어 상태는 모사하지 않습니다.
- HTTP 관리 API와 로컬 Publish 비밀번호 파일은 테스트 편의를 위한 구성입니다.
- Pairing Key와 Publish 자격증명을 Git, 로그, 화면 공유 자료에 포함하지 마십시오.
- FFmpeg 오류 로그에서는 Publish 비밀번호를 가리지만, 상태 디렉터리 자체는 OS 파일
  권한으로 보호해야 합니다.
- `--management-bind`와 `--recovery-bind`는 필요하지 않으면 `127.0.0.1`로 제한하십시오.
