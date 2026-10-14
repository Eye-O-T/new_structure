"""Canonical entry point for the first-time installer GUI."""

from __future__ import annotations

import logging
import sys


def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from PyQt5.QtWidgets import QApplication

        from server.installer.gui import InstallerWindow
    except ImportError as exc:
        raise RuntimeError("설치 관리자 GUI 의존성을 설치해 주세요.") from exc

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI CCTV 서버 설치 관리자")
    window = InstallerWindow()
    window.show()
    return app.exec_()
