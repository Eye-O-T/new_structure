# 일상적인 영상·카메라·이벤트 관리를 위한 비상승 권한 데스크톱 앱이다.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

repository_root = Path(SPECPATH).resolve().parents[3]
hiddenimports = collect_submodules("pydantic") + collect_submodules("av")

a = Analysis(
    [str(repository_root / "server" / "desktop" / "admin" / "gui_entry.py")],
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
    name="AI_CCTV_Admin",
    console=False,
    uac_admin=False,
)
