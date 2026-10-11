"""Theme editor: pick a preset or build your own - colours for every kind of
element, text colour per element type, fonts, sizes and corner radius. Changes
preview live on the whole application; Cancel puts the previous theme back."""
import json

from PyQt5 import QtCore, QtGui, QtWidgets

from . import messages
from .frame import FramedDialog

from . import theme
from .widgets import ThemedCombo


class ColorRow(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()

    def __init__(self, key, label):
        super().__init__()
        self.key = key
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QtWidgets.QLabel(label)
        self.label.setMinimumWidth(190)
        self.swatch = QtWidgets.QPushButton()
        self.swatch.setFixedSize(34, 24)
        self.swatch.setCursor(QtCore.Qt.PointingHandCursor)
        self.swatch.clicked.connect(self._pick)
        self.edit = QtWidgets.QLineEdit()
        self.edit.setMaximumWidth(92)
        self.edit.setPlaceholderText('#rrggbb')
        self.edit.editingFinished.connect(self._typed)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.swatch)
        lay.addWidget(self.edit)
        self._value = '#000000'

    def value(self):
        return self._value

    def set_value(self, hex_color, emit=False):
        color = QtGui.QColor(hex_color)
        if not color.isValid():
            return
        self._value = color.name()
        self.edit.setText(self._value)
        self.swatch.setStyleSheet(
            f'background: {self._value}; border: 1px solid #888; border-radius: 5px;')
        if emit:
            self.changed.emit()

    def _typed(self):
        text = self.edit.text().strip()
        if text and not text.startswith('#'):
            text = '#' + text
        if QtGui.QColor(text).isValid():
            self.set_value(text, emit=True)
        else:
            self.edit.setText(self._value)

    def _pick(self):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self._value), self, self.label.text())
        if color.isValid():
            self.set_value(color.name(), emit=True)


class ThemeDialog(FramedDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('Theme Editor')
        self.resize(780, 700)
        self._app = QtWidgets.QApplication.instance()
        self._start = (theme.mode(), theme.load_custom(), theme.load_fonts())
        self.result_mode = theme.mode()
        self._loading = False

        layout = QtWidgets.QVBoxLayout(self.body)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel('Start from'))
        self.preset = ThemedCombo()
        for mode in theme.all_modes():
            if mode != 'custom':
                self.preset.addItem(theme.mode_label(mode), mode)
        self.preset.currentIndexChanged.connect(self._load_preset)
        top.addWidget(self.preset)
        top.addSpacing(12)
        top.addWidget(QtWidgets.QLabel('Based on'))
        self.base = ThemedCombo()
        self.base.addItem('Dark', 'dark')
        self.base.addItem('Light', 'light')
        self.base.currentIndexChanged.connect(self._preview)
        top.addWidget(self.base)
        top.addStretch(1)
        imp = QtWidgets.QPushButton('Import...')
        imp.clicked.connect(self._import)
        exp = QtWidgets.QPushButton('Export...')
        exp.clicked.connect(self._export)
        top.addWidget(imp)
        top.addWidget(exp)
        layout.addLayout(top)
        note = QtWidgets.QLabel('Pick a preset to start, then change anything. Everything you change here is '
                                'saved as your Custom theme, and shows live while you edit.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        layout.addWidget(note)

        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)
        self.rows = {}
        for group, items in theme.EDITABLE:
            page = QtWidgets.QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QtWidgets.QFrame.NoFrame)
            body = QtWidgets.QWidget()
            form = QtWidgets.QVBoxLayout(body)
            form.setSpacing(8)
            for key, label in items:
                row = ColorRow(key, label)
                row.changed.connect(self._preview)
                self.rows[key] = row
                form.addWidget(row)
            form.addStretch(1)
            page.setWidget(body)
            tabs.addTab(page, group)

        fonts_page = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(fonts_page)
        grid.setColumnStretch(2, 1)
        grid.setVerticalSpacing(10)
        self.font_family = QtWidgets.QFontComboBox()
        self.font_family.setEditable(False)
        self.font_mono = QtWidgets.QFontComboBox()
        self.font_mono.setFontFilters(QtWidgets.QFontComboBox.MonospacedFonts)
        self.sizes = {}
        rows = (('Interface font', self.font_family), ('Log / monospace font', self.font_mono))
        r = 0
        for label, widget in rows:
            grid.addWidget(QtWidgets.QLabel(label), r, 0)
            grid.addWidget(widget, r, 1, 1, 2)
            r += 1
        for key, label, lo, hi in (('size', 'Text size (pt)', 7, 16), ('title_size', 'Title size (pt)', 10, 32),
                                   ('heading_size', 'Section heading size (pt)', 8, 20),
                                   ('log_size', 'Log text size (pt)', 7, 16), ('radius', 'Corner radius (px)', 0, 18)):
            spin = QtWidgets.QSpinBox()
            spin.setRange(lo, hi)
            spin.setMaximumWidth(90)
            self.sizes[key] = spin
            grid.addWidget(QtWidgets.QLabel(label), r, 0)
            grid.addWidget(spin, r, 1)
            r += 1
        grid.setRowStretch(r, 1)
        tabs.addTab(fonts_page, 'Fonts and shape')
        self.font_family.currentFontChanged.connect(self._preview)
        self.font_mono.currentFontChanged.connect(self._preview)
        for spin in self.sizes.values():
            spin.valueChanged.connect(self._preview)

        foot = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton('Reset fonts and shape')
        reset.clicked.connect(self._reset_fonts)
        foot.addWidget(reset)
        foot.addStretch(1)
        cancel = QtWidgets.QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton('Save theme')
        save.setObjectName('primary')
        save.clicked.connect(self._save)
        foot.addWidget(cancel)
        foot.addWidget(save)
        layout.addLayout(foot)

        self._populate_from_mode(theme.mode())

    # ---- loading ---------------------------------------------------------------
    def _populate_from_mode(self, mode):
        self._loading = True
        if mode == 'custom':
            custom = theme.load_custom()
            dark, colors = custom['base'] == 'dark', theme.resolve('custom', custom)[1]
        else:
            dark, colors = theme.resolve(mode)
        idx = self.preset.findData(mode)
        self.preset.setCurrentIndex(idx if idx >= 0 else 0)
        self.base.setCurrentIndex(self.base.findData('dark' if dark else 'light'))
        for key, row in self.rows.items():
            row.set_value(colors[key])
        fonts = theme.load_fonts()
        self.font_family.setCurrentFont(QtGui.QFont(fonts['family']))
        self.font_mono.setCurrentFont(QtGui.QFont(fonts['mono']))
        for key, spin in self.sizes.items():
            spin.setValue(int(fonts[key]))
        self._loading = False

    def _load_preset(self):
        if self._loading:
            return
        mode = self.preset.currentData()
        dark, colors = theme.resolve(mode)
        self._loading = True
        self.base.setCurrentIndex(self.base.findData('dark' if dark else 'light'))
        for key, row in self.rows.items():
            row.set_value(colors[key])
        self._loading = False
        self._preview()

    def _reset_fonts(self):
        d = theme.DEFAULT_FONTS
        self._loading = True
        self.font_family.setCurrentFont(QtGui.QFont(d['family']))
        self.font_mono.setCurrentFont(QtGui.QFont(d['mono']))
        for key, spin in self.sizes.items():
            spin.setValue(int(d[key]))
        self._loading = False
        self._preview()

    # ---- state -----------------------------------------------------------------
    def _state(self):
        fonts = {'family': self.font_family.currentFont().family(), 'mono': self.font_mono.currentFont().family()}
        fonts.update({k: s.value() for k, s in self.sizes.items()})
        return {'base': self.base.currentData(), 'colors': {k: r.value() for k, r in self.rows.items()},
                'fonts': fonts}

    def _preview(self, *_):
        if self._loading:
            return
        theme.apply(self._app, 'custom', preview=self._state())

    def _save(self):
        state = self._state()
        theme.save_custom({'base': state['base'], 'colors': state['colors']})
        theme.save_fonts(state['fonts'])
        self.result_mode = 'custom'
        self.accept()

    def reject(self):
        theme.apply(self._app, self._start[0])
        super().reject()

    # ---- import / export -----------------------------------------------------------
    def _export(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Export theme', 'theme.json', 'Theme (*.json)')
        if path:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._state(), f, indent=2)

    def _import(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Import theme', '', 'Theme (*.json)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self._loading = True
            self.base.setCurrentIndex(max(0, self.base.findData(data.get('base', 'dark'))))
            for key, value in data.get('colors', {}).items():
                if key in self.rows:
                    self.rows[key].set_value(value)
            fonts = data.get('fonts', {})
            if 'family' in fonts:
                self.font_family.setCurrentFont(QtGui.QFont(fonts['family']))
            if 'mono' in fonts:
                self.font_mono.setCurrentFont(QtGui.QFont(fonts['mono']))
            for key, spin in self.sizes.items():
                if key in fonts:
                    spin.setValue(int(fonts[key]))
            self._loading = False
            self._preview()
        except (OSError, ValueError, AttributeError) as exc:
            self._loading = False
            messages.warning(self, 'Could not import', f'That file is not a valid theme: {exc}')
