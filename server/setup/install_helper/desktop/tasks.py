"""Keep widgets alive until their bounded network operations finish."""

from ..qt_tasks import BackgroundTask
from .api import safe_error


class TaskOwner:
    task = None

    def execute(self, operation, completed):
        if self.task is not None:
            return
        self.body.setEnabled(False)
        self.message.setText("처리 중…")

        def guarded(_progress):
            try:
                return operation()
            except Exception as exc:
                raise RuntimeError(safe_error(exc)) from None

        task = BackgroundTask(guarded, self)
        self.task = task
        task.result.connect(completed)
        task.failed.connect(self.message.setText)

        def finished():
            self.task = None
            self.body.setEnabled(True)
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def reject(self):
        if self.task is None:
            super().reject()

    def closeEvent(self, event):
        if self.task is not None:
            event.ignore()
        else:
            super().closeEvent(event)
