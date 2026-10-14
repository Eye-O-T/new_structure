# 블로킹 호스트·네트워크 작업을 Qt 이벤트 루프와 분리하고 결과는 신호로 전달한다.
"""Run blocking host and network work without blocking the Qt event loop."""

from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import QObject, QThread, pyqtSignal


class BackgroundTask(QThread):
    """Operations receive a progress callback and never access Qt widgets.

    Callers retain the task until ``finished`` and present only safe error text.
    Interruption is advisory: a running subprocess or request is not terminated.
    """

    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self,
        operation: Callable[[Callable[[str], None]], Any],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._operation = operation

    # 작업 결과·오류는 신호로 전달한다. 호출자는 화면에 노출해도 되는 오류 메시지를 만들어야 한다.
    def run(self) -> None:
        try:
            self.result.emit(self._operation(self.progress.emit))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            # Do not retain a closure containing passwords after completion.
            self._operation = None
