"""Message boxes, text prompt and progress dialog in the app's own window
style (drop-in replacements for the QMessageBox / QInputDialog / QProgressDialog
calls)."""
from PyQt5 import QtCore, QtWidgets

from . import theme
from .frame import FramedDialog

Yes, No, Ok, Cancel = (QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No,
                       QtWidgets.QMessageBox.Ok, QtWidgets.QMessageBox.Cancel)
_LABELS = {Yes: 'Yes', No: 'No', Ok: 'OK', Cancel: 'Cancel'}
_GLYPH = {'info': ('i', 'accent'), 'warning': ('!', 'warn'), 'error': ('×', 'danger'),
          'question': ('?', 'accent')}


class MessageDialog(FramedDialog):
    def __init__(self, parent, title, text, kind='info', buttons=(Ok,), default=None):
        super().__init__(parent, title, resizable=False)
        self.clicked = buttons[-1] if Cancel not in buttons else Cancel
        self.setMinimumWidth(380)
        self.setMaximumWidth(620)
        lay = QtWidgets.QVBoxLayout(self.body)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(16)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(14)
        glyph, color_key = _GLYPH[kind]
        badge = QtWidgets.QLabel(glyph)
        badge.setFixedSize(38, 38)
        badge.setAlignment(QtCore.Qt.AlignCenter)
        badge.setStyleSheet(f"background: {theme.colors()[color_key]}; color: {theme.colors()['accent_text']};"
                            f"border-radius: 19px; font-size: 17pt; font-weight: 700;")
        row.addWidget(badge, 0, QtCore.Qt.AlignTop)
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        row.addWidget(label, 1)
        lay.addLayout(row)
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        default = default if default in buttons else buttons[0]
        for b in buttons:
            btn = QtWidgets.QPushButton(_LABELS[b])
            if b == default:
                btn.setObjectName('primary')
                btn.setDefault(True)
            btn.clicked.connect(lambda _=False, v=b: self._done(v))
            foot.addWidget(btn)
        lay.addLayout(foot)
        self.adjustSize()

    def _done(self, value):
        self.clicked = value
        self.accept()


def _show(parent, title, text, kind, buttons=(Ok,), default=None):
    dialog = MessageDialog(parent, title, text, kind, buttons, default)
    dialog.exec_()
    return dialog.clicked


def information(parent, title, text):
    return _show(parent, title, text, 'info')


def warning(parent, title, text):
    return _show(parent, title, text, 'warning')


def critical(parent, title, text):
    return _show(parent, title, text, 'error')


def question(parent, title, text):
    return _show(parent, title, text, 'question', (Yes, No), No)


def get_text(parent, title, label, text=''):
    dialog = FramedDialog(parent, title, resizable=False)
    dialog.setMinimumWidth(380)
    lay = QtWidgets.QVBoxLayout(dialog.body)
    lay.setContentsMargins(18, 16, 18, 14)
    lay.setSpacing(10)
    lay.addWidget(QtWidgets.QLabel(label))
    edit = QtWidgets.QLineEdit(text)
    lay.addWidget(edit)
    foot = QtWidgets.QHBoxLayout()
    foot.addStretch(1)
    cancel = QtWidgets.QPushButton('Cancel')
    cancel.clicked.connect(dialog.reject)
    ok = QtWidgets.QPushButton('OK')
    ok.setObjectName('primary')
    ok.setDefault(True)
    ok.clicked.connect(dialog.accept)
    foot.addWidget(cancel)
    foot.addWidget(ok)
    lay.addLayout(foot)
    edit.setFocus()
    edit.selectAll()
    accepted = dialog.exec_() == QtWidgets.QDialog.Accepted
    return edit.text(), accepted


class ProgressDialog(FramedDialog):
    """Modal 'loading' box with a determinate bar; Esc and the title bar cannot
    dismiss it."""

    def __init__(self, parent, label, maximum):
        super().__init__(parent, 'Please wait', resizable=False, buttons=())
        self.setMinimumWidth(420)
        lay = QtWidgets.QVBoxLayout(self.body)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        self.label = QtWidgets.QLabel(label)
        lay.addWidget(self.label)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, max(1, maximum))
        lay.addWidget(self.bar)
        self.setWindowModality(QtCore.Qt.WindowModal)
        self.show()
        QtWidgets.QApplication.processEvents()

    def setValue(self, value):
        self.bar.setValue(value)

    def setLabelText(self, text):
        self.label.setText(text)

    def finish(self):
        QtWidgets.QDialog.done(self, 0)

    def reject(self):
        pass
