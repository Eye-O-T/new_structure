# Raspberry Pi Edge 설치·배포 가이드

이 문서는 개발자가 아닌 설치 담당자가 중앙 서버와 Raspberry Pi Edge를 연결하는
정본 절차다. 대상은 **Raspberry Pi OS Bookworm 64-bit(ARM64), Python 3.11**이며
배포 파일은 다음 두 개다.

```text
ai-cctv-edge_<version>_arm64.deb
ai-cctv-edge_<version>_arm64.deb.sha256
```

Edge에는 AI 모델을 설치하지 않는다. 추론 모델은 중앙 서버 설치 전에 사용자가
호환되는 모델 파일을 내려받은 뒤 Configurator GUI의 파일 선택 또는 중앙 CLI의
`init --model <local-file>`로 지정한다. 현재 설치 프로그램은 모델 다운로드나
라이선스 판단을 수행하지 않는다.

## 1. 설치 전 준비

- 중앙 서버 설치와 관리자 계정 생성을 먼저 완료한다.
- 원격 Edge가 게시할 중앙 서버의 신뢰 LAN IP에 RTSP 8554/TCP를 Bind한다.
- Raspberry Pi에서 카메라를 연결하고 OS 시간 동기화와 유선 LAN을 준비한다.
- 중앙 서버와 Edge의 장치 ID, Camera ID, 고정 주소를 정한다.
- `.deb`와 `.sha256`을 같은 디렉터리에 둔다.

Camera ID는 중앙 등록과 Edge setup에서 정확히 같아야 한다. 이 예시는 다음 값을
사용한다.

```text
Edge device ID: edge-001
Camera ID:      cam-001
Central LAN IP: 192.0.2.10
Edge LAN IP:    192.0.2.41
```

## 2. 패키지 확인과 설치

Raspberry Pi에서 체크섬과 아키텍처를 확인한 뒤 설치한다.

```bash
cd ~/Downloads
sha256sum -c ai-cctv-edge_0.3.0_arm64.deb.sha256
test "$(dpkg --print-architecture)" = arm64
sudo apt update
sudo apt install ./ai-cctv-edge_0.3.0_arm64.deb
```

설치 시 다음 작업만 수행한다.

- 전용 `ai-cctv-edge` 시스템 계정 생성
- Python venv와 오프라인 wheel 설치
- 세 systemd Service 등록(구성 완료 전에는 enable/start하지 않음)
- `/var/lib/ai-cctv-edge` 상태·백업 디렉터리 생성
- `/etc/ai-cctv-edge/recovery.token` 최초 1회 생성

최초 설치는 잘못된 예제 주소로 영상을 게시하지 않도록 Capture Service를 바로
시작하지 않는다. 아래 자격증명 교환과 `setup`이 끝나야 세 Service가 시작된다.

## 3. 중앙–Edge 자격증명 교환

### 3.0 권장: Configurator LAN Pairing

Raspberry Pi와 중앙 Windows PC를 신뢰할 수 있는 동일 IPv4 LAN에 연결한다. Edge에서
다음 명령을 실행하고 숨김 Prompt에 임의의 32자 이상 Key를 두 번 입력한다. Key를
명령행 인자, 채팅, 스크린샷이나 로그에 넣지 않는다.

```bash
sudo ai-cctv-edge pair \
  --device-id edge-001 \
  --camera-id cam-001 \
  --set-pairing-key
```

이 명령은 구성 완료 Marker가 없는 Edge에서만 실행된다. UDP 37020으로 HMAC-SHA256
서명 광고를 보내고 Port 8003에 임시 Pairing API를 연다. 중앙 Configurator에서 다음
순서로 진행한다.

1. `Edge pairing / bearer key`에 Edge와 같은 Key를 입력한다.
2. `Discover Edge on trusted LAN`을 누른다.
3. 발견 목록에서 `edge-001`을 선택한다. 실제 UDP Peer 주소로 관리 8003과 복구 8002
   URL이 자동 입력되는지 확인한다.
4. `Central RTSP host for Edge`에 Edge에서 접근 가능한 중앙 LAN IP를 입력하고
   `Edge backup root`를 확인한다.
5. Camera 이름과 `hd`/`fhd` Profile을 확인하고 `Register Edge and camera`를 누른다.

정상 완료 시 Configurator가 관리자 HTTPS API로 Camera를 등록하고, 한 번 반환된 RTSP
게시 자격증명을 선택된 Edge의 임시 Pairing API에 즉시 전달한다. Edge는 Device/Camera
ID, Profile과 중앙 주소를 검증한 뒤 설정, 게시 비밀번호와 `.configured` Marker를 원자
저장하고 Pairing Listener를 종료한다. 이어서 Capture, Control, Recovery Service가
시작된다. 정상 경로에는 Token 또는 게시 자격증명 전달 파일이 생기지 않는다.

자동 전달에 실패하면 Configurator는 게시 자격증명을 `Publish credential handoff`에
지정된 보호 파일로 저장한다. 아래 수동 절차의 3.3부터 계속한다. 검색 자체가 실패하면
Windows Private Network 방화벽의 UDP 37020, 같은 Broadcast Domain, AP Client
Isolation과 입력 Key를 확인한다.

Pairing 광고에는 Secret이나 IP 주소가 없다. Configurator는 실제 UDP 발신 주소를
사용하고 잘못된 Key, 변조된 서명과 10초보다 오래된 광고를 무시한다. Pairing API는
Bearer Key로 보호되지만 신뢰 LAN의 임시 HTTP Bootstrap이므로 공용 Wi-Fi나 인터넷에
노출하지 않는다.

### 3.0.1 대체: 수동 Handoff

Broadcast가 차단되거나 GUI를 사용할 수 없으면 자격증명을 다음 방향으로 한 번씩
이동한다.

```text
Edge recovery.token
  └─ export-auth-token ──> 중앙 edge-register의 --edge-auth-token-file

중앙 일회성 cam-001-publish.json
  └─ 안전한 파일 전송 ──> Edge setup의 --publish-credentials-file
```

Token이나 게시 비밀번호를 채팅, 명령행 인자, 스크린샷 또는 일반 로그에 붙여
넣지 않는다.

### 3.1 Edge 인증 Token 내보내기

`.deb` 설치가 생성한 실 Token을 값으로 출력하지 않고 새 `0600` 파일에 복사한다.
기존 출력 파일은 덮어쓰지 않는다.

```bash
sudo ai-cctv-edge export-auth-token \
  --output /home/pi/edge-001-control.token
stat -c '%a %U %G %n' /home/pi/edge-001-control.token
```

`sudo`로 실행하면 CLI는 가능한 경우 파일 소유권을 원래 로그인 사용자에게 돌려준다.
결과 Mode가 `600`인지 확인하고 SFTP/SCP 또는 암호화된 이동식 저장장치로 중앙
관리자 PC에 전달한다.

Windows PowerShell의 OpenSSH 예시:

```powershell
scp pi@192.0.2.41:/home/pi/edge-001-control.token `
  "$env:USERPROFILE\AI_CCTV\edge-001-control.token"
```

전송이 끝나면 Edge에 만든 전달용 복사본만 삭제한다. 원본
`/etc/ai-cctv-edge/recovery.token`은 삭제하지 않는다.

```bash
rm /home/pi/edge-001-control.token
```

### 3.2 중앙에서 Edge 등록

Configurator GUI를 사용할 수 있으면 `Register Edge and camera`에 동일한 값을
입력하고 게시 자격증명 저장 경로를 지정한다. GUI를 사용할 수 없는 경우 중앙
CLI에서 다음과 같이 등록한다. 관리자 비밀번호는 기본적으로 숨김 Prompt로 받으며
자동화할 때만 보호된 `--password-file`을 사용한다.

```powershell
AI_CCTV_CLI.exe edge-register cam-001 `
  --server-url https://cctv.example.com `
  --name Entrance `
  --edge-device-id edge-001 `
  --management-url http://192.0.2.41:8003 `
  --recovery-url http://192.0.2.41:8002 `
  --edge-auth-token-file "$env:USERPROFILE\AI_CCTV\edge-001-control.token" `
  --publish-credentials-output "$env:USERPROFILE\AI_CCTV\cam-001-publish.json"
```

관리·복구 URL은 중앙 서버에서 Edge로 접근할 수 있는 신뢰 LAN 주소다. 두 Port는
독립 계약이므로 한 주소에서 다른 주소를 추측하지 않는다. 성공 시 중앙은 평문
게시 비밀번호를 콘솔에 표시하지 않고 지정한 비공개 JSON 파일에 한 번만 저장한다.

### 3.3 게시 자격증명을 Edge로 가져오기

중앙에서 생성한 JSON을 해당 Camera의 Edge로만 전송한다.

```powershell
scp "$env:USERPROFILE\AI_CCTV\cam-001-publish.json" `
  pi@192.0.2.41:/home/pi/cam-001-publish.json
```

Edge에서 파일 권한을 제한한 뒤 대화형 setup을 실행한다.

```bash
chmod 600 /home/pi/cam-001-publish.json
sudo ai-cctv-edge setup \
  --publish-credentials-file /home/pi/cam-001-publish.json
```

Prompt에는 다음처럼 입력한다.

```text
Device ID: edge-001
Camera ID: cam-001
RTSP mode: central_publish
Central server address: 192.0.2.10
Video profile: hd
Supported profiles: hd,fhd
Backup root: /var/lib/ai-cctv-edge/recordings
```

setup은 JSON의 `camera_id`와 `username`이 입력한 Camera ID와 일치하는지 먼저
검사한다. 검사에 실패하면 기존 실행 설정과 Profile 선택을 바꾸지 않는다. 성공하면
게시 비밀번호를 `/etc/ai-cctv-edge/publish.password`에 분리 저장하고 세 Service를
재시작한다.

성공 후 중앙과 Edge에 남은 두 전달용 파일을 삭제한다.

```bash
rm /home/pi/cam-001-publish.json
```

```powershell
Remove-Item -LiteralPath "$env:USERPROFILE\AI_CCTV\cam-001-publish.json"
Remove-Item -LiteralPath "$env:USERPROFILE\AI_CCTV\edge-001-control.token"
```

## 4. 설치 확인

Edge에서 다음 명령을 실행한다.

```bash
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
systemctl is-enabled ai-cctv-edge.service \
  ai-cctv-edge-control.service ai-cctv-edge-recovery.service
systemctl is-active ai-cctv-edge.service \
  ai-cctv-edge-control.service ai-cctv-edge-recovery.service
```

`doctor`는 Camera 열거, GStreamer Element, Encoder, 백업 쓰기, 중앙 RTSP TCP와
Secret 파일을 확인한다. 중앙 방화벽이나 MediaMTX가 아직 준비되지 않았다면 중앙
RTSP만 `WARN`일 수 있지만 Camera·GStreamer·Secret의 `ERROR`는 해결 후 운영한다.

중앙 GUI 또는 CLI에서도 확인한다.

```powershell
AI_CCTV_CLI.exe camera-status cam-001 `
  --server-url https://cctv.example.com
AI_CCTV_CLI.exe video-profile cam-001 `
  --server-url https://cctv.example.com
```

마지막으로 재부팅 후 세 Service가 다시 `active`인지, 중앙 Live/HLS와 녹화가
이어지는지 확인한다.

```bash
sudo reboot
```

## 5. 일상 운영과 장애 진단

```bash
sudo ai-cctv-edge status
sudo ai-cctv-edge doctor
sudo ai-cctv-edge logs
sudo ai-cctv-edge restart
```

`logs`는 세 Service의 Journal을 함께 Follow한다. Token과 RTSP URL 전체를 이슈
보고서에 복사하지 않는다. 진단 시 Camera 입력 장애와 중앙 연결 장애를 별도로
판단한다.

## 6. 업그레이드와 Rollback

새 패키지와 체크섬을 받은 뒤 같은 방법으로 검증하고 설치한다.

```bash
sha256sum -c ai-cctv-edge_0.3.1_arm64.deb.sha256
sudo apt install ./ai-cctv-edge_0.3.1_arm64.deb
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
```

업그레이드는 기존 `config.toml`, Token, 게시 비밀번호와 `/var/lib/ai-cctv-edge`
영상을 보존한다. 설치 전 활성 상태였던 Service만 새 wheel 설치 후 재시작한다.
Rollback은 보관한 이전 `.deb`를 `apt install`하고 같은 검사를 반복한다. DB나 Edge
백업을 삭제하는 명령은 Rollback 절차에 포함하지 않는다.

## 7. 제거

```bash
sudo apt remove ai-cctv-edge
```

제거 시 세 Service는 중지·비활성화되지만 운영 설정, Token과 로컬 백업은 복구를
위해 자동 삭제하지 않는다. 해당 데이터를 완전히 지우려면 백업 여부와 정확한
경로를 별도로 확인한 뒤 관리자가 명시적으로 처리한다.

## 8. Release용 `.deb` 빌드

이 절은 배포 담당자용이다. 잘못된 플랫폼 wheel 혼입을 막기 위해 x64 PC에서
Cross-build하지 않고 신뢰할 수 있는 ARM64 Builder에서 실행한다.

필요 도구:

- ARM64 Linux와 Python 3.11/pip
- `dpkg-deb`, GNU coreutils, Git
- 독립적으로 출처와 SHA-256을 확인한 ARM64 MediaMTX **v1.9.0** 바이너리
- Python wheel을 최초 수집할 Package Index 연결

```bash
export MEDIAMTX_BINARY="$PWD/vendor/mediamtx"
export MEDIAMTX_SHA256='<release-manifest에서 확인한 64자리 SHA-256>'
export MEDIAMTX_VERSION='v1.9.0'
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export OUTPUT_DIR="$PWD/dist/edge"

sh edge/packaging/build_deb.sh
```

버전 정본은 `edge/pyproject.toml`이다. 빌드는 Debian `control`의 Version이 다르면
중단한다. `constraints.txt`가 직접·전이 Python 의존성 버전을 고정하며, 패키지에는
설치 시 외부 Package Index가 필요하지 않도록 ARM64 wheelhouse를 포함한다.
MediaMTX는 스크립트가 다운로드하지 않으며 입력 SHA-256과 자체 Version 출력을 모두
검증한다.

결과:

```text
dist/edge/ai-cctv-edge_<version>_arm64.deb
dist/edge/ai-cctv-edge_<version>_arm64.deb.sha256
```

빌드 스크립트는 자동으로 다음 검증을 수행한다.

- Package/Version/Architecture
- CLI, MediaMTX, config, 세 systemd unit, build-info 존재 여부
- Maintainer script 문법과 conffile 선언
- ARM64 `pydantic-core` wheel 및 x86 wheel 부재
- 생성형 Token·게시 비밀번호가 `.deb`에 포함되지 않았는지 여부
- 패키지 내부 MediaMTX SHA-256

기존 Artifact를 독립적으로 다시 확인할 수도 있다.

```bash
sh edge/packaging/verify_deb.sh \
  dist/edge/ai-cctv-edge_0.3.0_arm64.deb \
  "$MEDIAMTX_SHA256"
(cd dist/edge && sha256sum -c ai-cctv-edge_0.3.0_arm64.deb.sha256)
```

`SOURCE_DATE_EPOCH`, 소스, MediaMTX, 빌드 도구 버전 및 Package Index에서 받은
wheel Byte가 같으면 Archive Timestamp와 압축 설정도 같게 만든다. Release 전에는
동일한 깨끗한 Builder에서 두 번 빌드해 두 `.deb`의 SHA-256이 같은지 별도로
확인한다. Package 내부
`/usr/share/doc/ai-cctv-edge/build-info`에는 Commit, Dirty 여부, Epoch와 MediaMTX
Version/Hash가 기록된다.

## 9. 출고 검증 체크리스트

- [ ] 깨끗한 Raspberry Pi OS Bookworm ARM64에서 체크섬 검증과 신규 설치
- [ ] 설치 직후 Capture가 예제 주소로 시작하지 않음
- [ ] Token 전달 파일과 게시 자격증명 파일이 `0600`이며 화면에 값이 노출되지 않음
- [ ] 중앙 등록 Camera ID와 Edge setup Camera ID가 일치함
- [ ] `doctor`에 Camera·GStreamer·Encoder·Secret `ERROR`가 없음
- [ ] 세 Service가 설치 직후와 재부팅 후 모두 `active`
- [ ] HD 1280×720@30fps·2Mbps 게시, HLS, 녹화와 추론 이벤트 확인
- [ ] FHD 지원 장치에서만 1920×1080@30fps·4Mbps 적용 확인
- [ ] 중앙 LAN 단절 중 로컬 MPEG-TS 백업과 복구 후 업로드 확인
- [ ] 이전 Release에서 Upgrade와 Rollback 시 설정·Token·백업 보존
- [ ] `apt remove`가 Service를 중지하고 운영 데이터를 보존
- [ ] Package 검증 스크립트와 이중 빌드 SHA-256 비교 통과

실제 Camera·Encoder·UPS·네트워크·재부팅 시험이 끝나기 전에는 ARM64 설치 프로그램을
소비자 검증 완료로 표시하지 않는다.
