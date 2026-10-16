"""Main window of the Fusion Drawing Generator (PyQt5)."""
import glob
import os
import sys
import time

from PyQt5 import QtCore, QtGui, QtWidgets

import tolerance as tolerance_mod
import units

from . import icon, theme
from . import messages
from .about_dialogs import AboutDialog, AboutSettingsDialog, program_folder
from .frame import TitleBar, hit_test
from .runner import Job
from .widgets import ThemedCombo

APP_NAME = 'Fusion Drawing Generator'
MAX_RECENT = 8


class _MenuStrip(QtWidgets.QWidget):
    """Flat row of menu buttons that lives in the title bar."""

    def __init__(self, window):
        super().__init__()
        self._window = window
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._lay = lay

    @staticmethod
    def _style(menu):
        menu.setWindowFlags(menu.windowFlags() | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint)
        menu.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        return menu

    def submenu(self, parent_menu, title):
        menu = self._style(QtWidgets.QMenu(title, parent_menu))
        parent_menu.addMenu(menu)
        return menu

    def addMenu(self, title):
        menu = QtWidgets.QMenu(title, self._window)
        menu.setWindowFlags(menu.windowFlags() | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint)
        menu.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        button = QtWidgets.QToolButton()
        button.setObjectName('menuButton')
        button.setText(title)
        button.setMenu(menu)
        button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        button.setCursor(QtCore.Qt.PointingHandCursor)
        self._lay.addWidget(button)
        return menu


def card(title=None):
    """A rounded surface with an optional title; returns (frame, layout)."""
    frame = QtWidgets.QFrame()
    frame.setObjectName('card')
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    if title:
        label = QtWidgets.QLabel(title)
        label.setObjectName('cardTitle')
        layout.addWidget(label)
    return frame, layout


def muted(text, wrap=True):
    label = QtWidgets.QLabel(text)
    label.setWordWrap(wrap)
    label.setProperty('muted', True)
    return label


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QtCore.QSettings('FusionDrawingGenerator', 'DrawingGenerator')
        self.body_overrides = {}
        self.worker = None
        self.scan_job = None
        self._scan_timer = QtCore.QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(350)
        self._scan_timer.timeout.connect(self._scan_project)
        self._started = 0.0
        self._last_output = None
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icon.app_icon())
        self.setAcceptDrops(True)
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.FramelessWindowHint)
        self.resize(self.settings.value('size', QtCore.QSize(920, 980), QtCore.QSize))
        self.setMinimumSize(780, 560)

        self._build_menu()
        self._build_ui()
        self._tick = QtCore.QTimer(self)
        self._tick.timeout.connect(self._update_elapsed)
        # follow the OS light/dark setting while "System" is selected
        self._theme_watch = QtCore.QTimer(self)
        self._theme_watch.timeout.connect(self._check_system_theme)
        self._theme_watch.start(2000)

        last = self.settings.value('last_project', '', str)
        if last and os.path.isdir(last) and self.settings.value('remember_project', True, bool):
            self.project_edit.setText(last)

    # ---- construction --------------------------------------------------------
    def _build_menu(self):
        bar = _MenuStrip(self)
        self.menu_strip = bar
        file_menu = bar.addMenu('File')
        act = file_menu.addAction('Open project folder...', self._pick_project_dir)
        act.setShortcut('Ctrl+O')
        self.recent_menu = bar.submenu(file_menu, 'Recent projects')
        self._refresh_recent()
        file_menu.addAction('Open output folder', self._open_output_folder)
        file_menu.addSeparator()
        file_menu.addAction('Quit', self.close).setShortcut('Ctrl+Q')

        project = bar.addMenu('Project')
        project.addAction('Per-body temperatures...', self._edit_body_overrides)
        project.addAction('Body inclusion...', self._edit_body_inclusion)
        project.addAction('Assembly order...', self._edit_assembly_order)
        project.addAction('Drawing settings...', self._edit_drawing_settings)
        project.addSeparator()
        project.addAction('Save tolerance settings', self._save_config).setShortcut('Ctrl+S')
        project.addAction('Generate drawings', self._generate).setShortcut('Ctrl+G')

        themes = bar.addMenu('Themes')
        self.theme_group = QtWidgets.QActionGroup(self)
        self.theme_actions = {}

        def add_theme(menu, mode):
            act = menu.addAction(theme.mode_label(mode))
            act.setCheckable(True)
            act.setActionGroup(self.theme_group)
            act.triggered.connect(lambda _=False, m=mode: self._set_theme(m))
            self.theme_actions[mode] = act

        for mode in theme.MODES:
            add_theme(themes, mode)
        themes.addSeparator()
        dark_menu = bar.submenu(themes, 'Dark presets')
        light_menu = bar.submenu(themes, 'Light presets')
        for name, (base, _c) in theme.PRESETS.items():
            add_theme(dark_menu if base == 'dark' else light_menu, 'preset:' + name)
        themes.addSeparator()
        add_theme(themes, 'custom')
        themes.addAction('Customize theme...', self._edit_theme)
        if theme.mode() in self.theme_actions:
            self.theme_actions[theme.mode()].setChecked(True)

        settings_menu = bar.addMenu('Settings')
        settings_menu.addAction('Preferences...', self._open_settings).setShortcut('Ctrl+,')
        units_menu = bar.submenu(settings_menu, 'Units')
        self.unit_actions = {}
        for kind, title, table, symbol_of in (
                ('length', 'Length', units.LENGTH, units.length_symbol),
                ('angle', 'Angle', units.ANGLE, units.angle_symbol),
                ('temperature', 'Temperature', units.TEMPERATURE, units.temperature_symbol)):
            sub = bar.submenu(units_menu, title)
            group = QtWidgets.QActionGroup(self)
            for name in table:
                act = sub.addAction(f'{name.capitalize()}  ({symbol_of(name)})')
                act.setCheckable(True)
                act.setActionGroup(group)
                act.setChecked(name == units.current(kind))
                act.triggered.connect(lambda _=False, k=kind, n=name: self._quick_unit(k, n))
                self.unit_actions[(kind, name)] = act
        settings_menu.addSeparator()
        settings_menu.addAction('Open project settings folder', self._open_config_folder)
        settings_menu.addAction('Open program folder', lambda: os.startfile(program_folder()))

        fusion = bar.addMenu('Fusion Script')
        fusion.addAction('Show export script folder', self._show_export_script)
        fusion.addAction('Copy script path', self._copy_script_path)
        fusion.addAction("Open Fusion's scripts folder", self._open_fusion_scripts)
        fusion.addSeparator()
        fusion.addAction('How to install and run it...', lambda: self._show_help('Fusion export script'))

        helpm = bar.addMenu('Help')
        helpm.addAction('Help and guide', self._show_help).setShortcut('F1')
        helpm.addAction('Keyboard shortcuts', lambda: self._show_help('Keyboard shortcuts'))
        helpm.addAction('Troubleshooting', lambda: self._show_help('Troubleshooting'))
        about = bar.addMenu('About')
        about.addAction(f'About {APP_NAME}', self._about)
        about.addAction('About settings...', self._about_settings)
        about.addSeparator()
        about.addAction('Support the project...', self._support)
        for m in (file_menu, project, themes, settings_menu, fusion, helpm, about):
            for a in m.actions():
                if not a.shortcut().isEmpty():
                    self.addAction(a)   # keep shortcuts working with the menu closed

    def _build_ui(self):
        shell = QtWidgets.QWidget()
        shell.setObjectName('shell')
        shell.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        shell_lay = QtWidgets.QVBoxLayout(shell)
        shell_lay.setContentsMargins(1, 1, 1, 1)
        shell_lay.setSpacing(0)
        self.titlebar = TitleBar(self, APP_NAME, menus=self.menu_strip)
        shell_lay.addWidget(self.titlebar)
        root = QtWidgets.QWidget()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(root)
        shell_lay.addWidget(scroll, 1)
        self.setCentralWidget(shell)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(20, 14, 20, 12)
        outer.setSpacing(12)

        # header
        header = QtWidgets.QHBoxLayout()
        logo = QtWidgets.QLabel()
        logo.setPixmap(QtGui.QPixmap.fromImage(icon.render(96)).scaled(
            44, 44, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        header.addWidget(logo)
        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(0)
        title = QtWidgets.QLabel(APP_NAME)
        title.setObjectName('title')
        sub = QtWidgets.QLabel('Technical drawings, tolerances and assembly instructions from a Fusion 360 export')
        sub.setObjectName('subtitle')
        titles.addWidget(title)
        titles.addWidget(sub)
        header.addLayout(titles, 1)
        header.addWidget(QtWidgets.QLabel('Theme'))
        self.theme_combo = ThemedCombo()
        for mode in theme.all_modes():
            self.theme_combo.addItem(theme.mode_label(mode), mode)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(theme.mode())))
        self.theme_combo.currentIndexChanged.connect(lambda _i: self._set_theme(self.theme_combo.currentData()))
        header.addWidget(self.theme_combo)
        outer.addLayout(header)

        # project
        frame, lay = card('Project')
        grid = QtWidgets.QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.addWidget(QtWidgets.QLabel('Project folder'), 0, 0)
        self.project_edit = QtWidgets.QLineEdit()
        self.project_edit.setPlaceholderText('The project folder - the one that contains the "exports" folder (or drop it here)')
        self.project_edit.textChanged.connect(self._schedule_scan)
        grid.addWidget(self.project_edit, 0, 1)
        b = QtWidgets.QPushButton('Browse...')
        b.clicked.connect(self._pick_project_dir)
        grid.addWidget(b, 0, 2)
        self.project_status = QtWidgets.QLabel('Pick a project folder to begin.')
        self.project_status.setProperty('muted', True)
        grid.addWidget(self.project_status, 1, 1)
        self.scan_bar = QtWidgets.QProgressBar()
        self.scan_bar.setObjectName('scanBar')
        self.scan_bar.setTextVisible(False)
        self.scan_bar.setFixedHeight(5)
        self.scan_bar.setRange(0, 1000)
        self.scan_bar.hide()
        grid.addWidget(self.scan_bar, 1, 2)
        grid.addWidget(QtWidgets.QLabel('Output folder'), 2, 0)
        self.output_edit = QtWidgets.QLineEdit()
        self.output_edit.setPlaceholderText('Leave empty to use <project>/drawings')
        grid.addWidget(self.output_edit, 2, 1)
        b = QtWidgets.QPushButton('Browse...')
        b.clicked.connect(self._pick_output_dir)
        grid.addWidget(b, 2, 2)
        grid.addWidget(muted('Every part gets its own folder with its drawing (PDF) and a STEP copy rotated so '
                             'its features line up with the axes.'), 3, 1)
        lay.addLayout(grid)
        outer.addWidget(frame)

        # tolerance
        frame, lay = card('Tolerance and thermal expansion')
        self._build_tolerance(lay)
        outer.addWidget(frame)

        # project settings
        frame, lay = card('Project settings')
        row = QtWidgets.QHBoxLayout()
        for text, cmd in (('Per-body temperatures...', self._edit_body_overrides),
                          ('Body inclusion...', self._edit_body_inclusion),
                          ('Assembly order...', self._edit_assembly_order),
                          ('Drawing settings...', self._edit_drawing_settings)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, c=cmd: c())
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.override_summary = muted('No per-body temperatures set.')
        lay.addWidget(self.override_summary)
        outer.addWidget(frame)

        # actions + progress
        frame, lay = card()
        row = QtWidgets.QHBoxLayout()
        self.generate_btn = QtWidgets.QPushButton('Generate drawings')
        self.generate_btn.setObjectName('primary')
        self.generate_btn.clicked.connect(self._generate)
        self.plain_btn = QtWidgets.QPushButton('Without tolerances')
        self.plain_btn.setToolTip('Generate the drawings with no ± figures, ignoring the saved tolerance settings.')
        self.plain_btn.clicked.connect(self._generate_no_tolerance)
        self.guide_btn = QtWidgets.QPushButton('Assembly instructions (PDF)...')
        self.guide_btn.clicked.connect(self._generate_assembly_instructions_pdf)
        self.cancel_btn = QtWidgets.QPushButton('Cancel')
        self.cancel_btn.setObjectName('danger')
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        for w in (self.generate_btn, self.plain_btn, self.guide_btn):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.stage_label = QtWidgets.QLabel('Ready.')
        self.elapsed_label = muted('', wrap=False)
        info = QtWidgets.QHBoxLayout()
        info.addWidget(self.stage_label, 1)
        info.addWidget(self.elapsed_label)
        lay.addLayout(info)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat('%p%')
        lay.addWidget(self.progress)
        outer.addWidget(frame)

        # log
        head = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel('Activity')
        label.setObjectName('cardTitle')
        head.addWidget(label)
        head.addStretch(1)
        self.open_btn = QtWidgets.QPushButton('Open output folder')
        self.open_btn.clicked.connect(self._open_output_folder)
        clear = QtWidgets.QPushButton('Clear')
        clear.clicked.connect(lambda: self.log.clear())
        head.addWidget(self.open_btn)
        head.addWidget(clear)
        outer.addLayout(head)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName('log')
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        self.log.setMaximumBlockCount(5000)
        outer.addWidget(self.log, 1)
        self.statusBar().showMessage('Ready')

    def _build_tolerance(self, lay):
        self.preset_group = QtWidgets.QButtonGroup(self)
        self._programmatic = False
        self.mfg_temp = self._temp_spin()
        self.op_min = self._temp_spin()
        self.op_max = self._temp_spin()
        self.custom_pct = self._spin(0.2, 0.0, 100.0, 3, ' %')
        self.min_tol = self._len_spin()
        self.max_tol = self._len_spin()
        self.preset_edits = {}
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        rows = (('Machining margin (% of the dimension)', 0, self.custom_pct),
                ('Smallest tolerance (parts that touch)', 1, self.min_tol),
                ('Largest tolerance (no close neighbour)', 2, self.max_tol))
        self._custom_widgets = [w for _l, _i, w in rows]
        for col, name in enumerate(tolerance_mod.PRESET_CHOICES, start=1):
            radio = QtWidgets.QRadioButton(name.capitalize())
            radio.setProperty('preset', name)
            self.preset_group.addButton(radio)
            radio.toggled.connect(self._refresh_tolerance_table)
            grid.addWidget(radio, 0, col, QtCore.Qt.AlignCenter)
        for r, (label, idx, widget) in enumerate(rows, start=1):
            grid.addWidget(QtWidgets.QLabel(label), r, 0)
            for col, name in enumerate(tolerance_mod.PRESET_CHOICES, start=1):
                if name == 'custom':
                    grid.addWidget(widget, r, col)
                else:
                    value = QtWidgets.QLineEdit()
                    value.setReadOnly(True)
                    value.setFocusPolicy(QtCore.Qt.NoFocus)
                    value.setAlignment(QtCore.Qt.AlignCenter)
                    value.setMaximumWidth(130)
                    self.preset_edits.setdefault(name, []).append((idx, value))
                    grid.addWidget(value, r, col)
        lay.addLayout(grid)
        line = QtWidgets.QFrame()
        line.setObjectName('divider')
        line.setFixedHeight(1)
        lay.addWidget(line)
        limits = QtWidgets.QHBoxLayout()
        self.kind_boxes = {}
        kinds = QtWidgets.QHBoxLayout()
        kinds.addWidget(QtWidgets.QLabel('Print a ± on:'))
        for key, title in tolerance_mod.KINDS.items():
            box = QtWidgets.QCheckBox(title)
            box.setChecked(tolerance_mod.DEFAULT_KINDS[key])
            self.kind_boxes[key] = box
            kinds.addWidget(box)
        kinds.addStretch(1)
        lay.addLayout(kinds)
        limits.addWidget(QtWidgets.QLabel('Leave the tolerance off when it is looser than'))
        self.max_standard = QtWidgets.QComboBox()
        for key, (title, _table) in tolerance_mod.STANDARDS.items():
            self.max_standard.addItem(title, key)
        self.max_standard.setToolTip('A feature whose calculated ± is bigger than this standard allows for its size '
                                     'gets no tolerance figure at all.')
        limits.addWidget(self.max_standard)
        limits.addStretch(1)
        lay.addLayout(limits)
        temps = QtWidgets.QHBoxLayout()
        for text, spin in (('Manufacturing temperature', self.mfg_temp), ('Operating minimum', self.op_min),
                           ('Operating maximum', self.op_max)):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(3)
            col.addWidget(QtWidgets.QLabel(text))
            col.addWidget(spin)
            temps.addLayout(col)
        temps.addStretch(1)
        save = QtWidgets.QPushButton('Save tolerance settings')
        save.clicked.connect(self._save_config)
        temps.addWidget(save, 0, QtCore.Qt.AlignBottom)
        lay.addLayout(temps)
        lay.addWidget(muted(
            'The ± figure on each dimension combines a machining margin (the preset\'s % of the dimension, kept '
            'between the smallest tolerance and the fit band: half the gap to the nearest part, or the largest '
            'tolerance when none is close) with thermal movement from the part\'s material. Equal temperatures give '
            'no thermal movement; the machining margin still applies. Drawings only carry tolerances once settings '
            'have been saved for the project. Fillets and chamfers carry no ± unless their box is ticked, and a ± '
            'larger than the chosen ISO 2768 class allows for that size is left off.'))
        self._apply_unit_labels()
        self._set_len(self.min_tol, 0.02)
        self._set_len(self.max_tol, 0.5)
        for spin in (self.mfg_temp, self.op_min, self.op_max):
            self._set_temp(spin, 20.0)
        self._select_preset(tolerance_mod.DEFAULT_PRESET)

    @staticmethod
    def _spin(value, lo, hi, decimals, suffix):
        s = QtWidgets.QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSuffix(suffix)
        s.setValue(value)
        s.setMaximumWidth(130)
        s.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        s.setAlignment(QtCore.Qt.AlignCenter)
        return s

    def _len_spin(self):
        s = self._spin(0.0, 0.0, 1000.0, 3, ' mm')
        s._base = 0.0
        s.valueChanged.connect(lambda v, sp=s: self._typed(sp, units.length_to_mm(v)))
        return s

    def _temp_spin(self):
        s = self._spin(20.0, -273.15, 5000.0, 1, ' \u00b0C')
        s.setMaximumWidth(150)
        s._base = 20.0
        s.valueChanged.connect(lambda v, sp=s: self._typed(sp, units.temp_to_c(v)))
        return s

    def _typed(self, spin, base_value):
        """Keep the exact internal value unless the person typed a new one, so
        switching units never nudges a stored number through rounding."""
        if not self._programmatic:
            spin._base = base_value

    def _set_len(self, spin, mm):
        self._programmatic = True
        spin._base = mm
        spin.setValue(units.length_from_mm(mm))
        self._programmatic = False

    def _set_temp(self, spin, celsius):
        self._programmatic = True
        spin._base = celsius
        spin.setValue(units.temp_from_c(celsius))
        self._programmatic = False

    def _apply_unit_labels(self):
        """Suffixes, decimals, limits and preset figures in the chosen units."""
        sym, dec = units.length_symbol(), units.length_decimals()
        self._programmatic = True
        for spin in (self.min_tol, self.max_tol):
            spin.setDecimals(dec)
            spin.setSuffix(' ' + sym)
            spin.setRange(0.0, units.length_from_mm(1000.0))
        tsym, tdec = units.temperature_symbol(), units.temperature_decimals()
        for spin in (self.mfg_temp, self.op_min, self.op_max):
            spin.setDecimals(tdec)
            spin.setSuffix(' ' + tsym)
            spin.setRange(units.temp_from_c(-273.15), units.temp_from_c(5000.0))
        self._programmatic = False
        for name, rows in self.preset_edits.items():
            for idx, edit in rows:
                value = tolerance_mod.PRESET_VALUES[name][idx]
                edit.setText(f'{value:g} %' if idx == 0 else units.format_length(value, with_unit=True))

    def _snapshot(self):
        return {'mfg': self.mfg_temp._base, 'min': self.op_min._base, 'max': self.op_max._base,
                'tol_min': self.min_tol._base, 'tol_max': self.max_tol._base}

    def _restore(self, snap):
        self._apply_unit_labels()
        self._set_temp(self.mfg_temp, snap['mfg'])
        self._set_temp(self.op_min, snap['min'])
        self._set_temp(self.op_max, snap['max'])
        self._set_len(self.min_tol, snap['tol_min'])
        self._set_len(self.max_tol, snap['tol_max'])

    def _select_preset(self, name):
        for b in self.preset_group.buttons():
            if b.property('preset') == name:
                b.setChecked(True)
        self._refresh_tolerance_table()

    def _preset(self):
        b = self.preset_group.checkedButton()
        return b.property('preset') if b else tolerance_mod.DEFAULT_PRESET

    @staticmethod
    def _set_prop(widget, **props):
        for key, value in props.items():
            widget.setProperty(key, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _refresh_tolerance_table(self, *_):
        """The chosen column lights up: a preset's fixed figures keep grey text,
        Custom's boxes become editable. The other columns dim."""
        chosen = self._preset()
        for name, rows in self.preset_edits.items():
            on = name == chosen
            for _idx, edit in rows:
                self._set_prop(edit, locked=on, dim=not on)
        custom = chosen == 'custom'
        for w in self._custom_widgets:
            w.setEnabled(custom)
            self._set_prop(w, picked=custom, dim=not custom)

    # ---- theme ------------------------------------------------------------------
    def _set_theme(self, mode):
        self.settings.setValue('theme', mode)
        theme.apply(QtWidgets.QApplication.instance(), mode)
        if mode in self.theme_actions:
            self.theme_actions[mode].setChecked(True)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(mode)))
        self.theme_combo.blockSignals(False)

    def _edit_theme(self):
        from .theme_dialog import ThemeDialog
        dialog = ThemeDialog(self)
        if dialog.exec_():
            self._set_theme(dialog.result_mode)
        else:
            self._set_theme(self.settings.value('theme', 'system', str))

    def _show_help(self, topic=None):
        from .help_dialog import HelpDialog
        HelpDialog(self, topic if isinstance(topic, str) else None).exec_()

    @staticmethod
    def _script_path():
        import paths
        return paths.export_script_folder()

    def _show_export_script(self):
        path = self._script_path()
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messages.warning(self, 'Script not found', f'The export script folder is missing: {path}')

    def _copy_script_path(self):
        QtWidgets.QApplication.clipboard().setText(self._script_path())
        self.statusBar().showMessage('Copied: ' + self._script_path(), 5000)

    def _open_fusion_scripts(self):
        folder = os.path.join(os.environ.get('APPDATA', ''), 'Autodesk', 'Autodesk Fusion 360', 'API', 'Scripts')
        if os.path.isdir(folder):
            os.startfile(folder)
        else:
            messages.information(
                self, 'Fusion scripts folder not found',
                'Fusion keeps its scripts in:\n' + folder + '\n\nIt appears once Fusion 360 has been run on this '
                'computer. You can also add the script from inside Fusion (Shift+S, then the + button).')

    def _support(self):
        from .help_dialog import SupportDialog
        SupportDialog(self).exec_()

    def _open_settings(self):
        from .settings_dialog import SettingsDialog
        snap = self._snapshot()          # exact values, in internal units, before the unit change
        dialog = SettingsDialog(self)
        if dialog.exec_():
            self._restore(snap)
            self._update_override_summary()
            self._sync_unit_menu()
            self.statusBar().showMessage('Settings saved.', 4000)

    def _sync_unit_menu(self):
        for (kind, name), act in self.unit_actions.items():
            act.setChecked(name == units.current(kind))

    def _quick_unit(self, kind, name):
        from . import settings_dialog
        snap = self._snapshot()
        settings_dialog.qsettings().setValue(f'unit_{kind}', name)
        settings_dialog.load_units()
        self._restore(snap)
        self._update_override_summary()
        self.statusBar().showMessage(f'{kind.capitalize()} unit: {name}', 4000)

    def _about_settings(self):
        project = self._resolved_project_dir()
        AboutSettingsDialog(self, project if project and os.path.isdir(project) else None).exec_()

    def _check_system_theme(self):
        if theme.mode() == 'system' and theme.system_prefers_dark() != theme.is_dark():
            theme.apply(QtWidgets.QApplication.instance(), 'system')

    # ---- project folder -----------------------------------------------------------
    def _resolved_project_dir(self):
        """The chosen folder, or its `exports` subfolder when that is where
        the Fusion export script put project_data.json and the .step files."""
        p = self.project_edit.text().strip()
        if p and not os.path.exists(os.path.join(p, 'project_data.json')):
            sub = os.path.join(p, 'exports')
            if os.path.exists(os.path.join(sub, 'project_data.json')):
                return sub
        return p

    def _output_dir(self):
        text = self.output_edit.text().strip()
        return text or os.path.join(self.project_edit.text().strip(), 'drawings')

    def _pick_project_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, 'Select project folder', self.project_edit.text())
        if d:
            self.project_edit.setText(d)

    def _pick_output_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, 'Select output folder', self.output_edit.text())
        if d:
            self.output_edit.setText(d)

    def _schedule_scan(self, _text=''):
        """Re-check the folder shortly after typing stops; the scan itself runs
        in a background process with a progress bar."""
        self._scan_timer.start()

    def _project_changed(self, _text=''):
        self._scan_timer.stop()
        self._scan_project()

    def _scan_project(self):
        if self.scan_job is not None:
            self.scan_job.kill()
            self.scan_job = None
        self.scan_bar.hide()
        project = self._resolved_project_dir()
        if not project:
            self._set_status_label('Pick a project folder to begin.', None)
        elif not os.path.isdir(project):
            self._set_status_label('That folder does not exist.', 'error')
        elif not os.path.exists(os.path.join(project, 'project_data.json')):
            self._set_status_label('No exports folder with project_data.json found here - pick the folder that contains "exports".', 'error')
        else:
            self._set_status_label('Loading the project folder...', None)
            self.scan_bar.setValue(0)
            self.scan_bar.show()
            job = self.scan_job = Job('scan', (project,), self)
            job.progress.connect(lambda f, m: (self.scan_bar.setValue(int(f * 1000)),
                                               m and self._set_status_label(m, None), self._repolish_status()))
            job.finished_ok.connect(lambda r, j=job, p=project: self._scan_done(j, p, r))
            job.failed.connect(lambda msg, j=job: self._scan_failed(j, msg))
            job.start()
            return
        self._repolish_status()

    def _scan_failed(self, job, message):
        if job is not self.scan_job:
            return
        self.scan_job = None
        self.scan_bar.hide()
        self._set_status_label(f'Could not read the project folder: {message}', 'error')
        self._repolish_status()

    def _scan_done(self, job, project, result):
        if job is not self.scan_job:
            return
        self.scan_job = None
        self.scan_bar.hide()
        counts = result['counts']
        text = (f'Project found: {result["steps"]} STEP file(s) in {os.path.basename(project)}'
                f' \u2022 {counts.get("drawing", 0)} get a drawing, {counts.get("assembly_only", 0)} '
                f'assembly-only, {counts.get("ignore", 0)} ignored')
        self._set_status_label(text, 'ok')
        self._repolish_status()
        self._load_existing_config(project)
        self.settings.setValue('last_project', self.project_edit.text().strip())
        self._remember_recent(self.project_edit.text().strip())

    def _repolish_status(self):
        label = self.project_status
        label.style().unpolish(label)
        label.style().polish(label)

    def _set_status_label(self, text, state):
        self.project_status.setText(text)
        self.project_status.setProperty('state', state)
        self.project_status.setProperty('muted', state is None)

    def _remember_recent(self, path):
        recent = [p for p in self.settings.value('recent', [], list) if p != path]
        self.settings.setValue('recent', ([path] + recent)[:MAX_RECENT])
        self._refresh_recent()

    def _refresh_recent(self):
        self.recent_menu.clear()
        recent = [p for p in self.settings.value('recent', [], list) if os.path.isdir(p)]
        for p in recent:
            self.recent_menu.addAction(p, lambda _=False, q=p: self.project_edit.setText(q))
        self.recent_menu.setEnabled(bool(recent))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isdir(path):
                self.project_edit.setText(path)
                return

    def _load_existing_config(self, project_dir):
        cfg = tolerance_mod.load_tolerance_config(project_dir)
        self._set_temp(self.mfg_temp, float(cfg.manufacturing_temp_c))
        self._set_temp(self.op_min, float(cfg.operating_min_c))
        self._set_temp(self.op_max, float(cfg.operating_max_c))
        self.custom_pct.setValue(float(cfg.custom_percent))
        self._set_len(self.min_tol, float(cfg.custom_min_mm))
        self._set_len(self.max_tol, float(cfg.custom_max_mm))
        self._select_preset(cfg.preset)
        for key, box in self.kind_boxes.items():
            box.setChecked(cfg.kinds[key])
        self.max_standard.setCurrentIndex(max(self.max_standard.findData(cfg.max_standard), 0))
        self.body_overrides = dict(cfg.body_overrides)
        self._update_override_summary()

    def _update_override_summary(self):
        n = len(self.body_overrides)
        if n == 0:
            self.override_summary.setText('No per-body temperatures set: every body uses the operating range above.')
        else:
            names = ', '.join(sorted(self.body_overrides)[:3])
            more = f' (+{n - 3} more)' if n > 3 else ''
            self.override_summary.setText(f'{n} body(ies) with their own operating range: {names}{more}')

    def _read_config(self):
        return tolerance_mod.ToleranceConfig(
            preset=self._preset(), custom_percent=self.custom_pct.value(),
            manufacturing_temp_c=self.mfg_temp._base, operating_min_c=self.op_min._base,
            operating_max_c=self.op_max._base, custom_min_mm=self.min_tol._base,
            custom_max_mm=self.max_tol._base, body_overrides=self.body_overrides,
            kinds={k: b.isChecked() for k, b in self.kind_boxes.items()}, max_standard=self.max_standard.currentData())

    def _require_project(self, need_data=True):
        project = self._resolved_project_dir()
        if not project or not os.path.isdir(project):
            messages.warning(self, 'No project folder', 'Pick a valid project folder first.')
            return None
        if need_data and not os.path.exists(os.path.join(project, 'project_data.json')):
            messages.warning(
                self, 'Missing project_data.json',
                'No exports folder with project_data.json was found in this folder.')
            return None
        return project

    # ---- editors ------------------------------------------------------------------
    def _edit_drawing_settings(self):
        project = self._require_project(False)
        if project:
            from .dialogs import DrawingSettingsDialog
            DrawingSettingsDialog(self, project).exec_()

    def _edit_body_inclusion(self):
        project = self._require_project(False)
        if not project:
            return
        import body_inclusion
        if not body_inclusion.load_inclusion(project):
            messages.warning(self, 'No bodies found', 'No .step files found in this project folder.')
            return
        from .dialogs import BodyInclusionDialog
        BodyInclusionDialog(self, project).exec_()
        self._project_changed('')

    def _edit_body_overrides(self):
        project = self._require_project(False)
        if not project:
            return
        import body_inclusion
        inclusion = body_inclusion.load_inclusion(project)
        if not any(s in ('drawing', 'assembly_only') for s in inclusion.values()):
            messages.warning(
                self, 'No bodies found',
                'No bodies with inclusion "drawing" or "assembly_only" found in this project folder.')
            return
        from .dialogs import BodyOverridesDialog
        dialog = BodyOverridesDialog(self, project, self.body_overrides)
        if dialog.exec_() and dialog.result_overrides is not None:
            self.body_overrides = dialog.result_overrides
            self._update_override_summary()

    def _edit_assembly_order(self):
        project = self._require_project()
        if not project:
            return

        def done(analyzer):
            from .order_editor import OrderEditor
            OrderEditor(self, project, analyzer).exec_()

        self._start('order', (project,),
                    'Analysing the assembly (the first run can take a few minutes; results are cached)...',
                    on_done=done, determinate=False)

    # ---- saving / generating --------------------------------------------------------
    def _save_config(self):
        project = self._require_project(False)
        if not project:
            return False
        path = tolerance_mod.save_tolerance_config(project, self._read_config())
        self.statusBar().showMessage(f'Saved tolerance settings to {path}', 6000)
        return True

    def _generate(self):
        if self._save_config():
            self._run_generation(include_tolerances=True)

    def _generate_no_tolerance(self):
        self._run_generation(include_tolerances=False)

    def _run_generation(self, include_tolerances):
        project = self._require_project()
        if not project:
            return
        output = self._output_dir()
        self._last_output = output

        def done(generated):
            self.log.appendPlainText(f'\nDone: {len(generated)} drawing(s) generated in {output}')
            self._finish(f'Done - {len(generated)} drawing(s) generated.', 'ok')
            if self.settings.value('flash_when_done', True, bool):
                QtWidgets.QApplication.alert(self, 3000)
            if self.settings.value('open_when_done', False, bool) and os.path.isdir(output):
                os.startfile(output)

        self.log.clear()
        self._start('generate', (project, output, include_tolerances, units.current('length')),
                    'Generating drawings...', on_done=done, determinate=True, finish_on_done=False)

    def _generate_assembly_instructions_pdf(self):
        project = self._require_project()
        if not project:
            return
        folder = self._output_dir()
        os.makedirs(folder, exist_ok=True)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save assembly instructions PDF', os.path.join(folder, 'assembly_instructions.pdf'), 'PDF (*.pdf)')
        if not path:
            return

        def done(saved):
            self.log.appendPlainText(f'Saved assembly instructions to {saved}')
            self._finish('Assembly instructions saved.', 'ok')
            if messages.question(self, 'Done', f'Saved to {saved}\n\nOpen it now?') == \
                    QtWidgets.QMessageBox.Yes:
                os.startfile(saved)

        self._start('guide', (project, path), 'Building the assembly instructions...', on_done=done,
                    determinate=False, finish_on_done=False)

    # ---- worker plumbing --------------------------------------------------------------
    def _set_busy(self, busy):
        for w in (self.generate_btn, self.plain_btn, self.guide_btn):
            w.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)
        self.menu_strip.setEnabled(not busy)

    def _start(self, task, args, message, on_done, determinate, finish_on_done=True):
        if self.worker is not None:
            return
        self._set_busy(True)
        self.stage_label.setText(message)
        self.progress.setRange(0, 1000 if determinate else 0)
        self.progress.setValue(0)
        self._started = time.time()
        self._tick.start(500)
        worker = self.worker = Job(task, args, self)
        worker.progress.connect(self._on_progress)
        worker.log.connect(self._append_log)

        def ok(result):
            self._end_worker()
            if finish_on_done:
                self._finish('Ready.', None)
            on_done(result)

        def fail(msg):
            self._end_worker()
            self._finish('Failed - see the activity log.', 'error')
            messages.critical(self, 'Something went wrong', msg)

        def cancelled():
            self._end_worker()
            self.log.appendPlainText('\nCancelled.')
            self._finish('Cancelled.', None)

        worker.finished_ok.connect(ok)
        worker.failed.connect(fail)
        worker.cancelled.connect(cancelled)
        worker.start()

    def _end_worker(self):
        worker, self.worker = self.worker, None
        self._tick.stop()
        if worker is not None:
            worker.deleteLater()

    def _finish(self, message, state):
        self._set_busy(False)
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000 if state == 'ok' else 0)
        self.stage_label.setText(message)
        self.statusBar().showMessage(message, 8000)

    def _cancel(self):
        if self.worker is not None:
            self.worker.cancel()   # kills the process at once; the cancelled signal does the clean-up

    def _on_progress(self, fraction, message):
        self.progress.setValue(int(fraction * 1000))
        if message:
            self.stage_label.setText(message)

    def _update_elapsed(self):
        secs = int(time.time() - self._started)
        self.elapsed_label.setText(f'{secs // 60}:{secs % 60:02d} elapsed')

    def _append_log(self, text):
        cursor = self.log.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.insertText(text)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()

    # ---- misc ------------------------------------------------------------------------------
    def _open_output_folder(self):
        folder = self._last_output or self._output_dir()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messages.information(self, 'Nothing to open', 'The output folder does not exist yet.')

    def _open_config_folder(self):
        project = self._require_project(False)
        if project:
            path = os.path.dirname(tolerance_mod.config_file(project, 'tolerance_config.json', for_write=True))
            os.startfile(path)

    def _about(self):
        AboutDialog(self).exec_()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.WindowStateChange and hasattr(self, 'titlebar'):
            self.titlebar.sync_state()
        super().changeEvent(event)

    def nativeEvent(self, event_type, message):
        if sys.platform == 'win32' and event_type == b'windows_generic_MSG':
            result = hit_test(self, self.titlebar, message, True)
            if result:
                return result
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        if self.worker is not None:
            if self.settings.value('confirm_close', True, bool) and messages.question(
                    self, 'Work in progress', 'A job is still running. Cancel it and quit?') != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self.worker.kill()
        if self.scan_job is not None:
            self.scan_job.kill()
        self.settings.setValue('size', self.size())
        super().closeEvent(event)
