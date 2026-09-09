# Windows GUI 실행 파일의 PyInstaller 빌드 정의다.
# 빌드 명령: pyinstaller server/setup/install_helper/packaging/ai_cctv_install_helper.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# 작업 디렉터리 대신 spec 위치에서 저장소 루트를 계산해 다른 폴더에서 시작한 빌드도 허용한다.
repository_root = Path(SPECPATH).resolve().parents[3]
# 실행 중 동적으로 불러오는 모듈도 포함해 개발 PC 밖에서 모듈 누락 오류가 나지 않게 한다.
hiddenimports = collect_submodules("pydantic")

# 루트 패키지와 lib를 분석 경로에 넣어 설치 도우미 및 공유 설정 모듈을 함께 수집한다.
a = Analysis(
    [str(repository_root / "server" / "setup" / "install_helper" / "gui_entry.py")],
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
    name="AI_CCTV_Server_Install_Helper",
    # GUI는 별도 콘솔 창을 열지 않으며 보호된 운영 설정을 쓰기 위해 관리자 권한을 요청한다.
    console=False,
    uac_admin=True,
)
