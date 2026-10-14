# Detached Windows process that owns the local FFmpeg publisher.
from pathlib import Path

repository_root = Path(SPECPATH).resolve().parents[3]

a = Analysis(
    [str(repository_root / "server" / "local_camera_publisher" / "__main__.py")],
    pathex=[str(repository_root), str(repository_root / "lib")],
    binaries=[],
    datas=[],
    hiddenimports=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="AI_CCTV_Local_Camera",
    # The controller supplies one JSON document over stdin; CREATE_NO_WINDOW
    # keeps this console-capable helper invisible during normal operation.
    console=True,
)
