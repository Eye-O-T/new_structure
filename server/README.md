# 서버 호스트 도구

Windows 호스트 도구는 컨테이너와 분리된 세 개의 프로그램으로 구성됩니다.

| 프로그램 | 책임 | 실행 |
|---|---|---|
| Server Installer | 최초 설치, 설정·모델·TLS 준비, 최초 기동 | `python -m server.installer` |
| Server Manager | Compose 수명주기, AI 서비스 토글, 네트워크 설정, 진단 | `python -m server.desktop.server_manager` |
| CCTV Admin | 카메라, 영상, 이벤트, Edge 온보딩 | `python -m server.desktop.admin` |

`server.operations`는 Compose와 네트워크·진단의 호스트 동작을 제공하며 GUI를 import하지 않습니다. `server.deployment`는 설치와 운영이 함께 쓰는 배포 경로·검증 모델입니다. `server.edge_onboarding`은 Edge 검색·pairing·서버 API 프로토콜을 제공합니다.

개발 환경에서는 다음 순서로 실행합니다.

```powershell
uv sync --project server/setup/install_helper --locked
uv run --project server/setup/install_helper --locked python -m server.installer
uv run --project server/setup/install_helper --locked python -m server.desktop.server_manager
uv run --project server/setup/install_helper --locked python -m server.desktop.admin
```

기존 `server.setup.install_helper.*` 경로는 호환성을 위해 유지됩니다. 신규 코드와 문서는 위 경로를 사용해야 합니다.
