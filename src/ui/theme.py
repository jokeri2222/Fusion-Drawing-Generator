"""Themes for the Qt GUI: light, dark, the operating system's setting, named
presets and a fully editable custom theme (colours, text colour per kind of
element, fonts, corner radius).

`apply(app, mode)` styles the whole application (Fusion style + palette +
stylesheet). `colors()` returns the active palette as a dict, for widgets that
paint themselves (the assembly block editor, the 3D viewport background).

A theme mode is one of 'system', 'light', 'dark', 'preset:<name>' or 'custom'.
The custom theme and the font options are kept in QSettings.
"""
import json
import os
import sys
import tempfile

from PyQt5 import QtCore, QtGui, QtWidgets

MODES = ('system', 'light', 'dark')

LIGHT = {
    'window': '#f4f5f7', 'surface': '#ffffff', 'surface_alt': '#eceef2', 'text': '#1d2330',
    'muted': '#667085', 'border': '#d4d8e0', 'accent': '#2563eb', 'accent_hover': '#1d4ed8',
    'accent_text': '#ffffff', 'disabled': '#a4aab6', 'input': '#ffffff', 'log': '#fafbfc',
    'danger': '#c62828', 'ok': '#1b7f4b', 'warn': '#b45309', 'selection': '#cfe0ff',
    'viewport': '#eef0f5',
    'depth_fill': ('#e8eef9', '#e6f4ea', '#fdf1e0', '#f3e8f9', '#e0f4f6'),
    'depth_line': ('#5b7fc4', '#4e9a63', '#d08a2e', '#9b5bc4', '#3a9aa6'),
    'set_fill': '#fbf6ea', 'set_line': '#c77d12', 'part_fill': '#fff7d6', 'part_line': '#9a9a9a',
    'fasten_fill': '#dde6ee', 'fasten_line': '#5a7186', 'picked_fill': '#ffd9d9', 'picked_line': '#d33333',
    'canvas': '#ffffff', 'ghost_fill': '#cfe3ff', 'ghost_line': '#2a62c9',
    'state_drawing': '#1b7f4b', 'state_assembly': '#b7791f', 'state_ignore': '#c62828',
    'pick_normal': '#3d6ea6', 'pick_selected': '#2f7df6', 'pick_box': '#2a5db0', 'close_hover': '#e5484d',
}
DARK = {
    'window': '#1b1e25', 'surface': '#242831', 'surface_alt': '#2d323d', 'text': '#e6e8ee',
    'muted': '#97a0b3', 'border': '#3a4050', 'accent': '#5b8def', 'accent_hover': '#78a1f5',
    'accent_text': '#0e1320', 'disabled': '#667085', 'input': '#1b1e25', 'log': '#16181e',
    'danger': '#ef6b6b', 'ok': '#4cc38a', 'warn': '#e3a03c', 'selection': '#33466f',
    'viewport': '#20242c',
    'depth_fill': ('#27324a', '#243a2f', '#43361f', '#38294a', '#1f3b40'),
    'depth_line': ('#7fa0e6', '#6cc08a', '#e0a24a', '#bf8ae0', '#5cc4d0'),
    'set_fill': '#3d3322', 'set_line': '#e0a24a', 'part_fill': '#4a4220', 'part_line': '#8a8a8a',
    'fasten_fill': '#2c3a47', 'fasten_line': '#7b95ab', 'picked_fill': '#5a2d2d', 'picked_line': '#ef6b6b',
    'canvas': '#1b1e25', 'ghost_fill': '#2b4373', 'ghost_line': '#78a1f5',
    'state_drawing': '#4cc38a', 'state_assembly': '#e9c34a', 'state_ignore': '#ef6b6b',
    'pick_normal': '#3d6ea6', 'pick_selected': '#2f7df6', 'pick_box': '#78a1f5', 'close_hover': '#e5484d',
}

# Colours people can edit, grouped for the theme editor: (key, label).
EDITABLE = (
    ('Surfaces', (('window', 'Window background'), ('surface', 'Cards, menus and buttons'),
                  ('surface_alt', 'Hover and alternate rows'), ('input', 'Input fields'),
                  ('log', 'Activity log background'), ('viewport', '3D viewport background'),
                  ('border', 'Borders and dividers'))),
    ('Accent', (('accent', 'Accent (primary button, focus)'), ('accent_hover', 'Accent when hovered'),
                ('accent_text', 'Text on accent'), ('selection', 'Selected rows'))),
    ('Text by element', (('text', 'Default text'), ('title_text', 'Window title'),
                         ('heading_text', 'Section headings'), ('muted', 'Descriptions and hints'),
                         ('button_text', 'Button text'), ('input_text', 'Input text'),
                         ('log_text', 'Activity log text'), ('menu_text', 'Menu text'),
                         ('status_text', 'Status bar text'), ('disabled', 'Disabled text'))),
    ('Status', (('ok', 'Success'), ('warn', 'Warning'), ('danger', 'Error and cancel'),
                ('close_hover', 'Close button when hovered'))),
    ('Body states', (('state_drawing', 'Drawing (included)'), ('state_assembly', 'Assembly only'),
                     ('state_ignore', 'Ignored'), ('pick_normal', '3D view: body'),
                     ('pick_selected', '3D view: selected body'), ('pick_box', '3D view: selection box'))),
    ('Assembly order', (('canvas', 'Canvas background'), ('part_fill', 'Part fill'), ('part_line', 'Part outline'),
                        ('set_fill', 'Set fill'), ('set_line', 'Set outline'),
                        ('fasten_fill', 'Fastener fill'), ('fasten_line', 'Fastener outline'),
                        ('picked_fill', 'Picked fill'), ('picked_line', 'Picked outline'),
                        ('ghost_fill', 'Drag ghost fill'), ('ghost_line', 'Drag ghost outline'),
                        ('depth_fill_1', 'Level 1 fill'), ('depth_line_1', 'Level 1 outline'),
                        ('depth_fill_2', 'Level 2 fill'), ('depth_line_2', 'Level 2 outline'),
                        ('depth_fill_3', 'Level 3 fill'), ('depth_line_3', 'Level 3 outline'),
                        ('depth_fill_4', 'Level 4 fill'), ('depth_line_4', 'Level 4 outline'),
                        ('depth_fill_5', 'Level 5 fill'), ('depth_line_5', 'Level 5 outline'))),
)
DEPTH_KEYS = ('depth_fill', 'depth_line')
TEXT_FALLBACK = {'title_text': 'text', 'heading_text': 'text', 'button_text': 'text', 'input_text': 'text',
                 'log_text': 'text', 'menu_text': 'text', 'status_text': 'muted'}

DEFAULT_FONTS = {'family': 'Segoe UI', 'mono': 'Consolas', 'size': 9, 'title_size': 17, 'heading_size': 10,
                 'log_size': 9, 'radius': 6}

# Named presets: a base ('light'/'dark') plus colour overrides.
PRESETS = {
    'Midnight': ('dark', {'window': '#0f1220', 'surface': '#171b2e', 'surface_alt': '#202540', 'input': '#0f1220',
                          'log': '#0b0e1a', 'border': '#2b3252', 'accent': '#8b7cf6', 'accent_hover': '#a496fa',
                          'accent_text': '#0f1220', 'selection': '#3a3a7a', 'viewport': '#141830'}),
    'Nord': ('dark', {'window': '#2e3440', 'surface': '#3b4252', 'surface_alt': '#434c5e', 'input': '#2e3440',
                      'log': '#272c36', 'text': '#eceff4', 'muted': '#a7b1c4', 'border': '#4c566a',
                      'accent': '#88c0d0', 'accent_hover': '#9fd0de', 'accent_text': '#2e3440',
                      'selection': '#4c6a7d', 'viewport': '#323846', 'ok': '#a3be8c', 'warn': '#ebcb8b',
                      'danger': '#bf616a'}),
    'Dracula': ('dark', {'window': '#282a36', 'surface': '#303341', 'surface_alt': '#3a3d4e', 'input': '#282a36',
                         'log': '#21222c', 'text': '#f8f8f2', 'muted': '#a6accd', 'border': '#44475a',
                         'accent': '#bd93f9', 'accent_hover': '#cfaefc', 'accent_text': '#282a36',
                         'selection': '#4b4f78', 'viewport': '#2c2e3b', 'ok': '#50fa7b', 'warn': '#f1fa8c',
                         'danger': '#ff5555'}),
    'Solarized Dark': ('dark', {'window': '#002b36', 'surface': '#073642', 'surface_alt': '#0d4553',
                                'input': '#002b36', 'log': '#00232c', 'text': '#eee8d5', 'muted': '#93a1a1',
                                'border': '#18505e', 'accent': '#268bd2', 'accent_hover': '#4aa3e3',
                                'accent_text': '#fdf6e3', 'selection': '#14546a', 'viewport': '#05303b',
                                'ok': '#859900', 'warn': '#b58900', 'danger': '#dc322f'}),
    'Forest': ('dark', {'window': '#161d19', 'surface': '#1e2823', 'surface_alt': '#27342d', 'input': '#161d19',
                        'log': '#111712', 'border': '#34463c', 'accent': '#4fb477', 'accent_hover': '#6cc891',
                        'accent_text': '#0b1410', 'selection': '#2f5a42', 'viewport': '#1a231e'}),
    'Graphite': ('dark', {'window': '#121212', 'surface': '#1c1c1c', 'surface_alt': '#272727', 'input': '#121212',
                          'log': '#0d0d0d', 'text': '#ececec', 'muted': '#9a9a9a', 'border': '#333333',
                          'accent': '#e0e0e0', 'accent_hover': '#ffffff', 'accent_text': '#121212',
                          'selection': '#3d3d3d', 'viewport': '#171717'}),
    'High Contrast': ('dark', {'window': '#000000', 'surface': '#0a0a0a', 'surface_alt': '#1a1a1a',
                               'input': '#000000', 'log': '#000000', 'text': '#ffffff', 'muted': '#d0d0d0',
                               'border': '#ffffff', 'accent': '#ffd400', 'accent_hover': '#ffe45c',
                               'accent_text': '#000000', 'selection': '#0043a8', 'viewport': '#000000',
                               'disabled': '#8a8a8a'}),
    'Solarized Light': ('light', {'window': '#fdf6e3', 'surface': '#fffaf0', 'surface_alt': '#f2ead3',
                                  'input': '#fffaf0', 'log': '#fbf3dc', 'text': '#073642', 'muted': '#657b83',
                                  'border': '#e0d8bd', 'accent': '#268bd2', 'accent_hover': '#1a73b3',
                                  'accent_text': '#fdf6e3', 'selection': '#cfe3ee', 'viewport': '#f5eed7'}),
    'Ocean': ('light', {'window': '#eef4f8', 'surface': '#ffffff', 'surface_alt': '#e2edf4', 'input': '#ffffff',
                        'log': '#f7fafc', 'text': '#10303f', 'muted': '#5b7a8a', 'border': '#cbdde8',
                        'accent': '#0f8fb8', 'accent_hover': '#0c7596', 'selection': '#c4e6f2',
                        'viewport': '#e6eff5'}),
    'Rose': ('light', {'window': '#fbf3f5', 'surface': '#ffffff', 'surface_alt': '#f6e6ea', 'input': '#ffffff',
                       'log': '#fdf8f9', 'text': '#33202a', 'muted': '#85666f', 'border': '#ecd3da',
                       'accent': '#d6336c', 'accent_hover': '#b8285a', 'selection': '#f8d4e0',
                       'viewport': '#f7ecef'}),
    'Paper': ('light', {'window': '#f3efe6', 'surface': '#fbf8f1', 'surface_alt': '#ebe5d6', 'input': '#fbf8f1',
                        'log': '#f8f4ea', 'text': '#2a2620', 'muted': '#7a7366', 'border': '#d9d1bd',
                        'accent': '#b45f06', 'accent_hover': '#8f4a05', 'selection': '#f0dcb8',
                        'viewport': '#eee8da'}),
}

_current = {'mode': 'system', 'dark': False, 'colors': LIGHT, 'fonts': dict(DEFAULT_FONTS)}


def _settings():
    return QtCore.QSettings('FusionDrawingGenerator', 'DrawingGenerator')


# ---- stored preferences -------------------------------------------------------
def load_custom():
    """The saved custom theme: {'base': 'dark'|'light', 'colors': {key: '#hex'}}."""
    try:
        data = json.loads(_settings().value('custom_theme', '', str))
        if data.get('base') in ('light', 'dark') and isinstance(data.get('colors'), dict):
            return data
    except (ValueError, AttributeError):
        pass
    return {'base': 'dark', 'colors': {}}


def save_custom(custom):
    _settings().setValue('custom_theme', json.dumps(custom))


def load_fonts():
    fonts = dict(DEFAULT_FONTS)
    try:
        stored = json.loads(_settings().value('fonts', '', str))
        for key, default in DEFAULT_FONTS.items():
            if key in stored and type(stored[key]) in (type(default), int, float):
                fonts[key] = stored[key]
    except (ValueError, AttributeError):
        pass
    return fonts


def save_fonts(fonts):
    _settings().setValue('fonts', json.dumps(fonts))


def mode_label(mode):
    if mode.startswith('preset:'):
        return mode[len('preset:'):]
    return {'system': 'Match system', 'light': 'Light', 'dark': 'Dark', 'custom': 'Custom'}.get(mode, mode)


def all_modes():
    return list(MODES) + ['preset:' + n for n in PRESETS] + ['custom']


# ---- resolving ------------------------------------------------------------------
def system_prefers_dark():
    """Windows' app-theme setting; elsewhere, how dark the platform palette is."""
    if sys.platform == 'win32':
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            return value == 0
        except OSError:
            pass
    app = QtWidgets.QApplication.instance()
    if app is not None:
        return app.style().standardPalette().color(QtGui.QPalette.Window).lightness() < 128
    return False


def is_dark():
    return _current['dark']


def colors():
    return _current['colors']


def fonts():
    return _current['fonts']


def mode():
    return _current['mode']


def resolve(mode, custom=None):
    """(base_is_dark, colour dict) for a mode; per-element text colours that were
    not set follow the default text colour."""
    if mode == 'custom':
        custom = custom or load_custom()
        dark, over = custom['base'] == 'dark', custom['colors']
    elif mode.startswith('preset:') and mode[len('preset:'):] in PRESETS:
        base, over = PRESETS[mode[len('preset:'):]]
        dark = base == 'dark'
    else:
        dark = system_prefers_dark() if mode == 'system' else mode == 'dark'
        over = {}
    c = dict(DARK if dark else LIGHT)
    for kind in DEPTH_KEYS:
        for i, value in enumerate(c[kind]):
            c[f'{kind}_{i + 1}'] = value
    c.update({k: v for k, v in over.items() if k in c or k in TEXT_FALLBACK})
    for key, fallback in TEXT_FALLBACK.items():
        c.setdefault(key, c[fallback])
    for kind in DEPTH_KEYS:
        c[kind] = tuple(c[f'{kind}_{i + 1}'] for i in range(5))
    if 'accent' in over and 'accent_hover' not in over:
        c['accent_hover'] = QtGui.QColor(over['accent']).lighter(112 if dark else 90).name()
    return dark, c


def _palette(c):
    p = QtGui.QPalette()
    q = QtGui.QColor
    for role, key in ((QtGui.QPalette.Window, 'window'), (QtGui.QPalette.Base, 'input'),
                      (QtGui.QPalette.AlternateBase, 'surface_alt'), (QtGui.QPalette.Button, 'surface'),
                      (QtGui.QPalette.ToolTipBase, 'surface'), (QtGui.QPalette.Highlight, 'accent'),
                      (QtGui.QPalette.WindowText, 'text'), (QtGui.QPalette.Text, 'input_text'),
                      (QtGui.QPalette.ButtonText, 'button_text'), (QtGui.QPalette.ToolTipText, 'text'),
                      (QtGui.QPalette.HighlightedText, 'accent_text'), (QtGui.QPalette.Link, 'accent'),
                      (QtGui.QPalette.PlaceholderText, 'muted')):
        p.setColor(role, q(c[key]))
    for role in (QtGui.QPalette.WindowText, QtGui.QPalette.Text, QtGui.QPalette.ButtonText):
        p.setColor(QtGui.QPalette.Disabled, role, q(c['disabled']))
    return p


def _arrow(c):
    """Chevron image for combo boxes, drawn once per colour into the temp folder."""
    path = os.path.join(tempfile.gettempdir(), f"fdg_chevron_{c['muted'].lstrip('#')}.png")
    if not os.path.exists(path):
        img = QtGui.QImage(20, 20, QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor(c['muted']), 2.4)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(4, 7), QtCore.QPointF(10, 13), QtCore.QPointF(16, 7)]))
        p.end()
        img.save(path)
    return path.replace(chr(92), '/')


def _stylesheet(c, f):
    r = int(f['radius'])
    big = r + 4
    fam = f"'{f['family']}', 'Segoe UI', 'Inter', sans-serif"
    mono = f"'{f['mono']}', Consolas, 'Cascadia Mono', monospace"
    return f"""
    QWidget {{ font-family: {fam}; font-size: {f['size']}pt; }}
    QMainWindow, QDialog {{ background: {c['window']}; }}
    QToolTip {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
    QLabel#title {{ font-size: {f['title_size']}pt; font-weight: 700; color: {c['title_text']}; }}
    QLabel#subtitle, QLabel[muted="true"] {{ color: {c['muted']}; }}
    QLabel[state="ok"] {{ color: {c['ok']}; }}
    QLabel[state="warn"] {{ color: {c['warn']}; }}
    QLabel[state="error"] {{ color: {c['danger']}; }}
    QFrame#card {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: {big}px; }}
    QFrame#divider {{ background: {c['border']}; border: none; }}
    QFrame#card QLabel {{ background: transparent; }}
    QLabel#cardTitle {{ font-weight: 700; font-size: {f['heading_size']}pt; color: {c['heading_text']}; }}
    QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QTextBrowser {{
        background: {c['input']}; color: {c['input_text']}; border: 1px solid {c['border']};
        border-radius: {r}px; padding: 4px 8px;
        selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
    QLineEdit:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {{ color: {c['disabled']};
        background: {c['surface_alt']}; }}
    QLineEdit[locked="true"], QDoubleSpinBox[locked="true"] {{ color: {c['muted']}; border: 1px solid {c['accent']};
        background: {c['input']}; }}
    QLineEdit[dim="true"], QDoubleSpinBox[dim="true"] {{ color: {c['disabled']}; background: {c['surface_alt']};
        border: 1px solid {c['border']}; }}
    QDoubleSpinBox[picked="true"] {{ color: {c['input_text']}; border: 1px solid {c['accent']}; }}
    QPlainTextEdit#log {{ background: {c['log']}; color: {c['log_text']}; font-family: {mono};
        font-size: {f['log_size']}pt; }}
    QPushButton {{ background: {c['surface']}; color: {c['button_text']}; border: 1px solid {c['border']};
        border-radius: {r}px; padding: 6px 14px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:pressed {{ background: {c['surface_alt']}; }}
    QPushButton:disabled {{ color: {c['disabled']}; background: {c['surface_alt']}; border-color: {c['border']}; }}
    QPushButton#primary {{ background: {c['accent']}; color: {c['accent_text']}; border: none; font-weight: 700;
        padding: 9px 22px; }}
    QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
    QPushButton#primary:disabled {{ background: {c['border']}; color: {c['disabled']}; }}
    QPushButton#danger {{ color: {c['danger']}; }}
    QPushButton#link {{ border: none; background: transparent; color: {c['accent']}; padding: 2px 4px;
        text-align: left; }}
    QPushButton#link:hover {{ text-decoration: underline; }}
    QProgressBar {{ background: {c['surface_alt']}; border: none; border-radius: {r}px; height: 12px;
        text-align: center; color: {c['text']}; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: {r}px; }}
    QTableWidget, QTreeWidget, QListWidget {{ background: {c['surface']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: {r + 2}px; gridline-color: {c['border']};
        alternate-background-color: {c['surface_alt']}; outline: 0; }}
    QListWidget::item {{ padding: 6px 8px; border-radius: {r}px; }}
    QListWidget::item:selected {{ background: {c['selection']}; color: {c['text']}; }}
    QHeaderView::section {{ background: {c['surface_alt']}; color: {c['muted']}; border: none;
        border-bottom: 1px solid {c['border']}; padding: 6px 8px; font-weight: 600; }}
    QTableWidget::item:selected, QTreeWidget::item:selected {{ background: {c['selection']}; color: {c['text']}; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: {r}px; top: -1px; }}
    QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 7px 14px; border: none;
        border-bottom: 2px solid transparent; }}
    QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QProgressBar#scanBar {{ border: none; border-radius: 2px; background: {c['surface_alt']}; }}
    QProgressBar#scanBar::chunk {{ background: {c['accent']}; border-radius: 2px; }}
    QDialog#framedDialog {{ background: {c['window']}; border: 1px solid {c['border']}; }}
    QWidget#shell {{ background: {c['window']}; border: 1px solid {c['border']}; }}
    QWidget#titleBar {{ background: {c['surface']}; border: none; border-bottom: 1px solid {c['border']}; }}
    QWidget#titleBar QLabel {{ background: transparent; }}
    QLabel#titleName {{ font-weight: 700; color: {c['title_text']}; }}
    QToolButton#menuButton {{ background: transparent; border: none; border-radius: {r}px; padding: 5px 11px;
        color: {c['menu_text']}; }}
    QToolButton#menuButton:hover, QToolButton#menuButton:checked, QToolButton#menuButton:pressed {{
        background: {c['surface_alt']}; }}
    QToolButton#menuButton::menu-indicator {{ image: none; width: 0; }}
    QToolButton#winButton, QToolButton#winClose {{ background: transparent; border: none; border-radius: 0;
        font-family: 'Segoe Fluent Icons', 'Segoe MDL2 Assets'; font-size: 9pt; color: {c['text']}; }}
    QToolButton#winButton:hover {{ background: {c['surface_alt']}; }}
    QToolButton#winClose:hover {{ background: {c['close_hover']}; color: white; }}
    QMenu {{ background: {c['surface']}; color: {c['menu_text']}; border: 1px solid {c['border']};
        border-radius: {big}px; padding: 6px; }}
    QMenu::item {{ padding: 7px 28px 7px 14px; border-radius: {r}px; margin: 1px 0; }}
    QMenu::item:selected {{ background: {c['selection']}; color: {c['menu_text']}; }}
    QMenu::item:disabled {{ color: {c['disabled']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 5px 8px; }}
    QComboBox {{ padding-right: 26px; }}
    QComboBox:on {{ border: 1px solid {c['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 26px; }}
    QComboBox::down-arrow {{ image: url({_arrow(c)}); width: 10px; height: 10px; }}
    QListView#comboList {{ background: {c['surface']}; color: {c['text']}; border: 1px solid {c['border']};
        border-radius: {big}px; padding: 5px; outline: 0; }}
    QListView#comboList::item {{ min-height: 28px; padding: 0 10px; border-radius: {r}px; }}
    QListView#comboList::item:hover, QListView#comboList::item:selected {{ background: {c['selection']};
        color: {c['text']}; }}
    QStatusBar {{ background: {c['window']}; color: {c['status_text']}; }}
    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 4px; min-width: 28px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
    QSplitter::handle {{ background: {c['border']}; }}
    """


def apply(app, mode='system', preview=None):
    """Style `app` for `mode`; returns True if the result is dark.

    `preview` ({'base', 'colors', 'fonts'}) styles the app with unsaved editor
    values instead of the stored custom theme and fonts."""
    known = mode in MODES or mode == 'custom' or mode.startswith('preset:')
    mode = mode if known else 'system'
    custom = preview and {'base': preview['base'], 'colors': preview['colors']}
    dark, c = resolve(mode, custom)
    f = dict(preview['fonts']) if preview and 'fonts' in preview else load_fonts()
    _current.update(mode=mode, dark=dark, colors=c, fonts=f)
    app.setStyle('Fusion')
    app.setPalette(_palette(c))
    app.setStyleSheet(_stylesheet(c, f))
    return dark
