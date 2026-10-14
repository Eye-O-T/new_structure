# Short-lived Windows host process for Edge discovery and pairing.
from pathlib import Path

repository_root = Path(SPECPATH).resolve().parents[3]

a = Analysis(
    [str(repository_root / "server" / "host_helper" / "__main__.py")],
    pathex=[str(repository_root), str(repository_root / "lib")],
    binaries=[],
    datas=[],
    hiddenimports=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="AI_CCTV_Host_Helper",
    console=True,
)
