# AI CCTV 사용자 안내

이 안내는 **졸업작품 시연용 시스템**의 최초 설치와 일상적인 사용을 설명합니다. Edge와
Windows Server는 같은 신뢰 LAN에 둡니다. 목표 동시 시청 수는 2대이며, 최대 4대 이하의
동시 시청을 지원 범위로 확인합니다. 인터넷에 Edge 관리·복구 포트나 RTSP 포트를 공개하지
마십시오.

## 설치와 첫 실행

Windows PC에 Docker Desktop의 Linux 컨테이너 엔진, Python 3.11, Git, `uv`를 준비하고
저장소의 루트 README에 있는 설치 명령으로 Server Installer를 실행합니다. Installer에서
데이터 폴더, 관리자 계정, LAN에서 사용할 서버 주소, HTTP 또는 HTTPS를 설정하고 설치를
시작합니다. HTTPS를 선택하면 인증서와 키를 먼저 준비해야 합니다. 설치 후 Server Manager의
상태 화면에서 Data, External, MediaMTX, Nginx가 준비 상태인지 확인합니다.

Raspberry Pi OS Bookworm ARM64 Pi에 Edge DEB를 설치한 뒤 `sudo ai-cctv-edge pair`를
실행합니다. Server Desktop의 카메라 연결 화면에서 Edge를 검색하고 장치 정보를 확인한 뒤
중앙 RTSP 주소와 카메라 연결을 등록합니다. 검색 광고는 신뢰 LAN에서 장치를 찾기 위한
것이며 장치 인증을 대신하지 않습니다. 등록 이후 Control·Recovery 요청은 Edge 토큰으로
인증합니다. 상세한 수동 설치와 패키지 빌드는 [Edge 안내](../edge/README.md)를 따릅니다.

## 영상·이벤트 확인

Server Desktop에서 카메라를 선택하면 RTSP 우선으로 재생하고 필요한 경우 인증 HLS로
전환합니다. 복수 화면은 지원 범위 내에서 2대 동시 시청을 기본 시연 구성으로 사용하고,
4대 이하 최대 구성은 실제 대상 PC에서 부하를 확인합니다. Android 앱에는 Windows Server의
LAN 주소와 계정을 입력합니다. 서버 주소에 `/api/v1`을 붙이지 않습니다. 이벤트 상세의
이미지와 녹화 재생은 로그인 세션으로 보호됩니다.

사람 위치·인물 연결 결과는 AI 작업이 처리된 후 보일 수 있으며, 감지 결과는 실제 사람의
신원 확인을 의미하지 않습니다. 이벤트 이미지에 사람이 보이지 않거나 연결 ID가 없으면
먼저 Preprocessing·Analysis 서비스의 준비 상태와 처리 결과를 확인합니다.

## 문제가 생겼을 때

Manager의 진단과 로그를 먼저 확인합니다. Pi에서는 `sudo ai-cctv-edge doctor`,
`sudo ai-cctv-edge status`, `sudo ai-cctv-edge logs`를 사용합니다. Server와 Pi가 같은
LAN에 있고 AP isolation이 꺼져 있는지, Windows 방화벽이 필요한 LAN 연결만 허용하는지,
현재 서버 주소가 Pi 등록 정보와 일치하는지 확인합니다. Edge 녹화 복구와 DB 백업은
[운영·복원 안내](operations.md)를 참고합니다.
