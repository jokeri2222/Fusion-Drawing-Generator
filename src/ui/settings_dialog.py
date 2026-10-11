"""Application settings: units, behaviour and housekeeping. Stored per user
(QSettings), not per project."""
import units
from PyQt5 import QtCore, QtWidgets

from . import messages
from .frame import FramedDialog

from .widgets import ThemedCombo

ORG, APP = 'FusionDrawingGenerator', 'DrawingGenerator'


def qsettings():
    return QtCore.QSettings(ORG, APP)


def load_units():
    s = qsettings()
    units.set_units(length=s.value('unit_length', 'millimetre', str),
                    angle=s.value('unit_angle', 'degree', str),
                    temperature=s.value('unit_temperature', 'Celsius', str))


def flag(name, default=True):
    return qsettings().value(name, default, bool)


def _unit_combo(table, current, symbol_of):
    combo = ThemedCombo()
    for name in table:
        combo.addItem(f'{name.capitalize()}  ({symbol_of(name)})', name)
    combo.setCurrentIndex(max(0, combo.findData(current)))
    return combo


class SettingsDialog(FramedDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.resize(560, 520)
        s = qsettings()
        self.changed_units = False
        layout = QtWidgets.QVBoxLayout(self.body)
        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)

        # ---- units
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setVerticalSpacing(14)
        intro = QtWidgets.QLabel(
            'Choose how values are shown and entered. Everything is calculated internally in millimetres and '
            'degrees Celsius, so changing a unit never changes the geometry - only how it is written.')
        intro.setWordWrap(True)
        intro.setProperty('muted', True)
        form.addRow(intro)
        self.length = _unit_combo(units.LENGTH, units.current('length'), units.length_symbol)
        self.angle = _unit_combo(units.ANGLE, units.current('angle'), units.angle_symbol)
        self.temp = _unit_combo(units.TEMPERATURE, units.current('temperature'), units.temperature_symbol)
        form.addRow('Length', self.length)
        form.addRow(self._note('Tolerance fields, drawing-setting thresholds and every dimension written on the '
                               'drawings (spans, diameters, lengths and their ± figures, and the UNITS field '
                               'of the title block).'))
        form.addRow('Angle', self.angle)
        form.addRow(self._note('Used wherever an angle is shown. The drawings currently carry no angle '
                               'dimensions, so this has no effect on them yet.'))
        form.addRow('Temperature', self.temp)
        form.addRow(self._note('Manufacturing and operating temperatures, including the per-body table.'))
        self.preview = QtWidgets.QLabel()
        self.preview.setObjectName('cardTitle')
        form.addRow('Example', self.preview)
        for c in (self.length, self.angle, self.temp):
            c.currentIndexChanged.connect(self._example)
        self._example()
        tabs.addTab(page, 'Units')

        # ---- behaviour
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setSpacing(10)
        self.flags = {}
        for key, text, default in (
                ('open_when_done', 'Open the output folder when generation finishes', False),
                ('flash_when_done', 'Flash the taskbar button when a long job finishes', True),
                ('remember_project', 'Reopen the last project on start', True),
                ('confirm_close', 'Ask before closing while a job is running', True)):
            box = QtWidgets.QCheckBox(text)
            box.setChecked(s.value(key, default, bool))
            self.flags[key] = box
            v.addWidget(box)
        v.addStretch(1)
        tabs.addTab(page, 'Behaviour')

        # ---- data
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setSpacing(10)
        note = QtWidgets.QLabel(
            'Window size, theme, units and the recent-project list are stored for your Windows user. '
            'Per-project settings (tolerances, inclusion, order, drawing thresholds) live in the project\'s own '
            'configs folder and are not touched here.')
        note.setWordWrap(True)
        note.setProperty('muted', True)
        v.addWidget(note)
        clear = QtWidgets.QPushButton('Clear recent projects')
        clear.clicked.connect(self._clear_recent)
        reset = QtWidgets.QPushButton('Reset all application settings')
        reset.setObjectName('danger')
        reset.clicked.connect(self._reset_all)
        v.addWidget(clear, 0, QtCore.Qt.AlignLeft)
        v.addWidget(reset, 0, QtCore.Qt.AlignLeft)
        v.addStretch(1)
        tabs.addTab(page, 'Data')

        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        cancel = QtWidgets.QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton('Save')
        save.setObjectName('primary')
        save.clicked.connect(self._save)
        foot.addWidget(cancel)
        foot.addWidget(save)
        layout.addLayout(foot)

    @staticmethod
    def _note(text):
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setProperty('muted', True)
        return label

    def _example(self):
        ln, an, tp = self.length.currentData(), self.angle.currentData(), self.temp.currentData()
        self.preview.setText(
            f'25.4 mm = {units.format_length(25.4, ln, True)}    '
            f'90° = {units.angle_from_deg(90, an):.{units.angle_decimals(an)}f} {units.angle_symbol(an)}    '
            f'20 °C = {units.temp_from_c(20, tp):.{units.temperature_decimals(tp)}f} '
            f'{units.temperature_symbol(tp)}')

    def _clear_recent(self):
        qsettings().remove('recent')
        messages.information(self, 'Settings', 'The recent project list was cleared.')

    def _reset_all(self):
        if messages.question(
                self, 'Reset', 'Reset theme, fonts, units, window size and all options to their defaults?') \
                != QtWidgets.QMessageBox.Yes:
            return
        qsettings().clear()
        messages.information(self, 'Settings', 'Done. Restart the program to apply everything.')
        self.reject()

    def _save(self):
        s = qsettings()
        before = (units.current('length'), units.current('angle'), units.current('temperature'))
        s.setValue('unit_length', self.length.currentData())
        s.setValue('unit_angle', self.angle.currentData())
        s.setValue('unit_temperature', self.temp.currentData())
        for key, box in self.flags.items():
            s.setValue(key, box.isChecked())
        self.old_units = before
        load_units()
        self.changed_units = before != (units.current('length'), units.current('angle'),
                                        units.current('temperature'))
        self.accept()
