# Raspberry Pi Edge

카메라 영상을 H.264로 중앙 MediaMTX에 보내며 로컬에도 계속 기록한다. 중앙 연결 장애 중에도 로컬 백업을 유지한다. 대상은 Raspberry Pi OS Bookworm ARM64다. AI 모델은 중앙에서 실행한다.

중앙 서버는 [설치 안내](../README.md#설치-선택)를 먼저 따른다. 아래 설치 명령은 Pi에서, 등록은 중앙 PC의 Configurator에서 실행한다.

| 위치 | 역할 |
|---|---|
| `src/ai_cctv_edge/` | 카메라 송출·설정·제어·복구 |
| `tests/` | Edge 기능·배포 계약 테스트 |
| `packaging/` | ARM64 패키지·서비스 설치 |

## 설치와 연결

Raspberry Pi OS Bookworm ARM64·Python 3.11에서 카메라와 시간 동기화를 확인한다. 패키지와 체크섬을 같은 폴더에 두고 실행한다.

```bash
sha256sum -c ai-cctv-edge_0.3.0_arm64.deb.sha256
test "$(dpkg --print-architecture)" = arm64
sudo apt update
sudo apt install ./ai-cctv-edge_0.3.0_arm64.deb
sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --set-pairing-key
```

숨김 입력으로 32자 이상 Key를 설정한다. 같은 신뢰 IPv4 LAN의 Configurator에서 같은 Key 입력 → **Discover Edge on trusted LAN** → Edge 선택 → 중앙 RTSP 주소·카메라·Profile 확인 → **Register Edge and camera**를 실행한다. 검색 실패 시 UDP 37020 방화벽·AP 격리·Key를 확인한다.

구성 완료 후 `sudo ai-cctv-edge doctor`·`status`를 실행하고 재부팅 뒤 Capture·Control·Recovery 세 서비스와 중앙 영상이 복귀하는지 확인한다. 평소에는 `logs`·`restart`를 사용한다. 업데이트 전 설정·토큰·백업을 보존하고 새 패키지를 같은 방법으로 설치한다. `sudo apt remove ai-cctv-edge`는 운영 데이터를 자동 삭제하지 않는다.

## 수동 연결

검색을 쓸 수 없을 때만 진행한다. 경로·주소·ID를 실제 값으로 바꾸고 전달 파일은 보호된 전송으로 옮긴다.

1. Edge에서 인증 토큰을 보호 파일로 내보낸 뒤 중앙 PC로 전송한다.

```bash
sudo ai-cctv-edge export-auth-token --output /home/pi/edge-001-control.token
```

2. 중앙 Windows CLI에서 등록한다. 관리 8003과 복구 8002는 독립 주소다.

```powershell
AI_CCTV_CLI.exe edge-register cam-001 `
  --server-url https://cctv.example.com --name Entrance `
  --edge-device-id edge-001 `
  --management-url http://192.0.2.41:8003 --recovery-url http://192.0.2.41:8002 `
  --edge-auth-token-file 'C:\secure\edge-001-control.token' `
  --publish-credentials-output 'C:\secure\cam-001-publish.json'
```

3. 생성된 JSON을 해당 Edge로 전달한다.

```bash
chmod 600 /home/pi/cam-001-publish.json
sudo ai-cctv-edge setup --publish-credentials-file /home/pi/cam-001-publish.json
```

동일 Device·Camera ID, `central_publish`, 중앙 LAN IP, 지원 Profile과 백업 경로를 입력한다. 완료 후 양쪽 전달용 token·JSON만 지운다. `/etc/ai-cctv-edge/`의 실제 운영 파일은 보존한다. Pairing 자동 전달만 실패했다면 Configurator가 만든 보호 파일로 3번부터 진행한다.

## Edge 코드 검증

Raspberry Pi 또는 Python 3.11 개발 환경에서 저장소 루트 기준으로 실행한다. 실제 카메라·GStreamer 검증은 Pi에서 따로 수행한다.

```sh
python -m pip install -e './edge[test]'
python -m pytest -c edge/pyproject.toml edge/tests -q
```

서버와의 프로토콜 연동은 서버 테스트 컨테이너에서도 확인한다. Edge의 OS 패키지·서비스 설치는 `.deb` 패키지가 담당한다.

## 패키지 빌드

저장소 루트에서 실행한다. ARM64 Linux·Python 3.11/pip·dpkg-deb·coreutils·Git에서 검증된 MediaMTX 1.9.0 바이너리를 준비한다. 최초 wheel 수집에는 Package Index가 필요하다.

```bash
export MEDIAMTX_BINARY="$PWD/vendor/mediamtx"
export MEDIAMTX_SHA256='<확인한 64자리 SHA-256>'
export MEDIAMTX_VERSION='v1.9.0'
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export OUTPUT_DIR="$PWD/dist/edge"
sh edge/packaging/build_deb.sh
```

결과는 `dist/edge/`에 체크섬과 오프라인 wheel을 포함해 생성된다. 배포 전 Pi에서 설치·업데이트·재부팅·제거를 확인한다.
