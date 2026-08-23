# Build on Windows with:
# pyinstaller configurator/packaging/ai_cctv_cli.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

repository_root = Path(SPECPATH).resolve().parents[1]
hiddenimports = collect_submodules("pydantic")

a = Analysis(
    [str(repository_root / "configurator" / "cli_entry.py")],
    pathex=[str(repository_root), str(repository_root / "src")],
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
    console=True,
)
