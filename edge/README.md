# Raspberry Pi Edge

카메라 영상을 H.264로 중앙 영상 서버(MediaMTX)에 보내며 Pi에도 기록한다. 중앙 연결 장애 중에도 로컬 백업을 유지한다. 대상은 Raspberry Pi OS Bookworm ARM64이며, AI 모델은 중앙에서 실행한다.

중앙 서버는 [설치 안내](../README.md#설치하기)를 먼저 따른다. 아래 설치 명령은 Pi에서, 등록은 중앙 PC의 설치 도우미에서 실행한다.

## 설치와 연결

Raspberry Pi OS Bookworm ARM64에서 `rpicam-hello --list-cameras`로 카메라를, `timedatectl status`로 시간 동기화를 확인한다. 받은 패키지와 체크섬을 같은 폴더에 두고 그 폴더에서 실행한다. 파일명의 버전은 받은 파일에 맞춘다. Python 3.11과 필요한 OS 패키지는 `apt install`이 설치하며, 처음에는 패키지 저장소에 접속할 수 있어야 한다. 명령이 실패하면 원인을 해결한 뒤 다음 단계로 진행한다.

```bash
sha256sum -c ai-cctv-edge_0.3.0_arm64.deb.sha256
test "$(dpkg --print-architecture)" = arm64
sudo apt update
sudo apt install ./ai-cctv-edge_0.3.0_arm64.deb
sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --set-pairing-key
```

`pair`는 첫 등록용이며, 장치마다 `edge-001`·`cam-001`을 고유한 ID로 바꾼다. 숨김 입력으로 32자 이상 연결 키를 설정한 뒤 등록이 끝날 때까지 Pi 터미널을 열어 둔다.

같은 신뢰 IPv4 LAN의 설치 도우미에서 관리 화면의 **카메라 연결**을 연다. 관리자 계정·비밀번호와 Pi와 같은 장치 연결 키를 입력하고 **Edge 찾기** → Edge 선택 → **선택한 Edge 연결**을 실행한다. 필요하면 **연결 확인**으로 먼저 점검한다. 고급 설정에서 중앙 RTSP 주소·카메라 ID·화질(`hd`/`fhd`)을 확인·변경할 수 있다. 중앙 RTSP 주소는 Pi에서 접근할 중앙 PC의 LAN 주소다. 검색 실패 시 UDP 37020 방화벽·공유기의 장치 간 통신 차단(AP 격리)·연결 키를 확인한다.

기본 포트의 통신 방향은 다음과 같다. 중앙의 `RTSP_BIND_ADDRESS`도 Pi가 접근할 LAN 주소에 바인딩되어 있어야 한다.

| 출발지 → 목적지 | 프로토콜·포트 | 용도 |
|---|---|---|
| Pi → 설치 도우미 PC | UDP 37020 | 장치 검색 광고 |
| 설치 도우미 PC → Pi | TCP 8003 | 최초 설정 전달 |
| 중앙 서버 컨테이너 → Pi | TCP 8003, 8002 | 관리·상태 조회, 녹화 복구 |
| Pi → 중앙 서버 PC | TCP 8554 | RTSP 송출 |

구성 완료 후 다음 명령으로 진단·상태를 확인하고 중앙 화면에서 실제 영상과 녹화를 확인한다. `doctor`의 TCP 연결 성공만으로 영상 송출까지 검증되지는 않는다.

```bash
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
```

재부팅 뒤 촬영·제어·복구 세 서비스와 중앙 영상이 복귀해야 한다. 로그 조회와 재시작은 각각 `sudo ai-cctv-edge logs`, `sudo ai-cctv-edge restart`다. 로그 조회는 `Ctrl+C`로 끝낸다. 업데이트 전 `/etc/ai-cctv-edge/`와 `/var/lib/ai-cctv-edge/` 및 별도 백업 경로를 보존하고 새 패키지를 같은 방법으로 설치한 뒤 `sudo ai-cctv-edge restart`로 적용한다. 이미 설정된 장치에는 `pair`를 다시 실행하지 않는다. `sudo apt remove ai-cctv-edge`는 운영 데이터를 자동 삭제하지 않는다.

기본 로컬 백업은 `/var/lib/ai-cctv-edge/recordings/<camera-id>/`의 10초 MPEG-TS 조각이며, 24시간 또는 20 GiB 보존 한도를 넘으면 오래된 조각부터 정리한다. 긴 장애에서도 모든 영상을 무기한 보존하는 구성은 아니다. 보존 한도는 `/etc/ai-cctv-edge/config.toml`의 `[backup]`에서 조정하고 서비스를 재시작한다. 백업 경로를 바꾸면 `ai-cctv-edge` 서비스 계정에 쓰기 권한이 있어야 한다. 서비스가 홈 디렉터리 접근을 제한하므로 `/home/pi` 대신 `/mnt/cctv` 같은 별도 저장소 경로를 사용한다.

## 수동 연결

패키지 설치 후 자동 등록을 쓸 수 없을 때 진행한다. `pair`가 실행 중이면 `Ctrl+C`로 종료한다. 경로·주소·ID는 실제 값으로 바꾸고 전달 파일은 SSH/SCP 등 보호된 전송으로 옮긴다. `/home/pi`는 실제 Pi 사용자 홈으로 바꾼다.

1. Edge에서 인증 토큰을 보호 파일로 내보낸 뒤 중앙 PC로 전송한다.

   ```bash
   sudo ai-cctv-edge export-auth-token --output /home/pi/edge-001-control.token
   ```

2. Windows 설치 후 새 PowerShell에서 등록한다. CLI는 중앙 관리자 비밀번호를 숨김 입력으로 받으며 기본 계정명은 `admin`이다. 다르면 `--username`을 추가한다. 관리 8003·복구 8002 주소는 중앙 컨테이너에서 접근할 수 있는 Pi 주소로 지정한다. 소스 CLI 실행법은 [설치 도우미 README](../server/setup/install_helper/README.md#개발-실행)를 따른다.

   ```powershell
   AI_CCTV_CLI.exe edge-register cam-001 --server-url https://cctv.example.com --name Entrance --edge-device-id edge-001 --management-url http://192.0.2.41:8003 --recovery-url http://192.0.2.41:8002 --edge-auth-token-file 'C:\secure\edge-001-control.token' --publish-credentials-output 'C:\secure\cam-001-publish.json'
   ```

3. 생성된 JSON을 해당 Edge로 전달한다.

   파일 권한을 제한한 뒤 설정을 실행한다.

   ```bash
   chmod 600 /home/pi/cam-001-publish.json
   sudo ai-cctv-edge setup --publish-credentials-file /home/pi/cam-001-publish.json
   ```

등록한 Device·Camera ID, `central_publish`(중앙으로 송출), 중앙 LAN IP, 지원 Profile(`hd`/`fhd`)과 백업 경로를 입력한다. 완료하면 서비스가 시작되고 재부팅 후에도 자동 실행된다. 중앙에서 영상·상태 조회를 확인한 뒤 양쪽 전달용 token·JSON은 지우고 `/etc/ai-cctv-edge/`의 운영 파일은 보존한다. 자동 등록 중 인증 파일 전달만 실패했다면 Pi의 `pair`를 종료하고 설치 도우미가 만든 JSON으로 3번부터 진행한다. 중앙에 이미 생성된 카메라를 다시 등록하지 않는다.

## Edge 코드 검증

Python 3.11 가상환경을 활성화하고 저장소 루트에서 실행한다. 실제 카메라·GStreamer 검증은 Pi에서 따로 수행한다.

```bash
python -m pip install -e './edge[test]'
python -m pytest -c edge/pyproject.toml edge/tests -q
```

서버와의 프로토콜 연동은 서버 테스트 컨테이너에서도 확인한다. `pip install`은 OS 패키지와 자동 실행 서비스를 설치하지 않으므로 실제 장치 설치에는 `.deb`를 사용한다.

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
