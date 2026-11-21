# Raspberry Pi Edge

카메라 영상을 H.264로 중앙 영상 서버(MediaMTX)에 보내며 Pi에도 기록한다. 중앙 연결 장애 중에도 로컬 백업을 유지한다. 대상은 Raspberry Pi OS Bookworm ARM64이며, AI 모델은 중앙에서 실행한다.

중앙 서버는 저장소 [설치 안내](../README.md)를 먼저 따른다. 아래 설치 명령은 Pi에서, 중앙 등록은 Server를 실행하는 PC에서 수행한다.

현재 GUI의 **카메라 등록 · 운영 설정** 화면에서 **신규 Edge 연결** 탭으로 자동 등록할 수 있다. 자동 등록은 아래 절차를, CLI로 등록하려면 [수동 연결](#수동-연결)을 따른다.

## 설치와 연결

### 네트워크 준비 (이더넷 기본)

Pi는 Raspberry Pi OS Bookworm ARM64를 사용한다. **최초 등록부터 최종 운영까지 이더넷 연결을 기본으로 한다.** 아래에서 공유기 LAN 연결과 데스크톱–Pi 직결 중 실제 구성에 맞는 절차를 따른다. Raspberry Pi Imager의 설정이나 Pi에서 실행한 `sudo raspi-config`로 SSH를 활성화한다.

#### 공유기 LAN에 연결하는 경우

Pi의 랜선을 DHCP 서버가 있는 공유기 또는 그 공유기에 연결된 스위치에 연결하고, Windows PC도 같은 신뢰 LAN에 연결한다.

Pi 터미널에서 이더넷의 IPv4 주소를 확인한다. `eth0`는 예시이며 실제 유선 인터페이스명이 다르면 바꾼다.

```bash
ip -br -4 addr show dev eth0
```

`eth0` 행에 `192.168.0.50/24` 같은 주소가 있어야 한다. 아래 전송·접속 명령에는 `/24`를 제외한 IP만 사용한다. 주소가 없으면 랜선·링크 상태와 공유기의 DHCP 설정을 확인한 뒤 진행한다.

#### 데스크톱과 Pi를 이더넷 케이블로 직결하는 경우

직결 상태에서 `ip -br link`의 `eth0`에 `UP, LOWER_UP`이 표시되면 물리 연결 신호는 잡힌 상태다. 데스크톱에서 DHCP 서버나 인터넷 연결 공유를 제공하지 않는다면 자동으로 IPv4 주소를 받지 못할 수 있으므로 양쪽에 같은 대역의 고정 IP를 지정한다. 아래는 Wi-Fi·VPN·다른 LAN과 `192.168.50.0/24` 대역이 겹치지 않는다는 전제의 예시다. 겹치면 다른 사설 대역을 골라 양쪽 주소를 함께 바꾼다.

| 장치 | 직결 이더넷 IPv4 주소 | 서브넷 마스크 |
|---|---|---|
| Windows 데스크톱 | `192.168.50.1/24` | `255.255.255.0` |
| Pi | `192.168.50.2/24` | `255.255.255.0` |

**Windows 관리자 PowerShell**에서 Pi와 연결한 유선 어댑터의 `Name`을 확인한다. 다음 `"이더넷"`을 확인한 실제 이름으로 바꿔 실행한다. Wi-Fi나 가상 어댑터를 선택하지 않는다. `New-NetIPAddress`는 이 유선 어댑터의 DHCP를 끄고 고정 IP를 설정한다. 직결 통신용이므로 기본 게이트웨이와 DNS는 지정하지 않는다.

```powershell
Get-NetAdapter
New-NetIPAddress -InterfaceAlias "이더넷" -IPAddress 192.168.50.1 -PrefixLength 24
```

이어서 **Pi 터미널**에서 직결용 연결 프로필을 저장하고 적용한다. 기존 Wi-Fi SSH 접속 또는 Pi에 연결한 키보드·화면으로 실행할 수 있다. 아래 명령은 Raspberry Pi OS Bookworm의 기본 NetworkManager 기준이다. `eth0`는 실제 유선 인터페이스명으로 바꾼다.

```bash
sudo nmcli connection add type ethernet ifname eth0 con-name edge-direct ipv4.method manual ipv4.addresses 192.168.50.2/24 ipv4.never-default yes connection.autoconnect-priority 100
sudo nmcli connection up edge-direct
ip -br -4 addr show dev eth0
```

`eth0`에 `192.168.50.2/24`가 표시되어야 한다. `edge-direct` 생성 명령은 최초 한 번만 실행한다. 저장된 프로필은 재부팅 후에도 사용되며, 다시 활성화할 때는 `sudo nmcli connection up edge-direct`를 사용한다. `ipv4.never-default yes`는 직결 이더넷이 인터넷용 기본 경로가 되지 않도록 한다. 이 설정만으로 인터넷이 공유되지는 않으므로, 설치 시 `apt`가 인터넷에 접속할 수 있도록 Pi의 Wi-Fi 등 별도 인터넷 연결을 유지한다.

Windows에서 다음 명령으로 **유선 SSH 접속**을 확인한다. `pi`는 실제 Pi 계정으로 바꾼다. 접속되면 이어지는 패키지 전송·설치 절차의 Pi 주소로 `192.168.50.2`를 사용하고, 최초 등록은 `--interface eth0`로 진행한다.

```powershell
ssh pi@192.168.50.2
```

접속을 확인했으면 `exit`로 Windows PowerShell에 돌아와 다음 패키지 전송 절차를 수행한다.

이더넷 자체를 사용할 수 없는 환경에서만 [Wi-Fi 대체 연결](#wi-fi-대체-연결)을 따른다.

### 패키지 다운로드와 Pi 전송 (Windows PC)

아래 `pi`는 OS 설치 시 만든 실제 Pi 계정으로, `192.168.0.50`은 위에서 확인한 Pi의 이더넷 IP로 바꾼다. 직결 예시를 따랐다면 `192.168.50.2`로 바꾼다. Wi-Fi 대체 절차를 사용하는 경우에는 Wi-Fi IP를 사용한다.

GitHub 저장소의 **Actions → Build Edge DEB**에서 성공한 실행을 열고, **Artifacts**의 `ai-cctv-edge-arm64`를 Windows PC에 다운로드한다. 받은 ZIP에 `.deb`와 `.deb.sha256`이 모두 있는지 확인한다. 아래는 ZIP을 기본 다운로드 폴더에 저장한 예시다. **Windows PowerShell**에서 실행하며, ZIP 경로와 패키지 버전은 실제 받은 파일에 맞춘다.

```powershell
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\ai-cctv-edge-arm64.zip" -DestinationPath "$env:USERPROFILE\Downloads\ai-cctv-edge-arm64"
Set-Location "$env:USERPROFILE\Downloads\ai-cctv-edge-arm64"
scp .\ai-cctv-edge_0.3.3_arm64.deb .\ai-cctv-edge_0.3.3_arm64.deb.sha256 pi@192.168.0.50:~/
ssh pi@192.168.0.50
```

걍 압축풀고, powershell로 폴더 드가서 scp로 파이에다가 파일만 날리셈. ㅇㅇ.

`scp`는 두 파일을 Pi 계정의 홈 폴더로 복사하고, `ssh`는 Pi 터미널에 접속한다. 비밀번호를 요청하면 Pi 계정의 비밀번호를 입력한다. Windows에서 `scp`나 `ssh` 명령을 찾지 못하면 Windows 선택적 기능의 **OpenSSH 클라이언트**를 설치한다. 전송에 실패하면 IP·계정·SSH 활성화 상태와 PC에서 Pi의 TCP 22 포트에 접속할 수 있는지 확인한다.

### Pi에서 설치와 최초 연결

이제 **SSH로 접속한 Pi 터미널**에서 실행한다. `rpicam-hello --list-cameras`로 카메라를, `timedatectl status`로 시간 동기화를 확인한다. 아래 `cd ~`는 패키지를 전송한 Pi 홈 폴더로 이동한다. 다른 경로에 전송했다면 그 폴더로 이동하고 파일명의 버전도 받은 파일에 맞춘다. Python 3.11과 필요한 OS 패키지는 `apt install`이 설치하며, 처음에는 패키지 저장소에 접속할 수 있어야 한다. 명령이 실패하면 원인을 해결한 뒤 다음 단계로 진행한다.

```bash
cd ~
sha256sum -c ai-cctv-edge_0.3.3_arm64.deb.sha256
test "$(dpkg --print-architecture)" = arm64
sudo apt update
sudo apt install ./ai-cctv-edge_0.3.3_arm64.deb
```

#### 이더넷으로 최초 연결 (기본)

패키지 설치 후, 위에서 확인한 이더넷 인터페이스에 IPv4 주소가 있는 상태에서 실행한다.

```bash
sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --interface eth0
```

`pair`는 최초 등록용 검색과 설정 수신을 시작한다. 장치마다 `edge-001`·`cam-001`을 고유한 ID로, `eth0`를 실제 이더넷 인터페이스명으로 바꾸고 등록이 끝날 때까지 Pi 터미널을 열어 둔다. 별도의 Pairing Key를 Pi 명령에 입력하지 않는다.

`network interface has no IPv4 address: eth0` 오류가 나면 이더넷 주소가 아직 없는 상태다. 네트워크 준비 단계로 돌아가 연결과 주소 할당을 확인한다. `--interface eth0` 자체가 이더넷을 활성화하거나 IP를 설정하지는 않는다. 이더넷으로 진행했다면 Wi-Fi 대체 절차를 건너뛰고 [중앙 서버에 등록](#중앙-서버에-등록)으로 이어간다.

#### Wi-Fi 대체 연결

랜선을 연결할 수 없는 등 이더넷 사용이 불가능한 경우에만 사용한다. 패키지 전송 전에 Pi를 서버·Windows PC와 통신할 수 있는 같은 신뢰 LAN의 Wi-Fi에 연결하고, 아래 명령으로 Wi-Fi IPv4 주소를 확인한다. `wlan0`는 실제 무선 인터페이스명으로 바꾼다. 게스트 네트워크나 AP 격리로 장치 간 통신이 차단된 Wi-Fi에서는 검색·등록이 실패할 수 있다.

```bash
ip -br -4 addr show dev wlan0
```

확인한 Wi-Fi IP로 위의 패키지 전송·SSH 접속·설치 절차를 수행한다. 패키지 설치 후에는 이더넷용 `pair` 명령 대신 다음을 실행하고 중앙 등록을 진행한다.

```bash
sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --interface wlan0
```

최초 검색에는 선택한 인터페이스의 IP·MAC이 전달된다. Wi-Fi 등록 후 이더넷으로 전환할 때는 중앙에 등록된 장치 주소·MAC과 관리·복구 접속 주소를 이더넷 기준으로 확인하고 갱신해야 한다. 이미 등록된 장치에 `pair`를 다시 실행해 전환하지 않는다.

#### 중앙 서버에 등록

위 직결 예시에서는 Pi의 관리·복구 주소가 각각 `http://192.168.50.2:8003`, `http://192.168.50.2:8002`이고, 중앙 RTSP 주소의 호스트는 데스크톱의 이더넷 IP인 `192.168.50.1`이다. 설치 도우미에서 서버 주소와 중앙 RTSP 호스트를 선택할 때 이 이더넷 IP를 사용하고, 서버의 `RTSP_BIND_ADDRESS`도 해당 주소에서 수신하도록 설정한다. 유선 SSH가 되는데 검색·등록이 실패하면 직결 이더넷에 적용된 Windows 방화벽 규칙과 아래 통신 포트를 확인한다.

같은 신뢰 IPv4 LAN의 설치 도우미에서 관리자 계정·비밀번호로 로그인하고 **카메라 등록 · 운영 설정** 화면의 **신규 Edge 연결** 탭을 연다. 이 Edge 전용의 무작위 32자 이상 운영 인증 토큰을 입력하고 **Edge 찾기** → Edge 선택 → **선택한 Edge 연결**을 실행한다. 이 값은 등록 과정에서 Pi 설정에 전달되어 Control·Recovery 요청을 인증한다. 필요하면 **연결 확인**으로 먼저 점검한다. 고급 설정에서 중앙 RTSP 주소·카메라 ID·화질(`hd`/`fhd`)을 확인·변경할 수 있다. 중앙 RTSP 주소는 Pi에서 접근할 중앙 PC의 LAN 주소다. 검색 실패 시 UDP 37020 방화벽과 공유기의 장치 간 통신 차단(AP 격리)을 확인한다.

중앙 등록 후 Edge 설정 전달만 실패하면 Pi의 `pair`를 유지하고 같은 GUI에서 **Edge 설정 전달 재시도**를 실행한다. 게시 계정 파일 저장 실패라면 **게시 계정 파일 저장 재시도**를 먼저 실행한다. 이미 중앙에 생성된 카메라는 다시 등록하지 않는다. 저장된 게시 계정 JSON에는 운영 인증 토큰이 없으므로 JSON만으로 수동 설정하면 중앙에 등록된 토큰과 Pi의 토큰이 달라질 수 있다. 수동 전환 시에는 양쪽 운영 토큰의 일치도 별도로 확인해야 한다.

기본 포트의 통신 방향은 다음과 같다. 중앙의 `RTSP_BIND_ADDRESS`도 Pi가 접근할 LAN 주소에 바인딩되어 있어야 한다.

#### 기존 Edge Repair

설정된 Edge를 다시 연결할 때 config 파일이나 `.configured`를 직접 지우지 않는다. 다음 명령은 기존 ID와 네트워크 설정을 기본값으로 사용하며 입력한 옵션만 바꾼다.

```bash
sudo ai-cctv-edge repair
sudo ai-cctv-edge repair --interface wlan0
sudo ai-cctv-edge repair --device-id edge-002 --camera-id cam-002
```

Repair는 최대 10분 동안 기존 「Edge 찾기」·「선택한 Edge 연결」 절차를 기다린다. 작업 중 캡처와 Edge API가 중지되어 녹화 공백이 생길 수 있다. 기존 녹화 파일과 이벤트 기록은 유지되지만 Camera ID를 바꾸면 이후 기록은 새 ID 경로에 저장되고 기존 이력은 자동 연결되지 않는다.

NIC를 바꾸면 광고 MAC 주소도 바뀐다. Server는 Device ID와 MAC의 기존 관계를 검사하므로, ID를 유지한 채 인터페이스만 바꾸면 등록이 거부될 수 있다. 등록 충돌 해결을 위해 Server 기록을 자동 삭제·이전하지 않는다. GUI 오류를 확인하고 Server의 지원 절차로 충돌을 해결한 뒤 기존 등록 응답이 있으면 Edge 설정 전달 재시도를 사용한다.

Repair가 실패하면 이전 로컬 설정과 서비스를 복원한다. 복원에 실패하면 보호된 백업 경로를 안내하며, 재부팅 시 미완료 작업을 감지해 복원을 시도한다. 토큰·게시 비밀번호를 로그에 공유하지 않는다. 성공 메시지는 Edge 로컬 설정과 기존에 실행 중이던 서비스가 복구됐다는 뜻이며, Server 등록·실제 영상 수신은 GUI와 영상 화면에서 따로 확인한다.

| 출발지 → 목적지 | 프로토콜·포트 | 용도 |
|---|---|---|
| Pi → 설치 도우미 PC | UDP 37020 | 장치 검색 광고 |
| 설치 도우미 PC → Pi | TCP 8003 | 최초 설정 전달 |
| 중앙 서버 컨테이너 → Pi | TCP 8003, 8002 | 관리·상태 조회, 녹화 복구 |
| Pi → 중앙 서버 PC | TCP 8554 | RTSP 송출 |

구성 완료 후 다음 명령으로 진단·서비스 상태를 확인한다. 신규 등록 직후에는 서비스가 실행되어도 송출·녹화 요청은 중지 상태다. 중앙 GUI에서 해당 카메라의 **녹화 시작**을 실행한 뒤 실제 영상 수신과 중앙·Pi 로컬 녹화를 확인한다. Pi의 `start`·`restart` 명령은 서비스를 제어하며 중앙의 녹화 시작 명령을 대신하지 않는다. `doctor`의 TCP 연결 성공만으로 영상 송출까지 검증되지는 않는다.

```bash
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
```

재부팅 뒤 촬영·제어·복구 세 서비스가 복귀하고 저장된 송출·녹화 의도가 유지되어야 한다. 녹화가 시작된 상태였다면 중앙 영상과 녹화도 복귀해야 한다. 로그 조회와 재시작은 각각 `sudo ai-cctv-edge logs`, `sudo ai-cctv-edge restart`다. 로그 조회는 `Ctrl+C`로 끝낸다. 업데이트 전 `/etc/ai-cctv-edge/`와 `/var/lib/ai-cctv-edge/` 및 별도 백업 경로를 보존하고 새 패키지를 같은 방법으로 설치한 뒤 `sudo ai-cctv-edge restart`로 적용한다. 이미 설정된 장치에는 `pair`를 다시 실행하지 않는다. `sudo apt remove ai-cctv-edge`는 운영 데이터를 자동 삭제하지 않는다.

기본 로컬 백업은 `/var/lib/ai-cctv-edge/recordings/<camera-id>/`의 약 10초 MPEG-TS 조각이다. 녹화가 완료되면 실제 시작·종료 시각을 담은 `.ts.json` 메타데이터가 생성된다. 백업·이전 시 TS와 해당 메타데이터를 함께 보존한다. Recovery API는 유효한 완료 메타데이터가 있는 TS만 목록과 다운로드로 제공하며, 메타데이터 없는 기존 영상은 자동 복구 대상에서 제외된다.

24시간 또는 20 GiB 보존 한도를 넘으면 오래된 조각과 해당 메타데이터를 함께 정리한다. 메타데이터 없는 기존 TS도 파일 수정 시각을 기준으로 정리 대상이 되므로 필요한 영상은 보존 한도 밖의 별도 저장소에 복사한다. 긴 장애에서도 모든 영상을 무기한 보존하는 구성은 아니다. 보존 한도는 `/etc/ai-cctv-edge/config.toml`의 `[backup]`에서 조정하고 서비스를 재시작한다. 백업 경로를 바꾸면 `ai-cctv-edge` 서비스 계정에 쓰기 권한이 있어야 한다. 서비스가 홈 디렉터리 접근을 제한하므로 `/home/pi` 대신 `/mnt/cctv` 같은 별도 저장소 경로를 사용한다.

## 수동 연결

아직 중앙에 등록하지 않은 장치에서 자동 등록을 쓸 수 없을 때 진행한다. 자동 등록이 일부 완료된 장치는 위의 설정 전달 재시도 절차를 먼저 따른다. `pair`가 실행 중이면 `Ctrl+C`로 종료한다. 경로·주소·ID는 실제 값으로 바꾸고 전달 파일은 SSH/SCP 등 보호된 전송으로 옮긴다. `/home/pi`는 실제 Pi 사용자 홈으로 바꾼다.

1. Edge에서 인증 토큰을 보호 파일로 내보낸 뒤 중앙 PC로 전송한다.

   ```bash
   sudo ai-cctv-edge export-auth-token --output /home/pi/edge-001-control.token
   ```

2. Windows 설치 후 새 PowerShell에서 등록한다. CLI는 중앙 관리자 비밀번호를 숨김 입력으로 받으며 기본 계정명은 `admin`이다. 다르면 `--username`을 추가한다. 관리 8003·복구 8002 주소는 중앙 컨테이너에서 접근할 수 있는 Pi 주소로 지정한다. 소스 CLI 실행법은 [설치 도우미 README](../server/setup/install_helper/README.md#개발-실행)를 따른다.

   ```powershell
   AI_CCTV_CLI.exe edge-register cam-001 --server-url https://cctv.example.com --name Entrance --edge-device-id edge-001 --edge-mac-address aa:bb:cc:dd:ee:ff --management-url http://192.0.2.41:8003 --recovery-url http://192.0.2.41:8002 --edge-auth-token-file 'C:\secure\edge-001-control.token' --publish-credentials-output 'C:\secure\cam-001-publish.json'
   ```

   MAC 주소도 Pi에서 조회한 실제 값으로 바꾼다.

3. 생성된 JSON을 해당 Edge로 전달한다.

   파일 권한을 제한한 뒤 설정을 실행한다.

   ```bash
   chmod 600 /home/pi/cam-001-publish.json
   sudo ai-cctv-edge setup --publish-credentials-file /home/pi/cam-001-publish.json
   ```

등록한 Device·Camera ID, `central_publish`(중앙으로 송출), 중앙 LAN IP, 지원 Profile(`hd`/`fhd`)과 백업 경로를 입력한다. 완료하면 서비스가 시작되고 재부팅 후에도 자동 실행된다. 신규 등록 직후에는 중앙 GUI에서 해당 카메라의 **녹화 시작**을 별도로 실행한다. 중앙에서 영상·상태 조회를 확인한 뒤 양쪽 전달용 token·JSON은 지우고 `/etc/ai-cctv-edge/`의 운영 파일은 보존한다. 중앙에 이미 생성된 카메라를 다시 등록하지 않는다.

## Edge 상태 동작

캡처 상태는 PID 존재뿐 아니라 실행 잠금의 PID·인스턴스 ID와 단조 시계 heartbeat를 함께 확인한다. 갱신이 `max(15초, frame_timeout_seconds × 3)`를 넘거나 실행 소유자가 다르면 `capture_state=stale`, `camera_input=offline`, `last_error_code=CAPTURE_STATUS_STALE`로 응답한다. 중앙은 이전 입력이 정상이었고 Edge 일지에 손실 기록이 없을 때 입력 손실 이벤트를 보완한다. 업데이트 시 캡처·제어 서비스를 함께 재시작하여 새 heartbeat 형식을 적용한다.

실제 카메라 입력과 GStreamer 동작은 Raspberry Pi에서 확인한다. 소스 패키지를 `pip install`로 설치해도 OS 패키지와 자동 실행 서비스는 설치되지 않으므로 실제 장치 설치에는 `.deb`를 사용한다.

## 패키지 빌드

ARM64 Linux에서 Python 3.11/pip·dpkg-deb·coreutils·Git과 검증된 ARM64용 MediaMTX 1.9.0 바이너리를 준비한다. 아래는 바이너리를 `vendor/mediamtx`에 둔 예시이며 저장소 루트에서 실행한다. 최초 Python 패키지 수집에는 인터넷이 필요하다.

```bash
export MEDIAMTX_BINARY="$PWD/vendor/mediamtx"
export MEDIAMTX_SHA256='<확인한 64자리 SHA-256>'
export MEDIAMTX_VERSION='v1.9.0'
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export OUTPUT_DIR="$PWD/dist/edge"
sh edge/packaging/build_deb.sh
```

`dist/edge/`에 `.deb`와 `.deb.sha256`이 생성된다. Python 패키지 파일(wheel)은 `.deb` 안에 포함되며, OS 의존성은 별도로 `apt`가 설치한다. 빌드 끝에 `verify_deb.sh`가 패키지 구조·권한·ARM64 wheel·비밀 파일 혼입 여부를 검사한다. 배포 전 Pi에서 설치·업데이트·재부팅·제거를 확인한다.
# 최초 등록 보안

Discovery 광고는 같은 LAN에서 미설정 Edge를 찾는 데 쓰며 운영 인증 토큰을 포함하지 않는다. Device ID와 MAC 주소는 장치를 식별할 뿐 인증하지 않는다. 운영자가 등록 화면에서 입력한 32자 이상의 Edge 운영 인증 토큰은 등록 과정에서 Edge에 전달되어 이후 Control·Recovery 요청을 인증한다. 이 토큰은 Pi의 `pair` 명령에 입력하는 Pairing Key가 아니다.
