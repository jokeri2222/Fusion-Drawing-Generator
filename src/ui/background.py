"""Run a function on a worker thread and get its result back on the UI thread,
so slow geometry work never blocks a window."""
import threading

from PyQt5 import QtCore


class Background(QtCore.QObject):
    _result = QtCore.pyqtSignal(object, object, object)   # callback, value, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result.connect(self._deliver)

    @staticmethod
    def _deliver(callback, value, error):
        callback(value, error)

    def run(self, fn, callback):
        """Call `fn()` off the UI thread, then `callback(value, error)` on it."""
        def work():
            try:
                value, error = fn(), None
            except Exception as exc:
                value, error = None, exc
            try:
                self._result.emit(callback, value, error)
            except RuntimeError:   # window already gone
                pass
        threading.Thread(target=work, daemon=True).start()
