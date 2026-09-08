# Windows GUI 실행 파일의 PyInstaller 빌드 정의다.
# 빌드 명령: pyinstaller configurator/packaging/ai_cctv_configurator.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

repository_root = Path(SPECPATH).resolve().parents[1]
# 실행 중 동적으로 불러오는 모듈도 포함해 개발 PC 밖에서 모듈 누락 오류가 나지 않게 한다.
hiddenimports = collect_submodules("pydantic")

a = Analysis(
    [str(repository_root / "configurator" / "gui_entry.py")],
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
    name="AI_CCTV_Configurator",
    # GUI는 별도 콘솔 창을 열지 않으며 보호된 운영 설정을 쓰기 위해 관리자 권한을 요청한다.
    console=False,
    uac_admin=True,
)
