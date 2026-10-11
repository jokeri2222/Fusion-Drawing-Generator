"""Custom window chrome: a themed title bar and frameless dialogs that behave
like normal windows (drag by the title, double-click to maximise, resize from
the edges, Windows snap) through WM_NCHITTEST."""
import sys

from PyQt5 import QtCore, QtGui, QtWidgets

from . import icon

GLYPHS = {'min': '', 'max': '', 'restore': '', 'close': ''}


class TitleBar(QtWidgets.QWidget):
    """Icon, title, optional menu strip and window buttons."""

    def __init__(self, window, title='', menus=None, buttons=('min', 'max', 'close'), height=40):
        super().__init__()
        self.setObjectName('titleBar')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setFixedHeight(height)
        self._window = window
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(10)
        logo = QtWidgets.QLabel()
        logo.setPixmap(QtGui.QPixmap.fromImage(icon.render(64)).scaled(
            18, 18, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        lay.addWidget(logo)
        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName('titleName')
        lay.addWidget(self.title)
        if menus is not None:
            lay.addSpacing(8)
            lay.addWidget(menus)
        lay.addStretch(1)
        handlers = {'min': window.showMinimized, 'max': self._toggle_max, 'close': window.close}
        self.buttons = {}
        for key in buttons:
            b = QtWidgets.QToolButton()
            b.setObjectName('winClose' if key == 'close' else 'winButton')
            b.setText(GLYPHS[key])
            b.setFixedSize(46, height)
            b.clicked.connect(handlers[key])
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            lay.addWidget(b)
            self.buttons[key] = b
        if not buttons:
            lay.addSpacing(12)

    def set_title(self, text):
        self.title.setText(text)

    def _toggle_max(self):
        w = self._window
        w.showNormal() if w.isMaximized() else w.showMaximized()

    def sync_state(self):
        if 'max' in self.buttons:
            self.buttons['max'].setText(GLYPHS['restore' if self._window.isMaximized() else 'max'])


def hit_test(window, titlebar, message, resizable=True, edge=6):
    """Answer WM_NCHITTEST for a frameless window. Returns (True, code) when the
    point is on a resize edge or the draggable part of the title bar."""
    import ctypes
    from ctypes import wintypes
    msg = wintypes.MSG.from_address(int(message))
    if msg.message != 0x0084:
        return None
    dpr = window.devicePixelRatioF()
    x = ctypes.c_short(msg.lParam & 0xFFFF).value / dpr
    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value / dpr
    gpos = QtCore.QPoint(int(x), int(y))
    pos = window.mapFromGlobal(gpos)
    w, h = window.width(), window.height()
    if resizable and not window.isMaximized():
        key = (int(pos.x() < edge), int(pos.x() >= w - edge), int(pos.y() < edge), int(pos.y() >= h - edge))
        code = {(1, 0, 1, 0): 13, (0, 1, 1, 0): 14, (1, 0, 0, 1): 16, (0, 1, 0, 1): 17,
                (1, 0, 0, 0): 10, (0, 1, 0, 0): 11, (0, 0, 1, 0): 12, (0, 0, 0, 1): 15}.get(key)
        if code:
            return True, code
    local = titlebar.mapFromGlobal(gpos)
    if titlebar.rect().contains(local):
        child = titlebar.childAt(local)
        if child is None or isinstance(child, QtWidgets.QLabel):
            return True, 2   # HTCAPTION: drag, double-click maximise, snap
    return None


class FramedDialog(QtWidgets.QDialog):
    """A dialog with the app's own title bar. Subclasses lay their content out
    in `self.body` instead of in the dialog itself."""

    def __init__(self, parent=None, title='', resizable=True, buttons=None):
        super().__init__(parent)
        self._resizable = resizable
        self.setWindowFlags(QtCore.Qt.Dialog | QtCore.Qt.FramelessWindowHint)
        self.setObjectName('framedDialog')
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        if buttons is None:
            buttons = ('max', 'close') if resizable else ('close',)
        self.titlebar = TitleBar(self, title, buttons=buttons, height=36)
        self.body = QtWidgets.QWidget()
        outer.addWidget(self.titlebar)
        outer.addWidget(self.body, 1)
        if title:
            super().setWindowTitle(title)

    def setWindowTitle(self, text):
        super().setWindowTitle(text)
        if hasattr(self, 'titlebar'):
            self.titlebar.set_title(text)

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.WindowStateChange and hasattr(self, 'titlebar'):
            self.titlebar.sync_state()
        super().changeEvent(event)

    def nativeEvent(self, event_type, message):
        if sys.platform == 'win32' and event_type == b'windows_generic_MSG':
            result = hit_test(self, self.titlebar, message, self._resizable)
            if result:
                return result
        return super().nativeEvent(event_type, message)
