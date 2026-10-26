# 서버 호스트 도구

Windows 호스트 도구는 Docker 서비스와 수명주기가 분리되어 있습니다.

| 프로그램 | 책임 | 실행 |
|---|---|---|
| 서버 설치 도우미 | 최초 설치, 설정·모델·TLS 준비, 최초 기동 | `python -m server.installer` |
| 서버 제어판 | Compose 수명주기, AI 서비스 토글, 네트워크 설정, 진단 | `python -m server.desktop.server_manager` |
| CCTV 관제 화면 | 카메라, 영상, 이벤트, Edge 온보딩 | `python -m server.desktop.admin` |
| Host Helper | Edge Discovery·Probe·Pairing | `python -m server.host_helper {discover,probe,pair}` |
| Local Camera Publisher | DirectShow 웹캠의 RTSP 송출 | CLI에서 시작·중지 |

`server.operations`는 Compose와 네트워크·진단의 호스트 동작을 제공하며 GUI를 import하지 않습니다. `server.deployment`는 설치와 운영이 함께 쓰는 배포 경로·검증 모델입니다. `server.edge_onboarding`은 Edge 검색·pairing·서버 API 프로토콜을 제공합니다.

개발 환경에서는 다음 순서로 실행합니다.

```powershell
uv sync --project server/setup/install_helper --locked
uv run --project server/setup/install_helper --locked python -m server.installer
uv run --project server/setup/install_helper --locked python -m server.desktop.server_manager
uv run --project server/setup/install_helper --locked python -m server.desktop.admin
```

일반 운영에서는 저장소 루트의 `ai_cctv.ps1`을 사용합니다. `gui`는 기본 Docker
서비스를 준비한 뒤 선택한 AI 서비스와 Local Camera Publisher를 적용하고 CCTV 관제 화면을
실행합니다. `cli stop`만 Publisher와 Docker 서비스를 함께 종료하며, 관제 화면을
닫아도 둘은 종료되지 않습니다.

```powershell
.\ai_cctv.ps1 gui -Preprocessing on -Analysis on
.\ai_cctv.ps1 cli local-camera devices
.\ai_cctv.ps1 cli local-camera on --device 'USB Camera'
.\ai_cctv.ps1 cli edge discover
.\ai_cctv.ps1 cli stop
```

`edge probe`와 `edge pair`는 `--input <json-file>` 또는 표준 입력으로 구조화된 JSON을
받습니다. 운영 토큰과 게시 비밀번호는 명령행 인수나 일반 출력에 넣지 않습니다.

기존 `server.setup.install_helper.*` 경로는 호환성을 위해 유지됩니다. 신규 코드와 문서는 위 경로를 사용해야 합니다.
