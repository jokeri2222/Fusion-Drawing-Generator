"""About boxes: the program (with a note on supporting it) and the settings."""
import os
import sys
import webbrowser

import tolerance as tolerance_mod
from PyQt5 import QtCore, QtGui, QtWidgets

from . import APP_NAME, APP_VERSION, icon, theme
from .frame import FramedDialog
from . import help_dialog


def _heading(text):
    label = QtWidgets.QLabel(text)
    label.setObjectName('cardTitle')
    return label


def _text(html):
    label = QtWidgets.QLabel(html)
    label.setWordWrap(True)
    label.setTextFormat(QtCore.Qt.RichText)
    label.setOpenExternalLinks(True)
    return label


def _divider():
    line = QtWidgets.QFrame()
    line.setObjectName('divider')
    line.setFixedHeight(1)
    return line


def program_folder():
    """The program's main folder."""
    import paths
    return paths.app_folder()


class AboutDialog(FramedDialog):
    def __init__(self, parent):
        super().__init__(parent, f'About {APP_NAME}', resizable=False)
        self.setFixedWidth(520)
        lay = QtWidgets.QVBoxLayout(self.body)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(16)
        logo = QtWidgets.QLabel()
        logo.setPixmap(QtGui.QPixmap.fromImage(icon.render(160)).scaled(
            72, 72, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        head.addWidget(logo)
        titles = QtWidgets.QVBoxLayout()
        name = QtWidgets.QLabel(APP_NAME)
        name.setObjectName('title')
        version = QtWidgets.QLabel(f'Version {APP_VERSION}')
        version.setProperty('muted', True)
        titles.addWidget(name)
        titles.addWidget(version)
        head.addLayout(titles, 1)
        lay.addLayout(head)
        lay.addWidget(_text(
            'Turns a Fusion 360 export into manufacturing drawings, tolerance figures with thermal expansion, and '
            'step-by-step assembly instructions.'))
        lay.addWidget(_heading('What it does'))
        lay.addWidget(_text(
            '&bull; Exact hidden-line views with ordinate and chain dimensions and diameter callouts<br>'
            '&bull; Machining and thermal-expansion tolerances from each part\'s material<br>'
            '&bull; A build order for the assembly you can edit, with an illustrated PDF guide<br>'
            '&bull; Every part rotated to its drawing orientation, in its own folder'))
        lay.addWidget(_heading('Built with'))
        built = _text('Python, PyQt5, CadQuery / OpenCascade, VTK, ReportLab')
        built.setProperty('muted', True)
        lay.addWidget(built)
        lay.addWidget(_divider())
        lay.addWidget(_heading('Support the project'))
        lay.addWidget(_text(
            'This program is free to use and is built and maintained in spare time. If it saves you time, a '
            'donation helps keep it improving. It is entirely optional - every feature works either way.'))
        row = QtWidgets.QHBoxLayout()
        help_btn = QtWidgets.QPushButton('Help and guide')
        help_btn.clicked.connect(lambda: help_dialog.HelpDialog(self).exec_())
        qr = QtWidgets.QPushButton('Show QR code')
        qr.clicked.connect(lambda: help_dialog.SupportDialog(self).exec_())
        donate = QtWidgets.QPushButton('Donate...')
        donate.setObjectName('primary')
        donate.setEnabled(bool(help_dialog.DONATE_URL))
        donate.clicked.connect(lambda: webbrowser.open(help_dialog.DONATE_URL))
        close = QtWidgets.QPushButton('Close')
        close.clicked.connect(self.accept)
        row.addWidget(help_btn)
        row.addStretch(1)
        row.addWidget(qr)
        row.addWidget(donate)
        row.addWidget(close)
        lay.addLayout(row)
        self.adjustSize()


class AboutSettingsDialog(FramedDialog):
    """Explains where each kind of setting is stored and opens those places."""

    PROJECT_FILES = (
        ('tolerance_config.json', 'Tolerance preset, temperatures and per-body temperatures'),
        ('drawing_settings.json', 'Drawing thresholds (which extra dimensions and callouts appear)'),
        ('body_inclusion.json', 'Which bodies get a drawing, are assembly-only or ignored'),
        ('assembly_order.json', 'Your manual assembly order'),
        ('assembly_tree.json', 'Assembly groups and sets'),
    )

    def __init__(self, parent, project_dir=None):
        super().__init__(parent, 'About settings', resizable=False)
        self.setFixedWidth(600)
        self.project_dir = project_dir
        lay = QtWidgets.QVBoxLayout(self.body)
        lay.setContentsMargins(24, 18, 24, 16)
        lay.setSpacing(10)
        lay.addWidget(_text(
            'Settings live in two places: <b>application settings</b> follow you (your Windows user), and '
            '<b>project settings</b> travel with each project folder.'))
        lay.addWidget(_heading('Application settings'))
        lay.addWidget(_text(
            'Theme and custom colours, fonts, units, window size, recent projects and the behaviour options. '
            'Stored in the Windows registry under <code>HKEY_CURRENT_USER\\Software\\FusionDrawingGenerator'
            '\\DrawingGenerator</code>. Change them under <i>Settings &gt; Preferences</i> and the '
            '<i>Themes</i> menu.'))
        lay.addWidget(_divider())
        lay.addWidget(_heading('Project settings'))
        configs = None
        if project_dir:
            configs = os.path.dirname(tolerance_mod.config_file(project_dir, 'tolerance_config.json', for_write=True))
            lay.addWidget(_text(f'Folder: <code>{configs}</code>'))
        else:
            lay.addWidget(_text('Pick a project folder to see where its settings are stored.'))
        for name, what in self.PROJECT_FILES:
            present = False
            if project_dir:
                try:
                    present = os.path.exists(tolerance_mod.config_file(project_dir, name))
                except Exception:
                    present = False
            mark = '\u2713' if present else '\u2013'
            lay.addWidget(_text(f'{mark}&nbsp; <code>{name}</code> - {what}'))
        note = _text('A dash means the file has not been created yet; defaults are used until you save. '
                     'Copy the folder to share or back up a project\'s settings.')
        note.setProperty('muted', True)
        lay.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        open_cfg = QtWidgets.QPushButton('Open project settings folder')
        open_cfg.setEnabled(bool(configs))
        open_cfg.clicked.connect(lambda: self._open(configs))
        open_prog = QtWidgets.QPushButton('Open program folder')
        open_prog.setToolTip('Where the program lives and where gui_error.log is written')
        open_prog.clicked.connect(lambda: self._open(program_folder()))
        close = QtWidgets.QPushButton('Close')
        close.setObjectName('primary')
        close.clicked.connect(self.accept)
        row.addWidget(open_cfg)
        row.addWidget(open_prog)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.adjustSize()

    @staticmethod
    def _open(path):
        if path and os.path.isdir(path):
            os.startfile(path)
