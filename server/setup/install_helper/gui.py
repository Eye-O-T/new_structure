"""Windows 설치 마법사의 진입점. CLI에서는 Qt를 불러오지 않는다."""

import sys


# GUI 진입 시에만 Qt를 가져와 CLI의 Qt 의존성을 피하고 창의 수명을 이벤트 루프까지 유지한다.
def run() -> int:
    try:
        from PyQt5.QtWidgets import QApplication

        from .wizard import InstallerWindow
    except ImportError as exc:
        raise RuntimeError("설치 도우미의 GUI 의존성을 설치해 주세요.") from exc

    app = QApplication(sys.argv)
    app.setApplicationName("AI CCTV 서버 설치 도우미")
    window = InstallerWindow()
    window.show()
    return app.exec_()
