# Windows 명령줄 도구를 Python 미설치 PC에서도 실행할 수 있도록 묶는 빌드 정의다.
# 빌드 명령:
# pyinstaller server/setup/install_helper/packaging/ai_cctv_cli.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# 작업 디렉터리 대신 spec 위치에서 저장소 루트를 계산해 빌드 호출 위치의 영향을 없앤다.
repository_root = Path(SPECPATH).resolve().parents[3]
# 코드 분석만으로 찾기 어려운 Pydantic 내부 모듈까지 실행 파일에 포함한다.
hiddenimports = collect_submodules("pydantic")

# CLI 진입점과 공유 설정 패키지만 분석의 출발점으로 삼아 별도의 콘솔 실행 파일을 만든다.
a = Analysis(
    [str(repository_root / "server" / "setup" / "install_helper" / "cli_entry.py")],
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
