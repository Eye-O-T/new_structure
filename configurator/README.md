# Windows Configurator

중앙 서버 설정과 Docker 실행을 돕는 Windows GUI·CLI다. 설치 파일을 사용하는 경우 Python·uv 없이 [설치 안내](../README.md#windows-설치)를 따른다. 아래는 소스 개발·빌드용이다.

## 개발 실행

Windows에서 Python 3.11·uv를 준비하고 저장소 루트에서 실행한다.

```powershell
uv sync --project configurator --locked --extra test --extra build
uv run --project configurator --locked python -m configurator
uv run --project configurator --locked python -m pytest -c configurator/pyproject.toml configurator/tests
```

공통 코드는 `../lib`의 로컬 패키지를 사용하며 `uv.lock`으로 개발·빌드 의존성 버전을 고정한다. 서버 기동에는 Docker Desktop이 필요하다.

## 설치 파일 빌드

Windows에서 Python 3.11·uv·Inno Setup 6를 준비하고 저장소 루트에서 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File .\configurator\packaging\build_windows_installer.ps1 -Version 0.3.0
```

빌드는 `uv.lock`에 따라 `build/windows-installer/.venv`에 별도 환경을 만들고 테스트·실행 파일 생성·설치 파일 생성을 수행한다. 설치 파일과 체크섬은 `dist/installer/`에 생성된다. 배포 전 실제 Windows에서 설치·업데이트·제거를 확인하고 실행 파일 서명을 준비한다.
