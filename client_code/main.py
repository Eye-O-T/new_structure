import os
import sys

from PyQt5.QtCore import QLibraryInfo
from PyQt5.QtWidgets import QApplication

from ui.gui import CCTVMainWindow


def main():
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = QLibraryInfo.location(
        QLibraryInfo.PluginsPath
    )

    app = QApplication(sys.argv)
    window = CCTVMainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
