# Windows Configurator

일반 사용자가 중앙 서버를 설정하고 Docker 컨테이너를 시작하는 GUI·CLI다. 서버의 Python 개발 환경과 별도로 관리한다.

Windows에서 Python 3.11·uv를 준비하고 저장소 루트에서 실행한다.

```powershell
uv sync --project configurator --locked --extra test --extra build
uv run --project configurator --locked python -m configurator
uv run --project configurator --locked python -m pytest -c configurator/pyproject.toml configurator/tests
```

GUI를 사용해도 Docker Desktop은 필요하다. 실행 파일을 설치한 사용자는 Python·uv가 필요 없다. 패키징과 설치 절차는 [루트 README](../README.md#패키지-빌드)를 따른다. 공통 코드는 `../lib`의 로컬 패키지를 사용하며 `uv.lock`은 이 개발 환경의 버전을 고정한다.
