# Build on Windows with: pyinstaller configurator/packaging/ai_cctv_configurator.spec
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("pydantic")

a = Analysis(
    ["configurator/__main__.py"],
    pathex=[".", "src"],
    binaries=[],
    datas=[
        ("src", "src"),
        ("server/compose.yml", "server"),
        ("server/nginx", "server/nginx"),
        ("server/mediamtx", "server/mediamtx"),
    ],
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
    console=False,
)
