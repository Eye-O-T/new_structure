# Edge–Server 페어링 절차서

작성일: 2026-11-11 (KST)

이 문서는 Raspberry Pi Edge 패키지가 설치되어 있고, Windows Server를 Server Manager에서 실행하는 구성을 기준으로 한다. **현재 코드에서는 아래의 수동 연결 절차를 사용한다. GUI 자동 연결 절차는 별도 수정 계획의 구현 후 사용할 수 있다.**

페어링은 중앙에 카메라·Edge 정보를 등록하고, 중앙 송출 계정과 운영 토큰을 Pi에 맞추는 작업이다. 검색 성공, 설정 완료, 관리 연결, 영상 수신은 각각 확인한다. 현재 Edge 캡처 관련 수정·실기 검증 사항은 [조사 보고서](edge-server-connection-audit-2026-11-11.md)를 참고한다.

## 1. 준비할 값

아래 주소와 사용자명은 예시다. 실제 환경의 값으로 바꾼다.

| 항목 | 예시 | 용도 |
|---|---|---|
| Server PC의 LAN IPv4 | `192.168.0.10` | Pi가 영상을 보내는 목적지 |
| Server 관리 API 주소 | `http://127.0.0.1` | 같은 PC에서 CLI로 중앙 등록; 예시는 HTTP 80 배포 |
| Edge의 LAN IPv4 | `192.168.0.41` | 중앙에서 Pi에 접근하는 주소 |
| Pi 사용자 | `pi` | SSH/SCP 사용자; 실제 사용자로 변경 |
| 장치 ID | `edge-001` | 장치 식별 |
| 카메라 ID | `cam-001` | 중앙 등록·게시 계정·Pi 설정에서 같은 값 사용 |
| Pi 네트워크 인터페이스 | `eth0` | 실제 IPv4 인터페이스 사용; Wi-Fi이면 해당 인터페이스로 변경 |
| MAC 주소 | Pi에서 조회한 값 | 위 인터페이스의 MAC 사용 |
| Edge 관리·복구 포트 | `8003`, `8002` | 최초 설정/상태·제어, 로컬 녹화 복구 |
| 중앙 RTSP 포트 | `8554` | 영상 송출 목적지 |

관리 API가 HTTPS이면 설치된 HTTPS 주소를 사용한다. CLI는 HTTPS 또는 같은 PC의 loopback HTTP를 지원하며, 원격 LAN HTTP 주소를 허용하는 CLI 옵션은 현재 없다. HTTPS CLI는 운영체제가 신뢰하는 인증서를 사용한다. 추가 CA 파일을 사용하는 관제 GUI의 인증서 설정이 CLI에 자동 전달되는 것은 아니다.

## 2. Server 준비

저장소 루트의 PowerShell에서 기존 제어판을 연다. 이미 실행 중이면 해당 창을 사용한다.

```powershell
.\ai_cctv.ps1 cli manager
```

제어판에서 서버를 시작하고 서비스가 ready인지 확인한다. 네트워크 설정의 RTSP bind 주소는 Pi가 접근할 수 있는 PC의 LAN 주소로 지정한다. 기본 loopback bind만 열려 있으면 Pi가 접속할 수 없다. GUI에 표시되는 값과 실제 적용 상태를 확인한다.

| 통신 방향 | 포트 | 필요한 단계 |
|---|---|---|
| Pi → Windows PC | UDP 37020 | 자동 검색 |
| Windows PC → Pi | TCP 8003 | 최초 설정 전달·health 확인 |
| 중앙 서버 컨테이너 → Pi | TCP 8003, 8002 | 운영 상태·제어, 녹화 복구 |
| Pi → Windows PC | TCP 8554 또는 실제 RTSP 포트 | 영상 송출 |

Pi와 PC는 같은 IPv4 LAN에 둔다. 검색이 필요하면 Windows의 해당 네트워크 방화벽과 공유기 AP 격리 설정도 확인한다. PC에서 Pi에 접속되는 것만으로 컨테이너에서도 관리·복구 주소에 접근된다고 단정하지 않는다.

## 3. Pi 확인과 신규/기존 장치 구분

Pi에서 인터페이스·카메라·시간 동기화를 확인한다.

```bash
ip -br -4 addr
cat /sys/class/net/eth0/address
rpicam-hello --list-cameras
timedatectl status
```

MAC 조회의 `eth0`도 실제 사용하는 인터페이스로 바꾼다. 장치마다 장치 ID와 카메라 ID가 중복되지 않게 정한다.

설정 완료 표시가 있는지 확인한다.

```bash
if sudo test -f /etc/ai-cctv-edge/.configured; then
  echo "configured"
else
  echo "unconfigured"
fi
```

`configured`이면 이 문서의 신규 등록을 반복하지 않고 **7절의 기존 장치 점검**으로 이동한다. 기존 Edge를 다른 Server로 옮길 때는 **8절의 Server 이전**을 따른다. 중앙에는 이미 등록되어 있지만 Pi 설정만 미완료인 경우에도 새 중앙 등록을 하지 않고 기존 게시 계정 전달 파일로 5절부터 이어간다.

## 4. 현재 코드에서 가능한 수동 연결

현재 관제 GUI의 자동 연결 패널은 연결되지 않았고 수동 등록 폼에도 MAC 필드가 빠져 있다. 신규 중앙 등록은 CLI로 진행한다. 수동 연결에는 `pair` 실행이 필요하지 않으며, 이미 실행 중이면 Pi 터미널에서 `Ctrl+C`로 종료한다.

### 4-1. Pi의 기존 운영 토큰 내보내기

Pi에서 다음 명령을 실행한다.

```bash
sudo ai-cctv-edge export-auth-token --output "$HOME/edge-001-control.token"
```

출력 파일이 이미 있으면 기존 파일을 보존하고 다른 새 파일명을 사용한다. 토큰을 명령행 인수로 직접 쓰지 않는다.

수동 연결에서는 **Pi에서 내보낸 바로 그 토큰을 중앙에 등록**한다. 게시 계정 JSON에는 이 운영 토큰이 들어 있지 않으며, Pi의 `setup`은 기존 운영 토큰을 유지한다. 중앙에만 다른 토큰을 입력하면 이후 관리 API 인증이 맞지 않는다.

### 4-2. Windows로 토큰 파일 가져오기

서버 PC에서 별도 PowerShell을 열고 저장소 루트로 이동한다. 아래 변수는 이후 단계에서도 같은 터미널에서 사용한다.

```powershell
$edgeIp = '192.168.0.41'
$edgeUser = 'pi'
$edgeMac = 'dc:a6:32:12:34:56'
$serverBaseUrl = 'http://127.0.0.1'
$handoffDir = Join-Path $env:LOCALAPPDATA 'AI_CCTV\edge-onboarding\edge-001'
New-Item -ItemType Directory -Path $handoffDir -Force | Out-Null
$tokenPath = Join-Path $handoffDir 'edge-001-control.token'
$credentialsPath = Join-Path $handoffDir 'cam-001-publish.json'
scp "${edgeUser}@${edgeIp}:/home/${edgeUser}/edge-001-control.token" $tokenPath
```

`$edgeMac`는 3절에서 Pi가 출력한 실제 MAC으로 바꾼다. 예시는 Pi 홈이 `/home/<사용자명>`인 경우다. 다른 홈 경로이면 SCP 원본 경로도 바꾼다. `$serverBaseUrl`은 실제 배포의 스킴·포트에 맞추고 `/api/v1`을 붙이지 않는다. 이 예시는 Server PC 자신에게 HTTP 80으로 접속하는 경우다.

### 4-3. 중앙에 한 번 등록하고 게시 계정 저장

아래 명령은 중앙 카메라를 생성한다. 같은 ID가 이미 등록되어 있으면 반복 실행하지 않는다. 전달 파일은 해당 장치의 새 작업 파일 경로를 사용한다.

```powershell
.\ai_cctv.ps1 cli edge-register cam-001 `
  --server-url $serverBaseUrl `
  --username admin `
  --name Entrance `
  --edge-device-id edge-001 `
  --edge-mac-address $edgeMac `
  --management-url "http://${edgeIp}:8003" `
  --recovery-url "http://${edgeIp}:8002" `
  --edge-auth-token-file $tokenPath `
  --publish-credentials-output $credentialsPath
```

중앙 관리자 비밀번호는 숨김 입력으로 받는다. 계정명이 `admin`이 아니면 바꾼다. 성공하면 `Publish credentials saved to:` 뒤 경로를 확인한다. 출력 JSON의 비밀번호가 가려지는 것은 정상이며 실제 게시 계정은 보호 파일에 저장된다.

명령이 실패하면 오류를 확인한 뒤 다음 단계로 진행한다. `409`이면 먼저 기존 카메라를 확인한다. 중앙 등록은 완료됐으나 파일 저장만 실패한 경우에도 등록 명령을 반복하지 않는다. 기존 계정 파일을 보존하고, 파일을 잃은 경우에만 등록된 카메라의 게시 계정 재발급 절차를 사용한다.

### 4-4. 게시 계정 파일을 Pi로 전달

중앙 등록과 파일 저장 성공을 확인한 뒤 실행한다.

```powershell
scp $credentialsPath "${edgeUser}@${edgeIp}:/home/${edgeUser}/cam-001-publish.json"
```

## 5. Pi에 설정 적용

Pi에서 파일 권한을 제한하고 설정을 시작한다.

```bash
chmod 600 "$HOME/cam-001-publish.json"
sudo ai-cctv-edge setup --publish-credentials-file "$HOME/cam-001-publish.json"
```

대화형 입력은 중앙 등록과 맞춘다.

| 입력 항목 | 위 예시의 값 |
|---|---|
| Device ID | `edge-001` |
| Camera ID | `cam-001` |
| RTSP mode | `central_publish` |
| Central server address | `192.168.0.10` — Server PC의 LAN IP |
| Video profile | `hd` |
| Supported profiles | 실제 장치에서 사용할 프로필; 기본 예시 `hd,fhd` |
| Backup root | `/var/lib/ai-cctv-edge/recordings` |

중앙 주소에는 `http://`, `https://`, `/cam-001`, 포트를 붙이지 않는다. 기본 `setup`은 RTSP 8554를 사용한다. 서버가 다른 포트이면 설정 후 `/etc/ai-cctv-edge/config.toml`의 `[rtsp] central_port`를 실제 포트로 바꾸고 `sudo ai-cctv-edge restart`를 실행한다.

기본 설정 경로에서 `setup`이 성공하면 설정 완료 표시를 남기고 캡처·Control·Recovery 서비스를 자동 시작 및 부팅 활성화한다. 이미 생성된 중앙 카메라를 다시 등록할 필요는 없다.

## 6. 설정 완료와 실제 영상 확인

Pi에서 확인한다.

```bash
sudo ai-cctv-edge status
sudo ai-cctv-edge doctor
```

필요하면 `sudo ai-cctv-edge logs`로 로그를 조회한다. `Ctrl+C`는 로그 조회만 종료한다.

PC에서는 관리 서비스와 응답 ID를 확인한다.

```powershell
Invoke-RestMethod "http://${edgeIp}:8003/health/live" | Format-List
.\ai_cctv.ps1 cli admin
```

health 응답의 `device_id`, `camera_id`가 등록한 값과 같은지 직접 확인한다. `status=alive`는 관리 서비스가 실행 중이라는 뜻이며 영상 수신 완료를 뜻하지 않는다.

관제 화면에서 **설정 → 관리자 로그인 → 카메라 등록 · 운영 설정 → 목록 새로고침 → 해당 카메라 선택 → 선택한 카메라 상태 · 지원 화질 조회**로 진행한다.

| 확인 | 완료 기준 |
|---|---|
| 중앙 등록 | 목록에 해당 카메라 ID가 존재한다. |
| Pi 설정 적용 | `.configured`와 설정 파일이 존재하고 관리 API가 해당 ID를 응답한다. |
| 관리 연결 | `edge_online=true`, `last_seen_at`이 최근 시각으로 갱신된다. |
| 카메라 입력 | `camera_input=online`이다. |
| 송출·녹화 | 관제 화면에서 `녹화 시작` 후 중앙 연결 `online`, live 영상, 중앙 녹화를 확인한다. |

기본 등록 상태는 송출·녹화 중지다. 관리 연결이 확인되더라도 사용자가 녹화를 시작하기 전에는 영상이 없을 수 있다. 녹화 중지 상태의 `central_connection_status=unknown`도 정상일 수 있다.

`doctor`의 TCP 연결 성공만으로 영상 성공을 판정하지 않는다. 현재 캡처 시작·재시작 관련 확인 사항 때문에 설정 전달과 관리 연결이 정상이어도 영상 동작은 Pi에서 별도로 검증해야 한다.

성공을 확인한 뒤 PC·Pi의 토큰 및 게시 계정 **전달 복사본**을 정리한다. Pi의 `/etc/ai-cctv-edge/`에 있는 운영 토큰·게시 비밀번호·설정 파일은 보존한다.

## 7. 기존 장치 점검

이미 설정된 Pi에 `pair`를 다시 실행하지 않는다. Pi에서는 `status`, `doctor`, `logs`를 사용하고 PC에서는 기존 카메라의 상태를 조회한다.

Discovery 광고는 최초 `pair` 모드에서만 송신한다. 이미 설정된 장치가 `edge discover`에 나타나지 않는 것만으로 오프라인이라고 판단하지 않는다.

IP가 바뀌었다면 등록된 장치의 관리·복구 URL을 실제 Pi 주소로 수정한다. 중앙 관리 API의 토큰과 Pi의 운영 토큰이 같은지 확인하고, 카메라를 새로 만들지 않는다. 게시 계정 재발급은 기존 송출 비밀번호를 바꾸는 작업이므로 단순 상태 확인에 사용하지 않는다.

## 8. 기존 Edge를 새로운 Server로 이전

**현재 코드에서도 수동 이전은 가능하다.** `.configured`가 있는 Edge는 `pair`로 다시 검색·등록할 수 없지만, `setup`과 그 별칭인 `configure`는 기존 설정 여부와 관계없이 설정을 다시 쓸 수 있다. 완료 표시를 삭제하거나 패키지를 재설치할 필요가 없다.

| 경우 | 필요한 작업 |
|---|---|
| 같은 Server의 IP만 변경되거나 등록·게시 자격 증명까지 그대로 이관된 경우 | Pi 설정의 `[rtsp] central_host`, 필요 시 `central_port`를 바꾸고 Edge 서비스를 재시작한다. 기존 게시 계정이 유지되는지 먼저 확인한다. |
| 다른 Server의 새 DB에 연결하는 경우 | 새 Server에 Edge를 등록하고 새 게시 계정을 받아 Pi에서 `configure`를 실행한다. |
| 새 Server에 해당 Edge/카메라가 이미 등록된 경우 | 기존 등록과 게시 계정을 사용한다. 중앙 등록을 반복하지 않는다. |
| GUI에서 이미 설정된 Edge를 다시 자동 페어링하려는 경우 | 현재 최초 페어링 API는 이를 거절한다. 이 절의 수동 이전 경로를 사용한다. |

### 8-1. 이전 전 확인

기존 `/etc/ai-cctv-edge/config.toml`, `publish.password`, `recovery.token`과 사용자 지정 포트·백업 경로·화질 값을 보관한다. 가능하면 장치 ID와 카메라 ID는 유지한다. 새 Server의 DB에서 같은 ID가 다른 장치에 사용되고 있지 않은지 확인한다.

**이전 Server의 제어 접근을 먼저 정리한다.** 현재 Server는 카메라가 비활성화되어 있어도 녹화 경로 동기화에서 Edge에 중지 요청을 보낼 수 있다. 기존 토큰까지 유지하면 그 요청이 새 Server의 녹화 요청과 충돌할 수 있다. 이전 Server를 더 사용하지 않는다면 해당 서비스를 중지한다. 계속 운영해야 한다면 대상 Edge의 관리 접근을 차단하거나, 양쪽 설정을 맞춰 운영 토큰을 교체하는 별도 조치가 필요하다. 카메라의 사용 설정을 중지하는 것만으로 이전이 격리된다고 가정하지 않는다.

그다음 Pi의 서비스를 멈춘다.

```bash
sudo ai-cctv-edge stop
```

### 8-2. 새 Server 등록과 Pi 재설정

1. 새 Server PC에서 서버를 실행하고 LAN RTSP 주소·포트를 확인한다.
2. 4-1·4-2와 같이 Pi의 운영 토큰을 내보내 새 Server PC에 전달한다. `configure`는 기존 토큰을 유지하므로 새 Server에도 같은 토큰을 등록한다.
3. 새 Server PC에서 4-3의 `edge-register`를 실행한다. 장치 ID·카메라 ID·MAC은 기존 Edge 값, 관리·복구 URL은 실제 Pi 주소를 사용한다. 새 Server에 이미 등록된 경우 이 단계는 생략하고 해당 게시 계정을 사용한다.
4. 새 Server가 발급한 게시 계정 JSON을 Pi에 전달한다. 같은 이름의 과거 전달 파일과 혼동하지 않도록 파일명에 새 Server를 표시한다.
5. Pi에서 다음 명령을 실행한다.

   ```bash
   chmod 600 "$HOME/cam-001-new-server-publish.json"
   sudo ai-cctv-edge configure --publish-credentials-file "$HOME/cam-001-new-server-publish.json"
   ```

6. Device ID·Camera ID·백업 경로 등은 보관한 기존 값을 **직접 재입력**하고, `Central server address`에는 새 Server의 LAN 주소를 입력한다. 게시 비밀번호는 새 JSON에서 읽는다. 기본 설정 경로에서 명령이 성공하면 서비스가 자동 활성화·재시작된다.
7. 6절대로 새 Server에서 관리 연결과 실제 영상·녹화를 확인한다. 이전 Server에서 상태를 조회해 새 연결의 성공 여부를 판단하지 않는다.

`configure`는 기존 TOML을 읽어서 중앙 주소만 고치는 명령이 아니다. 인코더, RTSP·관리·복구 포트, 보존 한도 등은 기본값으로 다시 만들어지며 입력 기본값도 기존 파일의 값이 아니다. 예를 들어 사용자 지정 포트 18554/18003/18002는 8554/8003/8002로 바뀐다. 사용자 지정 값이 있다면 자동 재시작 후 서비스를 다시 멈추고 설정 파일에 필요한 값을 반영한 다음 `sudo ai-cctv-edge restart`로 적용한다.

기존 `capture-control.json`의 녹화·송출 의도는 자동 초기화되지 않는다. 새 Server의 기본 녹화 상태 및 관제 화면에서 사용자가 원하는 상태와 맞는지 확인한다. 기존 로컬 녹화 파일은 이 명령으로 삭제하지 않지만, 이전 Server의 DB·이벤트·중앙 녹화 이력이 새 Server로 자동 이전되는 것은 아니다.

### 8-3. 확인 근거와 검증 범위

`edge/src/ai_cctv_edge/cli.py`의 원본 `setup`, `pair`, `main`을 임시 파일과 격리된 호출로 검사했다. 실제 서비스·네트워크 호출은 대역으로 교체했다.

- `.configured`가 있는 상태에서 `pair`가 거절되는 것을 확인했다.
- 같은 상태에서 `setup`으로 기존 중앙 주소와 게시 비밀번호를 새 값으로 변경하고 설정을 다시 읽는 데 성공했다.
- 재입력한 장치/카메라 ID·백업 경로, 기존 토큰·녹화 파일·완료 표시가 유지됨을 확인했다. 사용자 지정 포트의 기본값 초기화와 녹화 의도 유지도 확인했다.
- 기본 설정 경로의 `setup`과 `configure`가 동일한 처리 후 `enable`, `restart`를 요청함을 확인했다. 실제 systemd 실행은 하지 않았다.
- 이전 Server의 동기화 함수가 비활성 카메라에도 녹화·송출 중지 요청을 보내는 것을 대역으로 재현했다.

재현 스크립트는 `.review/edge_reconfigure_checks.py`에 있다. 이 검사는 설정 이전 가능성을 확인한 것이며 실제 Pi의 RTSP 송출 성공을 확인한 결과는 아니다.

## 9. Server GUI에서 신규 Edge 자동 연결

1. Pi에서 실제 인터페이스와 토큰을 준비한다. 4-1·4-2의 내보내기·전송 절차로 PC에 토큰 파일을 둔다.
2. 미등록 Pi에서 최초 설정 대기를 시작하고 터미널을 열어 둔다.

   ```bash
   sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --interface eth0
   ```

3. PC에서 `cli manager` → 서버 ready 확인 → `CCTV 관제 · Edge 연결 열기` → 설정에서 로그인 → `카메라 등록 · 운영 설정` → `신규 Edge 연결`로 이동한다.
4. `Edge 찾기`로 장치를 선택하고 ID·MAC·IP를 확인한다. 필요하면 현재 CLI의 다음 명령으로도 광고를 확인할 수 있다.

   ```powershell
   .\ai_cctv.ps1 cli edge discover --timeout 5
   ```

5. `연결 확인`을 수행한다. 카메라 이름을 입력하고 `고급 설정`에서 Pi 토큰 파일을 불러온다. Pi가 접근할 Server LAN 주소와 RTSP 포트, 화질을 확인한다. Server 주소가 `127.0.0.1`, `localhost`, `0.0.0.0`이면 Pi에서 접근할 수 없으므로 PC의 LAN 주소로 바꾼다.
6. `선택한 Edge 연결`을 한 번 실행한다. GUI는 기존 로그인 세션으로 중앙 등록, 보호된 게시 계정 파일 저장, Pi 설정 전달을 순서대로 처리한다. 성공하면 Pi의 `pair`가 종료되고 기본 서비스가 시작된다.
7. `등록된 카메라` 탭에서 관리 연결을 확인하고 필요한 경우 녹화를 시작해 영상과 중앙 녹화를 확인한다.

중앙 등록 후 Pi 전달만 실패하면 GUI의 전달 재시도 또는 저장된 게시 계정 파일로 5절부터 이어간다. 새 중앙 등록을 반복하지 않는다. 등록 요청 응답이 끊긴 경우에는 `등록된 카메라`에서 목록 새로고침으로 먼저 확인한다. `cli edge pair --input <JSON>`은 설정 전달용 Host Helper 호출이며 중앙 등록을 자동 수행하는 단일 명령이 아니다. GUI 구현 상세와 실행 검증 한계는 [Server GUI 수정 계획](server-edge-gui-interface-plan.md)에 기록했다.

## 10. 자주 만나는 상태

| 상태·오류 | 확인할 것 |
|---|---|
| 검색 결과 없음 | Pi가 미등록 `pair` 모드인지, `--interface`가 맞는지, 같은 LAN·UDP 37020·시간 동기화가 맞는지 확인한다. 수동 연결에는 검색이 필요 없다. |
| `Edge is already configured` | 신규 페어링을 멈추고 기존 장치 상태를 확인한다. 완료 표시를 임의 삭제해 다시 등록하지 않는다. |
| 중앙 등록 `409` | 기존 카메라와 전달 파일을 확인하고 남은 Pi 설정만 이어간다. |
| 관리 API 인증 실패 | 중앙에 등록한 운영 토큰과 Pi의 `recovery.token`이 같은지 확인한다. 게시 비밀번호와 혼동하지 않는다. |
| 관리 연결 정상, 영상 없음 | 녹화·송출 의도, Server RTSP bind/LAN 주소/포트, 게시 계정, Pi 캡처 로그를 순서대로 확인한다. |
| `CAPTURE_STATUS_STALE` | 관리 API는 살아 있으나 캡처 heartbeat가 오래된 상태다. Pi 캡처 서비스와 로그를 확인한다. |
| HTTPS 인증서 오류 | 중앙 CLI는 운영체제 신뢰 인증서를 사용한다. 추가 CA를 사용하는 GUI는 수정 후 기존 관제 세션의 인증서 설정을 재사용한다. |

신규 설치 및 패키지 설명은 [Edge README](../edge/README.md), 포트·컨테이너 네트워크 설명은 [구성·네트워크](architecture.md)를 참고한다.
