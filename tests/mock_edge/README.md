# MP4 Mock Edge

MP4를 실시간 속도로 반복 송출하는 개발용 모의 카메라다. 중앙 MediaMTX에 H.264 영상을 보내고, 로컬에 기본 10초 MPEG-TS 조각을 저장한다. 장치 연결(Pairing)·HD/FHD 변경·상태·이벤트·녹화 복구 API를 제공한다. 실제 카메라·Pi 하드웨어·AI 모델은 모사하지 않는다.

[중앙 서버 설치](../../README.md#설치-선택)와 관리자 계정 준비를 먼저 마친다. 아래의 호스트 실행과 Docker 실행 중 하나를 선택하며, 명령은 저장소 루트에서 실행한다.

## 호스트에서 실행

저장소 루트에서 Python 3.11과 `libx264`가 포함된 FFmpeg를 준비한다.

```powershell
ffmpeg -hide_banner -encoders | Select-String libx264
py -3.11 -m venv .venv-mock-edge
.\.venv-mock-edge\Scripts\Activate.ps1
python -m pip install -r tests/mock_edge/requirements.txt
```

MP4와 32자 이상 난수로 만든 Pairing Key 파일을 준비한다. 키 파일은 **BOM 없는 UTF-8**로 저장하며 마지막 줄바꿈 외의 공백은 넣지 않는다. 예시 경로와 주소를 실제 값으로 바꾼다.

**자동 검색에는 Configurator PC의 실제 LAN IPv4 주소를 사용한다.** 아래는 중앙·Configurator·Mock Edge가 같은 PC인 예시다. `192.168.0.10`은 해당 PC의 주소로 바꾼다.

```powershell
python -m tests.mock_edge `
  --video 'C:\test-videos\sample.mp4' `
  --pairing-key-file 'C:\secure\mock-edge\pairing-key.txt' `
  --device-id mock-edge-001 --camera-id cam-001 `
  --discovery-destination 192.168.0.10
```

1. 중앙의 `RTSP_BIND_ADDRESS`가 Mock Edge에서 접근할 LAN 주소인지 확인한다.
2. Configurator의 **Central RTSP host for Edge**에 중앙 PC의 LAN 주소를, **Edge pairing / bearer key**에 같은 키를 입력한다.
3. **Discover Edge on trusted LAN** → 장치 선택 → HD/FHD 선택 → **Register Edge and camera**를 실행한다.
4. 등록된 관리·복구 주소가 Mock Edge 호스트의 LAN 주소인지 확인하고 **Query Edge status**와 중앙 영상·녹화를 확인한다.

발견 패킷의 발신 IP가 관리·복구 주소로 등록된다. `--discovery-destination 127.0.0.1`을 사용하면 Docker 내부 서비스가 자신의 loopback으로 접속하므로 관리·복구 연결이 실패한다. 다른 PC에 Configurator가 있으면 그 PC의 LAN 주소를 지정하거나 옵션을 생략해 브로드캐스트한다. 방화벽은 신뢰 LAN에서 UDP 37020, Mock Edge의 TCP 8002/8003, 중앙의 TCP 8554를 허용한다.

## 직접 등록과 컨테이너 실행

자동 검색 대신 Configurator CLI의 `edge-register`를 사용할 수 있다. [수동 연결](../../edge/README.md#수동-연결)의 **2번 중앙 등록 명령만** 사용한다. `--edge-device-id`는 `mock-edge-001`, 카메라 ID는 `cam-001`처럼 Mock Edge 실행 값과 맞춘다. `--edge-auth-token-file`에는 준비한 Pairing Key 파일을, 관리·복구 URL에는 **중앙 컨테이너에서 도달 가능한 Mock Edge 호스트 주소**를 지정한다. Pi의 토큰 내보내기·`setup` 단계는 필요 없다.

등록 명령이 생성한 `--publish-credentials-output` JSON을 준비한 뒤, 최초 Python 실행 명령에 다음을 추가한다.

```powershell
--central-host 192.168.0.10 --central-port 8554 `
--publish-credentials-file 'C:\secure\mock-edge\cam-001-publish.json' --no-discovery
```

PC에 Python·FFmpeg를 설치하지 않으려면 Docker를 사용한다. MP4·Pairing Key·중앙 등록 후 받은 JSON을 준비한다. 아래는 Windows Docker Desktop에서의 **최초 실행**이다.

```powershell
docker build -f tests/mock_edge/Dockerfile -t ai-cctv-mock-edge .
docker run --rm --init --name ai-cctv-mock-edge -p 8002:8002 -p 8003:8003 `
  -v 'C:/test-videos:/inputs:ro' -v 'C:/secure/mock-edge:/credentials:ro' `
  -v ai-cctv-mock-state:/state -v ai-cctv-mock-recordings:/recordings `
  ai-cctv-mock-edge --video /inputs/sample.mp4 `
  --pairing-key-file /credentials/pairing-key.txt --camera-id cam-001 `
  --publish-credentials-file /credentials/cam-001-publish.json `
  --central-host host.docker.internal --no-discovery `
  --state-dir /state --backup-dir /recordings
```

`host.docker.internal`은 Docker Desktop 호스트다. RTSP가 특정 LAN 주소에만 연결되어 있거나 중앙 서버가 원격이라면 그 LAN 주소를 쓴다. 브리지 컨테이너의 UDP 검색은 이 예시에서 사용하지 않는다. **호스트·Docker 모두 같은 상태로 재실행할 때는 `--central-host`와 `--publish-credentials-file` 두 옵션을 제거한다.** 저장된 설정·자격 증명을 자동으로 읽는다.

## 장애와 복구 시험

Mock Edge가 실행 중인 PC의 PowerShell에서 다음을 호출한다. 원격 실행이면 `127.0.0.1`을 Mock Edge 호스트 주소로 바꾼다.

```powershell
$pairingKey = (Get-Content -Raw -Encoding UTF8 'C:\secure\mock-edge\pairing-key.txt').TrimEnd([char[]]"`r`n")
$headers = @{ Authorization = "Bearer $pairingKey" }
$management = 'http://127.0.0.1:8003'
Invoke-RestMethod -Method Post -Uri "$management/mock/v1/simulate" `
  -Headers $headers -ContentType 'application/json' `
  -Body '{"action":"central_connection_lost"}'
```

30초 이상 지난 뒤 `action`을 `central_connection_restored`로 바꿔 호출한다. 중단 중에도 로컬 조각이 생성되고, 연결 복구 후 중앙에 누락 영상이 등록되는지 확인한다.

```powershell
Invoke-RestMethod -Uri "$management/internal/v1/status" -Headers $headers
Invoke-RestMethod -Uri "$management/internal/v1/events?limit=100" -Headers $headers
$end = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$start = [DateTime]::UtcNow.AddMinutes(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
Invoke-RestMethod -Headers $headers `
  -Uri "http://127.0.0.1:8002/v1/recovery/manifest?start=$start&end=$end"
```

위 조회는 최근 5분의 로컬 녹화 목록을 확인한다. 기간은 UTC로 한 번에 최대 24시간까지 지정할 수 있다. 기록 중인 최신 조각은 파일이 닫히고 다음 조각이 생긴 뒤 조회·다운로드할 수 있다.

| 장애 종류 | `action` |
|---|---|
| 중앙 연결 | `central_connection_lost`, `central_connection_restored` |
| 카메라 입력 | `camera_input_lost`, `camera_input_restored` |
| 배터리 | `battery_low`, `battery_critical` |
| 외부 전원 | `external_power_lost`, `external_power_restored` |
| 저장 공간 | `storage_warning`, `storage_critical` |

## 저장과 초기화

호스트 실행은 `.mock-edge/state/`에 설정·게시 비밀번호·이벤트를, `.mock-edge/recordings/`에 영상을 저장한다. Pairing으로 받은 Linux `backup_root`는 사용하지 않고 `--backup-dir`에 기록한다. Docker 예시는 두 이름 있는 볼륨에 각각 보존한다.

다른 카메라로 초기화하려면 프로세스를 먼저 종료하고 상태·녹화를 보관한 뒤 새 경로 또는 볼륨을 지정한다. 키·게시 자격 증명·상태 폴더는 저장소에 넣지 않는다. HTTP 관리·복구 API는 신뢰 LAN에서만 접근하도록 제한한다. 전체 실행 옵션은 `python -m tests.mock_edge --help`로 확인한다.
