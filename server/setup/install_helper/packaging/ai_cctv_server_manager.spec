# Windows 호스트의 Compose·네트워크·진단 관리 화면이다.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

repository_root = Path(SPECPATH).resolve().parents[3]
hiddenimports = collect_submodules("pydantic")

a = Analysis(
    [str(repository_root / "server" / "desktop" / "server_manager" / "gui_entry.py")],
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
    name="AI_CCTV_Server_Manager",
    console=False,
    uac_admin=True,
)
