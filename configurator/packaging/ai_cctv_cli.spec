# Windows 명령줄 도구를 Python 미설치 PC에서도 실행할 수 있도록 묶는 빌드 정의다.
# 빌드 명령:
# pyinstaller configurator/packaging/ai_cctv_cli.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

repository_root = Path(SPECPATH).resolve().parents[1]
# 코드 분석만으로 찾기 어려운 Pydantic 내부 모듈까지 실행 파일에 포함한다.
hiddenimports = collect_submodules("pydantic")

a = Analysis(
    [str(repository_root / "configurator" / "cli_entry.py")],
    pathex=[str(repository_root), str(repository_root / "lib")],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AI_CCTV_CLI",
    # CLI는 명령 결과와 오류를 터미널에 표시해야 하므로 콘솔 입출력을 유지한다.
    console=True,
)
