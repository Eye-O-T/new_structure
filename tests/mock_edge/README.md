# MP4 Mock Edge

MP4를 실시간 속도로 반복 송출하는 개발용 모의 카메라다. 중앙 MediaMTX에 H.264 영상을 보내고, 로컬에 기본 10초 MPEG-TS 조각을 저장한다. 장치 연결(Pairing)·HD/FHD 변경·상태·이벤트·녹화 복구 API를 제공한다. 실제 카메라·Pi 하드웨어·AI 모델은 모사하지 않는다.

[중앙 서버 설치](../../README.md#설치하기)와 관리자 계정 준비를 먼저 마친다. 아래의 호스트 실행과 Docker 실행 중 하나를 선택하며, 명령은 저장소 루트에서 실행한다.

## 호스트에서 실행

저장소 루트에서 Python 3.11과 `libx264`가 포함된 FFmpeg를 준비한다. 아래 호스트 명령은 Windows PowerShell용이다. 첫 명령에서 `libx264` 인코더가 표시되는지 확인한다.

```powershell
ffmpeg -hide_banner -encoders | Select-String libx264
py -3.11 -m venv .venv-mock-edge
.\.venv-mock-edge\Scripts\Activate.ps1
python -m pip install -r tests/mock_edge/requirements.txt
```

가상환경 활성화가 실행 정책으로 차단되면 `Activate.ps1`을 생략하고 아래의 `python`을 `.\.venv-mock-edge\Scripts\python.exe`로 바꿔 실행할 수 있다. FFmpeg가 PATH에 없으면 Mock Edge 실행 시 `--ffmpeg 'C:\tools\ffmpeg\bin\ffmpeg.exe'`처럼 실제 실행 파일 경로를 지정한다.

MP4와 32자 이상 난수로 만든 Pairing Key 파일을 준비한다. 키 파일은 **BOM 없는 UTF-8**로 저장하며 마지막 줄바꿈 외의 공백은 넣지 않는다. 예시 경로와 주소를 실제 값으로 바꾼다.

**자동 검색에는 설치 도우미 PC의 실제 LAN IPv4 주소를 사용한다.** 아래는 중앙·설치 도우미·Mock Edge가 같은 PC인 예시다. `192.168.0.10`은 해당 PC의 주소로 바꾼다.

```powershell
python -m tests.mock_edge --video 'C:\test-videos\sample.mp4' --pairing-key-file 'C:\secure\mock-edge\pairing-key.txt' --device-id mock-edge-001 --camera-id cam-001 --discovery-destination 192.168.0.10
```

명령은 포그라운드에서 계속 실행된다. 설치 도우미는 별도로 열고, 장애 시험 명령은 새 PowerShell에서 실행한다. 종료는 `Ctrl+C`다. 송출과 로컬 녹화는 등록 설정을 받은 뒤 시작한다.

1. 중앙의 `RTSP_BIND_ADDRESS`가 Mock Edge에서 접근할 LAN 주소인지 확인한다.
2. 설치 도우미 관리 화면의 **카메라 연결**에서 관리자 계정·비밀번호와 같은 장치 연결 키를 입력한다. 고급 설정의 중앙 RTSP 주소가 중앙 PC의 LAN 주소인지 확인한다.
3. **Edge 찾기** → 장치 선택 → **선택한 Edge 연결**을 실행한다. 필요하면 **연결 확인**으로 먼저 점검하고 고급 설정에서 HD/FHD 화질을 선택한다.
4. 등록된 관리·복구 주소가 Mock Edge 호스트의 LAN 주소인지 확인한다. 서버 관리자 화면에서 카메라를 선택하고 **장치 상태 · 지원 화질 새로고침**으로 연결 상태를 확인한 뒤 Android 앱에서 실시간 영상과 이벤트에 연결된 녹화를 확인한다.

발견 패킷의 발신 IP가 관리·복구 주소로 등록된다. `--discovery-destination 127.0.0.1`을 사용하면 Docker 내부 서비스가 자신의 loopback으로 접속하므로 관리·복구 연결이 실패한다. 다른 PC에 설치 도우미가 있으면 그 PC의 LAN 주소를 지정하거나 옵션을 생략해 브로드캐스트한다. 방화벽은 신뢰 LAN에서 UDP 37020, Mock Edge의 TCP 8002/8003, 중앙의 TCP 8554를 허용한다.

## 직접 등록과 컨테이너 실행

자동 검색 대신 설치 도우미 CLI의 `edge-register`를 사용할 수 있다. [수동 연결](../../edge/README.md#수동-연결)의 **2번 중앙 등록 명령만** 사용한다. `--edge-device-id`는 `mock-edge-001`, 카메라 ID는 `cam-001`처럼 Mock Edge 실행 값과 맞춘다. `--edge-auth-token-file`에는 준비한 Pairing Key 파일을, 관리·복구 URL에는 **중앙 컨테이너에서 도달 가능한 Mock Edge 호스트 주소**를 지정한다. Pi의 토큰 내보내기·`setup` 단계는 필요 없다.

등록 명령이 생성한 `--publish-credentials-output` JSON을 준비한 뒤, 최초 Python 실행 명령에 다음 옵션을 추가한다. 이미 등록 대기 중인 Mock Edge가 있으면 먼저 `Ctrl+C`로 종료한다. 경로에 공백이 있으면 따옴표로 감싼다.

| 옵션 | 예시 값 |
|---|---|
| `--central-host` | `192.168.0.10` |
| `--central-port` | `8554` |
| `--publish-credentials-file` | `'C:\secure\mock-edge\cam-001-publish.json'` |
| `--profile` | `hd` 또는 `fhd` (생략 시 `hd`) |
| `--no-discovery` | 값 없이 지정 |

PC에 Python·FFmpeg를 설치하지 않으려면 Docker를 사용한다. MP4·Pairing Key·중앙 등록 후 받은 JSON을 준비한다. 아래는 Windows Docker Desktop에서의 **최초 실행**이다.

```powershell
docker build -f tests/mock_edge/Dockerfile -t ai-cctv-mock-edge .
docker run --rm --init --name ai-cctv-mock-edge -p 8002:8002 -p 8003:8003 -v 'C:/test-videos:/inputs:ro' -v 'C:/secure/mock-edge:/credentials:ro' -v ai-cctv-mock-state:/state -v ai-cctv-mock-recordings:/recordings ai-cctv-mock-edge --video /inputs/sample.mp4 --pairing-key-file /credentials/pairing-key.txt --device-id mock-edge-001 --camera-id cam-001 --publish-credentials-file /credentials/cam-001-publish.json --central-host host.docker.internal --no-discovery --state-dir /state --backup-dir /recordings
```

`host.docker.internal`은 Docker Desktop 호스트다. RTSP가 특정 LAN 주소에만 연결되어 있거나 중앙 서버가 원격이라면 그 LAN 주소를 쓴다. 브리지 컨테이너의 UDP 검색은 이 예시에서 사용하지 않는다. 호스트 실행과 컨테이너 실행은 같은 TCP 8002/8003을 동시에 사용할 수 없으므로 기존 프로세스를 종료한다.

**호스트·Docker 모두 같은 상태로 재실행할 때는 `--central-host`와 `--publish-credentials-file` 두 옵션을 제거한다.** 저장된 설정·자격 증명을 자동으로 읽는다. 같은 Device·Camera ID, Pairing Key와 상태·녹화 경로를 유지한다. 저장된 설정이 있으면 `--profile`과 `--central-port`의 새 값도 적용되지 않으며, 영상 품질 변경은 서버 관리자 화면에서 실행한다.

## 장애와 복구 시험

등록 후 중앙에서 정상 영상과 녹화가 보이는 상태에서 시작한다. Mock Edge가 실행 중인 PC의 새 PowerShell에서 다음을 호출한다. 원격 실행이면 `$management`·`$recovery`의 `127.0.0.1`을 Mock Edge 호스트 주소로 바꾼다.

```powershell
$pairingKey = (Get-Content -Raw -Encoding UTF8 'C:\secure\mock-edge\pairing-key.txt').TrimEnd([char[]]"`r`n")
$headers = @{ Authorization = "Bearer $pairingKey" }
$management = 'http://127.0.0.1:8003'
$recovery = 'http://127.0.0.1:8002'
Invoke-RestMethod -Method Post -Uri "$management/mock/v1/simulate" -Headers $headers -ContentType 'application/json' -Body '{"action":"central_connection_lost"}'
```

30초 이상 지난 뒤 같은 세션에서 연결을 복구한다. 중단 중에도 로컬 조각이 생성되는지 확인한다.

```powershell
Invoke-RestMethod -Method Post -Uri "$management/mock/v1/simulate" -Headers $headers -ContentType 'application/json' -Body '{"action":"central_connection_restored"}'
```

Edge 상태·이벤트와 최근 5분의 로컬 녹화 목록을 조회한다.

```powershell
Invoke-RestMethod -Uri "$management/internal/v1/status" -Headers $headers
Invoke-RestMethod -Uri "$management/internal/v1/events?limit=100" -Headers $headers
$end = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$start = [DateTime]::UtcNow.AddMinutes(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
Invoke-RestMethod -Headers $headers -Uri "$recovery/v1/recovery/manifest?start=$start&end=$end"
```

기간은 UTC로 한 번에 최대 24시간까지 지정할 수 있다. 기록 중인 최신 조각은 파일이 닫히고 다음 조각이 생긴 뒤 조회·다운로드할 수 있다. 중앙 수집·복구 주기와 조각 마감 대기 시간이 있어 복구 직후에는 완료되지 않을 수 있다.

위 manifest는 Edge에 있는 파일 목록이다. 중앙 복구 완료는 [공개 API 계약](../../docs/openapi.yaml)에 따라 중앙 관리자 로그인 토큰으로 별도 확인한다. 이 토큰은 위의 Edge Pairing Key와 다르다.

- `GET /api/v1/recovery-jobs?camera_id=cam-001`: 해당 장애 작업의 `status`가 `completed`인지 확인한다.
- `GET /api/v1/recordings?camera_id=cam-001&from=<UTC-시작>&to=<UTC-종료>`: 장애 구간 녹화의 `source`가 `edge_recovery`인지 확인한다.
- `GET /api/v1/recordings/{segment_id}/playback`: 반환된 `playback_url`로 인증된 재생을 확인한다. Android 앱에서는 이벤트에 연결된 녹화를 열 수 있다.

| 장애 종류 | `action` |
|---|---|
| 중앙 연결 | `central_connection_lost`, `central_connection_restored` |
| 카메라 입력 | `camera_input_lost`, `camera_input_restored` |
| 배터리 | `battery_low`, `battery_critical` |
| 외부 전원 | `external_power_lost`, `external_power_restored` |
| 저장 공간 | `storage_warning`, `storage_critical` |

`central_connection_lost`는 실제 RTSP 송출 프로세스를 멈추고 로컬 녹화는 유지한다. 나머지 장애 액션은 상태와 이벤트를 모사하며 MP4 송출·녹화를 중단하지 않는다. 카메라 단절이나 디스크 고갈에 대한 실제 하드웨어 동작 검증은 별도로 수행한다.

## 저장과 초기화

호스트 실행은 `.mock-edge/state/`에 설정·게시 비밀번호·이벤트를, `.mock-edge/recordings/`에 영상을 저장한다. Pairing으로 받은 Linux `backup_root`는 사용하지 않고 `--backup-dir`에 기록한다. Docker 예시는 두 이름 있는 볼륨에 각각 보존한다.

Mock Edge에는 녹화의 시간·용량별 자동 정리가 없다. 장시간 실행 시 사용량을 확인하고 시험 종료 후 필요한 영상을 보관한 뒤 정리한다. 실제 Edge의 보존 정책을 시험하는 용도로 사용하지 않는다.

다른 카메라로 초기화하려면 프로세스를 먼저 종료하고 상태·녹화를 보관한 뒤 새 경로 또는 볼륨을 지정한다. 키·게시 자격 증명·상태 폴더는 저장소에 넣지 않는다. HTTP 관리·복구 API는 신뢰 LAN에서만 접근하도록 제한한다. 전체 실행 옵션은 `python -m tests.mock_edge --help`로 확인한다.
