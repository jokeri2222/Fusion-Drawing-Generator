"""Editors opened from the main window: drawing settings, body inclusion and
per-body temperature overrides. Each one reads and writes the project's saved
configuration through the same modules the generator uses."""
import dataclasses
import glob
import os

from PyQt5 import QtCore, QtGui, QtWidgets

from . import messages
from .frame import FramedDialog

import units

from . import theme
from .body_picker import BodyPicker3D



def state_color(state):
    """Colour for a body's inclusion state, from the active theme."""
    c = theme.colors()
    return {'drawing': c['state_drawing'], 'assembly_only': c['state_assembly'], 'ignore': c['state_ignore']}.get(state)


STATE_TIPS = {
    'drawing': 'Gets its own dimensioned drawing and appears in the assembly instructions.',
    'assembly_only': 'No drawing of its own, but still appears in the assembly instructions.',
    'ignore': 'Left out of everything.',
}


def hint(text):
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setProperty('muted', True)
    return label


def button_box(dialog, on_save, save_text='Save', extra=()):
    row = QtWidgets.QHBoxLayout()
    for button in extra:
        row.addWidget(button)
    row.addStretch(1)
    cancel = QtWidgets.QPushButton('Cancel')
    cancel.clicked.connect(dialog.reject)
    save = QtWidgets.QPushButton(save_text)
    save.setObjectName('primary')
    save.setDefault(True)
    save.clicked.connect(on_save)
    row.addWidget(cancel)
    row.addWidget(save)
    return row


# --------------------------------------------------------------------------
class DrawingSettingsDialog(FramedDialog):
    """Thresholds deciding which extra dimensions and diameter callouts the
    drawings get (feature_dimensions.FeatureDimensionSettings)."""

    def __init__(self, parent, project_dir):
        super().__init__(parent)
        import feature_dimensions as fd
        self.fd, self.project_dir = fd, project_dir
        self.setWindowTitle('Drawing Settings')
        self.resize(720, 640)
        settings = fd.load_settings(project_dir)
        layout = QtWidgets.QVBoxLayout(self.body)
        layout.addWidget(hint(
            'These thresholds decide which extra dimensions and diameter callouts the drawings get beyond the '
            'recognised holes and fillets. The defaults are neutral: nothing is filtered or merged. Raise '
            'them to thin out a crowded view. Saved per project.'))
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        body = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(body)
        column.setContentsMargins(4, 4, 14, 4)
        column.setSpacing(16)
        self.fields = {}
        for fld in dataclasses.fields(settings):
            name = QtWidgets.QLabel(self._label(fld.name))
            name.setObjectName('cardTitle')
            edit = QtWidgets.QLineEdit(self._shown(fld.name, getattr(settings, fld.name)))
            edit.setFixedWidth(160)
            self.fields[fld.name] = edit
            note = hint(fd.SETTING_HELP.get(fld.name, ''))
            note.setMinimumWidth(1)
            note.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.MinimumExpanding)
            item = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(item)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(5)
            lay.addWidget(name)
            lay.addWidget(edit, 0, QtCore.Qt.AlignLeft)
            lay.addWidget(note)
            column.addWidget(item)
        column.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        reset = QtWidgets.QPushButton('Reset to defaults')
        reset.clicked.connect(self._reset)
        layout.addLayout(button_box(self, self._save, extra=[reset]))

    @staticmethod
    def _is_length(name):
        return name.endswith('_mm')

    def _label(self, name):
        text = name.replace('_', ' ').capitalize()
        if self._is_length(name):
            text = text.replace(' mm', f' ({units.length_symbol()})')
        return text

    def _shown(self, name, value):
        if self._is_length(name):
            return f'{units.length_from_mm(value):.10g}'
        return str(value)

    def _reset(self):
        defaults = self.fd.FeatureDimensionSettings()
        for name, edit in self.fields.items():
            edit.setText(self._shown(name, getattr(defaults, name)))

    def _save(self):
        new = self.fd.FeatureDimensionSettings()
        for fld in dataclasses.fields(new):
            text = self.fields[fld.name].text().strip()
            try:
                value = int(float(text)) if isinstance(getattr(new, fld.name), int) else float(text)
            except ValueError:
                messages.warning(self, 'Invalid value', f'"{text}" is not a number ({fld.name}).')
                return
            if value < 0:
                messages.warning(self, 'Invalid value', f'{fld.name} cannot be negative.')
                return
            if self._is_length(fld.name):
                value = round(units.length_to_mm(value), 9)
            setattr(new, fld.name, value)
        self.fd.save_settings(self.project_dir, new)
        self.accept()


# --------------------------------------------------------------------------
class BodyInclusionDialog(FramedDialog):
    """What each body is used for: drawing / assembly_only / ignore."""
    BODY = QtCore.Qt.UserRole

    def __init__(self, parent, project_dir):
        super().__init__(parent)
        import body_inclusion
        self.bi, self.project_dir = body_inclusion, project_dir
        self.setWindowTitle('Body Inclusion')
        self.resize(760, 680)
        inclusion = body_inclusion.load_inclusion(project_dir)
        tree = body_inclusion.load_assembly_tree(project_dir)
        layout = QtWidgets.QVBoxLayout(self.body)
        layout.addWidget(hint(
            'Choose what each body is used for. "drawing": its own dimensioned drawing, shown in the assembly '
            'instructions. "assembly_only": no drawing of its own, still shown in the assembly instructions '
            '(the default for fasteners and bodies hidden in Fusion). "ignore": left out of everything. '
            'Select bodies or whole branches, then use the buttons to set them at once.'))
        self.view = QtWidgets.QTreeWidget()
        self.view.setHeaderLabels(['Body / assembly branch', 'Inclusion'])
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.view.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.view.header().resizeSection(1, 140)
        layout.addWidget(self.view, 1)

        placed = set()

        def insert(parent_item, node):
            item = QtWidgets.QTreeWidgetItem(parent_item, [node['name'], ''])
            for body in node['bodies']:
                self._body_item(item, body, inclusion.get(body, 'drawing'))
                placed.add(body)
            for child in node['children']:
                insert(item, child)
            item.setExpanded(True)

        if tree:
            insert(self.view.invisibleRootItem(), tree)
        leftover = sorted(n for n in inclusion if n not in placed)
        if leftover:
            group = QtWidgets.QTreeWidgetItem(self.view, ['Ungrouped' if tree else 'All bodies', ''])
            for body in leftover:
                self._body_item(group, body, inclusion[body])
            group.setExpanded(True)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel('Set selected to:'))
        for state in body_inclusion.VALID_STATES:
            b = QtWidgets.QPushButton(state)
            b.setToolTip(STATE_TIPS[state])
            b.clicked.connect(lambda _=False, s=state: self._set_state(s))
            bar.addWidget(b)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addLayout(button_box(self, self._save))

    def _body_item(self, parent, name, state):
        item = QtWidgets.QTreeWidgetItem(parent, [name, state])
        item.setData(0, self.BODY, name)
        item.setTextAlignment(1, QtCore.Qt.AlignCenter)
        self._tint(item)

    def _tint(self, item):
        state = item.text(1)
        item.setToolTip(1, STATE_TIPS.get(state, ''))
        color = QtGui.QBrush(QtGui.QColor(state_color(state) or theme.colors()['text']))
        item.setForeground(0, color)
        item.setForeground(1, color)

    def _bodies_under(self, item):
        if item.data(0, self.BODY) is not None:
            return [item]
        return [b for i in range(item.childCount()) for b in self._bodies_under(item.child(i))]

    def _set_state(self, state):
        for sel in self.view.selectedItems():
            for item in self._bodies_under(sel):
                item.setText(1, state)
                self._tint(item)

    def _all_bodies(self, item=None):
        item = item or self.view.invisibleRootItem()
        if item.data(0, self.BODY) is not None:
            return [item]
        return [b for i in range(item.childCount()) for b in self._all_bodies(item.child(i))]

    def _save(self):
        self.bi.save_inclusion(self.project_dir, {i.data(0, self.BODY): i.text(1) for i in self._all_bodies()})
        self.accept()


# --------------------------------------------------------------------------
class BodyOverridesDialog(FramedDialog):
    """Per-body operating temperature range, with a 3D picker to select rows."""
    COL_HIDE, COL_NAME, COL_MIN, COL_MAX = range(4)

    def __init__(self, parent, project_dir, overrides):
        super().__init__(parent)
        import body_inclusion
        self.overrides = dict(overrides)
        self.result_overrides = None
        self.setWindowTitle('Per-Body Temperatures')
        self.resize(1180, 680)
        inclusion = body_inclusion.load_inclusion(project_dir)
        self.step_paths = [p for p in sorted(glob.glob(os.path.join(project_dir, '*.step')))
                           if inclusion.get(os.path.splitext(os.path.basename(p))[0]) in ('drawing', 'assembly_only')]
        self.names = [os.path.splitext(os.path.basename(p))[0] for p in self.step_paths]
        self.assembly_only = {n for n in self.names if inclusion.get(n) == 'assembly_only'}
        self._guard = False

        layout = QtWidgets.QVBoxLayout(self.body)
        layout.addWidget(hint(
            'Give a body its own operating temperature range when it runs hotter or colder than the rest of the '
            'assembly (a bracket on a motor, for example). Leave both fields blank to use the global range. '
            'Select bodies in the 3D view or the table; typing a value fills it into every selected row. '
            'Green names get a drawing, yellow names are assembly-only.'))
        split = QtWidgets.QSplitter()
        split.setChildrenCollapsible(False)
        split.setHandleWidth(8)
        layout.addWidget(split, 1)

        left = QtWidgets.QWidget()
        left.setMinimumWidth(360)
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        views = QtWidgets.QHBoxLayout()
        self.picker = BodyPicker3D()
        for preset in ('ISO', 'Front', 'Top', 'Right'):
            b = QtWidgets.QPushButton(preset)
            b.setFixedWidth(64)
            b.clicked.connect(lambda _=False, p=preset: self.picker.set_view(p))
            views.addWidget(b)
        views.addStretch(1)
        lv.addLayout(views)
        lv.addWidget(hint('Left: select / box-select  |  Right: orbit  |  Middle: pan  |  Wheel: zoom  |  V: hide'))
        lv.addWidget(self.picker, 1)
        self.load_note = hint('Loading 3D view...')
        lv.addWidget(self.load_note)
        self.load_bar = QtWidgets.QProgressBar()
        self.load_bar.setObjectName('scanBar')
        self.load_bar.setTextVisible(False)
        self.load_bar.setFixedHeight(5)
        lv.addWidget(self.load_bar)
        split.addWidget(left)

        self.table = QtWidgets.QTableWidget(len(self.names), 4)
        self.table.setHorizontalHeaderLabels(['Hide', 'Body', f'Operating min ({units.temperature_symbol()})', f'Operating max ({units.temperature_symbol()})'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self.COL_HIDE, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_NAME, QtWidgets.QHeaderView.Stretch)
        for col in (self.COL_MIN, self.COL_MAX):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.Fixed)
            header.resizeSection(col, 150)
        self.table.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        for row, name in enumerate(self.names):
            existing = self.overrides.get(name, {})
            hide = QtWidgets.QTableWidgetItem()
            hide.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            hide.setCheckState(QtCore.Qt.Unchecked)
            label = QtWidgets.QTableWidgetItem(name)
            label.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            label.setForeground(QtGui.QBrush(QtGui.QColor(state_color('assembly_only' if name in self.assembly_only else 'drawing'))))
            self.table.setItem(row, self.COL_HIDE, hide)
            self.table.setItem(row, self.COL_NAME, label)
            for col, key in ((self.COL_MIN, 'operating_min_c'), (self.COL_MAX, 'operating_max_c')):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem(
                    f'{units.temp_from_c(existing[key]):.6g}' if key in existing else ''))
        split.addWidget(self.table)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([520, 660])

        layout.addLayout(button_box(self, self._save))

        self.picker.set_background(theme.colors()['viewport'])
        QtCore.QTimer.singleShot(50, self._load_picker)

    def _load_picker(self):
        c = theme.colors()
        self.picker.set_colors(c['pick_normal'], c['pick_selected'], c['pick_box'])
        self.picker.selectionChanged.connect(self._picker_changed)
        self.picker.visibilityChanged.connect(self._picker_visibility)
        self.table.itemSelectionChanged.connect(self._table_changed)
        self.table.itemChanged.connect(self._item_changed)
        self.load_bar.setRange(0, max(1, len(self.names)))
        self.load_bar.show()

        def step(i, n, name):
            self.load_bar.setValue(i + 1)
            self.load_note.setText(f'Loading 3D view... {name}')

        def done():
            self.load_bar.hide()
            self.load_note.hide()

        self.picker.load_bodies_async(
            self.step_paths, self.names,
            {n: state_color('assembly_only' if n in self.assembly_only else 'drawing') for n in self.names},
            progress=step, done=done)

    def done(self, result):
        self.picker.stop_loading()   # stop the meshing process if the window closes early
        super().done(result)

    # ---- two-way selection / mirroring ------------------------------------
    def _selected_names(self):
        return {self.names[i.row()] for i in self.table.selectionModel().selectedRows()}

    def _picker_changed(self, names):
        if self._guard:
            return
        self._guard = True
        try:
            self.table.clearSelection()
            model = self.table.selectionModel()
            for name in names:
                idx = self.table.model().index(self.names.index(name), 0)
                model.select(idx, QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
            if names:
                self.table.scrollToItem(self.table.item(self.names.index(names[0]), 1))
        finally:
            self._guard = False

    def _table_changed(self):
        if not self._guard:
            self._guard = True
            try:
                self.picker.set_selection(self._selected_names(), emit=False)
            finally:
                self._guard = False

    def _picker_visibility(self, name, visible):
        self._guard = True
        try:
            self.table.item(self.names.index(name), self.COL_HIDE).setCheckState(
                QtCore.Qt.Unchecked if visible else QtCore.Qt.Checked)
        finally:
            self._guard = False

    def _item_changed(self, item):
        if self._guard:
            return
        row, col = item.row(), item.column()
        if col == self.COL_HIDE:
            self.picker.set_visible(self.names[row], item.checkState() != QtCore.Qt.Checked)
            return
        if col not in (self.COL_MIN, self.COL_MAX):
            return
        # Mirror the typed value into every other selected row; the typed row
        # joins the selection rather than replacing it.
        self._guard = True
        try:
            selected = self._selected_names() | {self.names[row]}
            self.picker.set_selection(selected, emit=False)
            model = self.table.selectionModel()
            model.select(self.table.model().index(row, 0),
                         QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows)
            for other in selected:
                r = self.names.index(other)
                if r != row:
                    self.table.item(r, col).setText(item.text())
        finally:
            self._guard = False

    def _save(self):
        new = {}
        for row, name in enumerate(self.names):
            lo = self.table.item(row, self.COL_MIN).text().strip()
            hi = self.table.item(row, self.COL_MAX).text().strip()
            if not lo and not hi:
                continue
            try:
                new[name] = {'operating_min_c': round(units.temp_to_c(float(lo)), 6),
                             'operating_max_c': round(units.temp_to_c(float(hi)), 6)}
            except ValueError:
                messages.warning(
                    self, 'Invalid input',
                    f'"{name}" needs both operating min and max as numbers (or leave both blank).')
                return
        self.result_overrides = new
        self.accept()
