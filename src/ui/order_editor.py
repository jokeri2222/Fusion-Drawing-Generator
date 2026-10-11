"""Visual assembly-order editor: blocks on a canvas. A part is a block; a
sub-assembly is a container block holding other blocks (nested to any
depth). Top-to-bottom is assembly order. Drag blocks to reorder them or drop
them into / out of containers; every drop re-lays the tree out.

Mouse: drag = move (the whole selection if the block is selected), click =
select, Ctrl/Shift-click = add to selection, double-click a container =
rename, click the [-]/[+] on a container to collapse it, wheel = scroll.
"""
import copy
import threading

from PyQt5 import QtCore, QtGui, QtWidgets

from . import messages
from .background import Background
from .frame import FramedDialog

import assembly_order

from . import theme
from .dialogs import hint

PART_W, PART_H = 240, 28
HEAD_H = 28
PAD, GAP, INDENT = 8, 6, 14


def _is_group(n):
    return isinstance(n, dict) and n.get('type') != 'fasten'


def _is_fasten(n):
    return isinstance(n, dict) and n.get('type') == 'fasten'


class _Canvas(QtWidgets.QWidget):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setMouseTracking(False)

    def paintEvent(self, _event):
        self.editor.paint(QtGui.QPainter(self))

    def mousePressEvent(self, e):
        self.editor.on_press(e.x(), e.y(), e.modifiers())

    def mouseMoveEvent(self, e):
        self.editor.on_motion(e.x(), e.y())

    def mouseReleaseEvent(self, e):
        self.editor.on_release(e.x(), e.y())

    def mouseDoubleClickEvent(self, e):
        self.editor.on_double(e.x(), e.y())


class OrderEditor(FramedDialog):
    def __init__(self, parent, project_dir, analyzer):
        super().__init__(parent)
        self.project_dir = project_dir
        self.analyzer = analyzer
        self.tree = copy.deepcopy(analyzer.tree)
        self.history = []
        self.selection = set()
        self.collapsed = set()
        self.rects = {}         # key -> (x0, y0, x1, y1, node, parent_list, depth)
        self.drag = None
        self.ghost = None
        self.last_clicked = None
        self.fasteners_edited = analyzer.fastener_manual
        self.show_fasteners = False
        self._warn_cache = {}
        self._bg = Background(self)
        self._lock = threading.Lock()   # the analyzer serves one background job at a time
        self._canvas_size = (400, 300)

        self.setWindowTitle('Assembly Order')
        self.resize(1040, 800)
        layout = QtWidgets.QVBoxLayout(self.body)
        layout.addWidget(hint(
            'Top to bottom is the assembly order. Drag a block onto a container to put it inside that '
            'sub-assembly (containers nest to any depth), or between blocks to reorder. "Group as assembly" wraps '
            'the selected blocks in a sub-assembly that is built on its own pages and then installed as one block. '
            'A "set" (dashed, orange) is not an assembly: it gets no pages of its own, its parts are placed '
            'together and named by the set. Fastener steps are added automatically.'))

        def bar(items):
            row = QtWidgets.QHBoxLayout()
            for label, cmd in items:
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(lambda _=False, c=cmd: c())
                row.addWidget(b)
            row.addStretch(1)
            layout.addLayout(row)
            return row

        bar((('Group as assembly', self.group_selected), ('Group as set', lambda: self.group_selected('set')),
             ('Toggle assembly/set', self.toggle_type), ('New empty group', self.new_group),
             ('Ungroup', self.ungroup), ('Rename', self.rename_selected), ('Undo', self.undo),
             ('Collapse all', lambda: self.set_all_collapsed(True)),
             ('Expand all', lambda: self.set_all_collapsed(False)),
             ('Suggest baseline', self.suggest), ('Reset to auto', self.reset)))
        row2 = bar((('Merge fastener steps', self.merge_fasteners), ('Split fastener step', self.split_fastener),
                    ('Delete selected fastener step', self.delete_fasteners),
                    ('Auto-place fasteners', self.auto_place_fasteners)))
        self.fastener_box = QtWidgets.QCheckBox('Show fasteners (screws, bolts...)')
        self.fastener_box.toggled.connect(self.toggle_fasteners)
        row2.insertWidget(0, self.fastener_box)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.canvas = _Canvas(self)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(False)
        split.addWidget(self.scroll)
        holder = QtWidgets.QWidget()
        hl = QtWidgets.QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel('Warnings')
        title.setObjectName('cardTitle')
        hl.addWidget(title)
        self.warn = QtWidgets.QPlainTextEdit()
        self.warn.setReadOnly(True)
        hl.addWidget(self.warn)
        split.addWidget(holder)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([560, 140])
        layout.addWidget(split, 1)

        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        cancel = QtWidgets.QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        save = QtWidgets.QPushButton('Save')
        save.setObjectName('primary')
        save.clicked.connect(self.save)
        foot.addWidget(cancel)
        foot.addWidget(save)
        layout.addLayout(foot)

        self._warn_timer = QtCore.QTimer(self)
        self._warn_timer.setSingleShot(True)
        self._warn_timer.timeout.connect(self.refresh_warnings)
        QtWidgets.QShortcut(QtGui.QKeySequence.Undo, self, self.undo)
        QtWidgets.QShortcut(QtGui.QKeySequence.Delete, self, self.delete_fasteners)
        self.redraw()

    # ---- model helpers ---------------------------------------------------
    def _key(self, node):
        return node if isinstance(node, str) else 'g' + str(id(node))

    def snapshot(self):
        self.history.append(copy.deepcopy(self.tree))
        del self.history[:-50]

    def undo(self):
        if self.history:
            self.tree = self.history.pop()
            self.selection.clear()
            self.redraw()

    def find_parent_list(self, key, nodes=None):
        nodes = self.tree if nodes is None else nodes
        for n in nodes:
            if self._key(n) == key:
                return nodes
            if _is_group(n):
                found = self.find_parent_list(key, n['children'])
                if found is not None:
                    return found
        return None

    def node_by_key(self, key, nodes=None):
        nodes = self.tree if nodes is None else nodes
        for n in nodes:
            if self._key(n) == key:
                return n
            if _is_group(n):
                found = self.node_by_key(key, n['children'])
                if found is not None:
                    return found
        return None

    def contains(self, group, key):
        return any(self._key(n) == key or (_is_group(n) and self.contains(n, key)) for n in group['children'])

    # ---- layout ------------------------------------------------------------
    def layout_nodes(self, nodes, x, y, depth):
        """Fill self.rects; return the y below the laid-out nodes."""
        for n in nodes:
            k = self._key(n)
            if _is_fasten(n) and not self.show_fasteners:
                continue
            if _is_group(n):
                if k in self.collapsed:
                    self.rects[k] = (x, y, x + PART_W + 2 * PAD + INDENT, y + HEAD_H, n, nodes, depth)
                    y += HEAD_H + GAP
                    continue
                inner_end = self.layout_nodes(n['children'], x + PAD + INDENT, y + HEAD_H + 2, depth + 1)
                end = max(inner_end, y + HEAD_H + 2) + PAD - GAP
                right = x + PART_W + 2 * PAD + INDENT
                for r in self.rects.values():
                    if r[4] is not n and self._descendant_of(r[4], n):
                        right = max(right, r[2] + PAD)
                self.rects[k] = (x, y, right, end, n, nodes, depth)
                y = end + GAP
            else:
                self.rects[k] = (x, y, x + PART_W, y + PART_H, n, nodes, depth)
                y += PART_H + GAP
        return y

    def _descendant_of(self, node, group):
        return any(c is node or (_is_group(c) and self._descendant_of(node, c)) for c in group['children'])

    def redraw(self, warnings=True):
        self.rects = {}
        end = self.layout_nodes(self.tree, 12, 12, 0)
        width = max([r[2] for r in self.rects.values()] + [400]) + 40
        self._canvas_size = (width, max(end + 40, 300))
        self.canvas.setMinimumSize(*self._canvas_size)
        self.canvas.resize(max(width, self.scroll.viewport().width()), max(end + 40, self.scroll.viewport().height()))
        self.canvas.update()
        if warnings:
            self._warn_timer.start(250)   # debounced: the order check runs exact geometry queries

    def refresh_warnings(self):
        order = tuple(assembly_order.flatten(self.tree))
        if order in self._warn_cache:
            self._show_warnings(self._warn_cache[order])
            return
        self.warn.setPlainText('Checking the order...')

        def work():
            with self._lock:
                return self.analyzer.check_order(list(order))

        def done(warns, error):
            if error is not None:
                self.warn.setPlainText(f'Could not check the order: {error}')
                return
            self._warn_cache[order] = warns
            if tuple(assembly_order.flatten(self.tree)) == order:   # skip results for an order since changed
                self._show_warnings(warns)

        self._bg.run(work, done)

    def _show_warnings(self, warns):
        self.warn.setPlainText('\n'.join('- ' + w for w in warns) if warns else 'No problems found.')

    # ---- painting ----------------------------------------------------------
    def paint(self, p):
        c = theme.colors()
        q = QtGui.QColor
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.canvas.rect(), q(c['canvas']))
        order = {n: i + 1 for i, n in enumerate(assembly_order.flatten(self.tree))}
        base = p.font()

        def font(size, bold=False, italic=False):
            f = QtGui.QFont(base)
            f.setPointSizeF(size)
            f.setBold(bold)
            f.setItalic(italic)
            p.setFont(f)

        p.setPen(q(c['text']))
        for k, (x0, y0, x1, y1, n, _p, depth) in sorted(self.rects.items(), key=lambda kv: kv[1][6]):
            if not _is_group(n):
                continue
            sel, is_set = k in self.selection, assembly_order.is_set(n)
            pen = QtGui.QPen(q(c['picked_line'] if sel else (c['set_line'] if is_set else c['depth_line'][depth % 5])),
                             3 if sel else 2)
            if is_set:
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(q(c['set_fill'] if is_set else c['depth_fill'][depth % 5]))
            p.drawRoundedRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0), 6, 6)
            mark = '[+]' if k in self.collapsed else '[-]'
            kind = 'set' if is_set else 'assembly'
            font(9.5, bold=True)
            p.setPen(q(c['text']))
            p.drawText(QtCore.QRectF(x0 + 8, y0, x1 - x0 - 12, HEAD_H), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                       f'{mark}  {n["name"]}  [{kind}, {len(assembly_order.flatten(n["children"]))} parts]')
        for k, (x0, y0, x1, y1, n, _p, depth) in self.rects.items():
            if _is_group(n):
                continue
            sel = k in self.selection
            fasten = _is_fasten(n)
            fill = c['picked_fill'] if sel else (c['fasten_fill'] if fasten else c['part_fill'])
            line = c['picked_line'] if sel else (c['fasten_line'] if fasten else c['part_line'])
            p.setPen(QtGui.QPen(q(line), 2 if sel else 1))
            p.setBrush(q(fill))
            p.drawRoundedRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0), 4, 4)
            font(9, italic=fasten)
            p.setPen(q(c['text']))
            text = ('⚙  ' + self._fasten_label(n)) if fasten else f'{order.get(n, "?")}.  {n}'
            p.drawText(QtCore.QRectF(x0 + 8, y0, x1 - x0 - 12, y1 - y0), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                       p.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, int(x1 - x0 - 14)))
        if self.ghost:
            g = self.ghost
            if g['target']:
                kind, ref, lx0, ly, lx1 = g['target']
                p.setPen(QtGui.QPen(q(c['ghost_line']), 3))
                if kind == 'into':
                    p.setBrush(QtCore.Qt.NoBrush)
                    r = self.rects[ref]
                    p.drawRect(QtCore.QRectF(r[0], r[1], r[2] - r[0], r[3] - r[1]))
                p.drawLine(QtCore.QPointF(lx0, ly), QtCore.QPointF(lx1, ly))
            p.setPen(QtGui.QPen(q(c['ghost_line']), 1))
            p.setBrush(q(c['ghost_fill']))
            p.drawRoundedRect(QtCore.QRectF(g['x'] + 8, g['y'] + 4, PART_W, PART_H), 4, 4)
            font(9)
            p.setPen(q(c['text']))
            p.drawText(QtCore.QRectF(g['x'] + 14, g['y'] + 4, PART_W - 10, PART_H),
                       QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, g['label'])

    # ---- hit testing -------------------------------------------------------
    def hit(self, x, y, exclude=()):
        """Innermost block under (x, y) not inside an excluded block."""
        best = None
        for k, (x0, y0, x1, y1, n, _p, depth) in self.rects.items():
            if x0 <= x <= x1 and y0 <= y <= y1 and k not in exclude:
                if any(e in exclude and _is_group(self.node_by_key(e)) and
                       self.contains(self.node_by_key(e), k) for e in exclude):
                    continue
                if best is None or depth > best[1]:
                    best = (k, depth)
        return best[0] if best else None

    # ---- mouse ---------------------------------------------------------------
    def on_press(self, x, y, mods):
        shift = bool(mods & QtCore.Qt.ShiftModifier)
        ctrl = bool(mods & QtCore.Qt.ControlModifier)
        k = self.hit(x, y)
        self.drag = None
        if k is None:
            if not (shift or ctrl):
                self.selection.clear()
                self.redraw(warnings=False)
            return
        r = self.rects[k]
        if _is_group(r[4]) and x - r[0] < 40 and y - r[1] < HEAD_H:
            self.collapsed ^= {k}
            self.redraw(warnings=False)
            return
        narrow = False
        if shift and self.last_clicked in self.rects:
            keys = [kk for kk, _r in sorted(self.rects.items(), key=lambda kv: (kv[1][1], kv[1][0]))]
            i, j = sorted((keys.index(self.last_clicked), keys.index(k)))
            rng = set(keys[i:j + 1])
            self.selection = (self.selection | rng) if ctrl else rng
        elif ctrl:
            self.selection ^= {k}
            self.last_clicked = k
        else:
            if k not in self.selection:
                self.selection = {k}
            elif len(self.selection) > 1:
                narrow = True   # a plain click inside a multi-selection narrows it (unless dragged)
            self.last_clicked = k
        self.redraw(warnings=False)
        self.drag = {'key': k, 'start': (x, y), 'moved': False, 'narrow': narrow}

    def top_selected(self):
        """Selected blocks that aren't inside another selected block."""
        out = []
        for k in self.selection:
            n = self.node_by_key(k)
            if n is None:
                continue
            if any(o != k and _is_group(self.node_by_key(o)) and self.contains(self.node_by_key(o), k)
                   for o in self.selection if self.node_by_key(o) is not None):
                continue
            out.append(k)
        return out

    def on_motion(self, x, y):
        if not self.drag:
            return
        sx, sy = self.drag['start']
        if not self.drag['moved'] and abs(x - sx) + abs(y - sy) < 6:
            return
        self.drag['moved'] = True
        movers = self.top_selected()
        if not movers:
            return
        label = self.node_by_key(movers[0])
        label = label if isinstance(label, str) else (self._fasten_label(label) if _is_fasten(label) else label['name'])
        if len(movers) > 1:
            label += f'  (+{len(movers) - 1})'
        self.ghost = {'x': x, 'y': y, 'label': label, 'target': self.drop_target(x, y, movers)}
        self.canvas.update()

    def drop_target(self, x, y, movers):
        """('before'|'after'|'into'|'end', key|None, x0, y, x1) - where a drop at (x, y) would land."""
        k = self.hit(x, y, exclude=set(movers))
        if k is None:
            for n in self.tree:
                nk = self._key(n)
                if nk in movers or nk not in self.rects:
                    continue
                r = self.rects[nk]
                if y < (r[1] + r[3]) / 2:
                    return ('before', nk, r[0], r[1] - GAP / 2, r[2])
            return ('end', None, 12, (max([r[3] for r in self.rects.values()] + [12])) + GAP / 2, 12 + PART_W)
        x0, y0, x1, y1, n, _p, _d = self.rects[k]
        if _is_group(n):
            if y < y0 + HEAD_H:
                if y < y0 + HEAD_H * 0.3:
                    return ('before', k, x0, y0 - GAP / 2, x1)
                return ('into', k, x0 + PAD, y0 + HEAD_H, x1 - PAD)
            if k in self.collapsed:
                return ('into', k, x0 + PAD, y1, x1 - PAD)
            for c in n['children']:
                ck = self._key(c)
                if ck in movers or ck not in self.rects:
                    continue
                r = self.rects[ck]
                if y < (r[1] + r[3]) / 2:
                    return ('before', ck, r[0], r[1] - GAP / 2, r[2])
            last = [c for c in n['children'] if self._key(c) in self.rects and self._key(c) not in movers]
            ly = self.rects[self._key(last[-1])][3] + GAP / 2 if last else y0 + HEAD_H + 2
            return ('into', k, x0 + PAD, ly, x1 - PAD)
        if y < (y0 + y1) / 2:
            return ('before', k, x0, y0 - GAP / 2, x1)
        return ('after', k, x0, y1 + GAP / 2, x1)

    def on_release(self, x, y):
        d, self.drag = self.drag, None
        self.ghost = None
        self.canvas.update()
        if not d:
            return
        if not d['moved']:
            if d.get('narrow'):
                self.selection = {d['key']}
                self.redraw(warnings=False)
            return
        movers = self.top_selected()
        target = self.drop_target(x, y, set(movers))
        if not target:
            return
        kind, ref = target[0], target[1]
        for m in movers:                       # never drop a group into itself
            node = self.node_by_key(m)
            if ref and _is_group(node) and (ref == m or self.contains(node, ref)):
                return
        self.snapshot()
        nodes = [self.node_by_key(m) for m in movers]
        flat_order = {self._key(n): i for i, n in enumerate(self._walk())}
        nodes.sort(key=lambda n: flat_order.get(self._key(n), 0))
        for m in movers:
            self.find_parent_list(m).remove(self.node_by_key(m))
        if kind == 'end':
            self.tree.extend(nodes)
        elif kind == 'into':
            self.node_by_key(ref)['children'].extend(nodes)
            self.collapsed.discard(ref)
        else:
            lst = self.find_parent_list(ref)
            i = next(i for i, n in enumerate(lst) if self._key(n) == ref)
            at = i if kind == 'before' else i + 1
            lst[at:at] = nodes
        self._prune_empty()
        self.redraw()

    def _walk(self, nodes=None):
        for n in (self.tree if nodes is None else nodes):
            yield n
            if _is_group(n):
                yield from self._walk(n['children'])

    def _prune_empty(self):
        def prune(nodes):
            nodes[:] = [n for n in nodes if not _is_group(n) or (prune(n['children']) or n['children'] or n.get('keep'))]
        prune(self.tree)

    def on_double(self, x, y):
        k = self.hit(x, y)
        if k and _is_group(self.node_by_key(k)):
            self.selection = {k}
            self.rename_selected()

    # ---- fastener steps ----------------------------------------------------
    def _fasten_label(self, n):
        import assembly_guide
        names = n['fasteners']
        return f'Fasten {assembly_guide._part_label(names)}  →  {", ".join(n["bodies"][:3])}' + \
               ('...' if len(n['bodies']) > 3 else '')

    def _make_fasten(self, fasteners, bodies, axis):
        n = {'type': 'fasten', 'fasteners': list(fasteners), 'bodies': list(bodies), 'axis': axis}
        n['name'] = self._fasten_label(n)
        return n

    def _all_fasten(self):
        return [n for n in self._walk() if _is_fasten(n)]

    def toggle_fasteners(self, shown):
        self.show_fasteners = shown
        if shown and not self.fasteners_edited and not self._all_fasten():
            self.snapshot()
            self._insert_auto_fasteners()
        if shown:
            self.fasteners_edited = True
        self.redraw()

    def auto_place_fasteners(self):
        self.snapshot()
        self._strip_fasteners()
        self._insert_auto_fasteners()
        self.fasteners_edited = True
        self.show_fasteners = True
        self.fastener_box.blockSignals(True)
        self.fastener_box.setChecked(True)
        self.fastener_box.blockSignals(False)
        self.redraw()

    def _strip_fasteners(self):
        def strip(nodes):
            nodes[:] = [n for n in nodes if not _is_fasten(n)]
            for n in nodes:
                if _is_group(n):
                    strip(n['children'])
        strip(self.tree)

    def _insert_auto_fasteners(self):
        """One fastener step per automatic fastener group, right after the
        last body it joins, inside the smallest block holding all of them."""
        def parts_of(n):
            return {n} if isinstance(n, str) else set(assembly_order.flatten([n]))

        def place(nodes, node):
            bodies = set(node['bodies'])
            holders = [i for i, c in enumerate(nodes) if parts_of(c) & bodies]
            if not holders:
                return False
            if len(holders) == 1 and _is_group(nodes[holders[0]]) and bodies <= parts_of(nodes[holders[0]]):
                return place(nodes[holders[0]]['children'], node)
            nodes.insert(holders[-1] + 1, node)
            return True

        for g in sorted(self.analyzer.groups, key=lambda g: min(
                (i for i, p in enumerate(assembly_order.flatten(self.tree)) if p in g['bodies']), default=0)):
            bodies = [b for b in g['bodies'] if b in self.analyzer.parts]
            if bodies:
                place(self.tree, self._make_fasten(g['fasteners'], bodies, g['axis']))

    def _selected_fasteners(self):
        return [n for n in (self.node_by_key(k) for k in self.top_selected()) if _is_fasten(n)]

    def merge_fasteners(self):
        sel = self._selected_fasteners()
        if len(sel) < 2:
            return
        self.snapshot()
        merged = self._make_fasten([f for n in sel for f in n['fasteners']],
                                   list(dict.fromkeys(b for n in sel for b in n['bodies'])), sel[0].get('axis'))
        lst = self.find_parent_list(self._key(sel[0]))
        i = next(i for i, n in enumerate(lst) if n is sel[0])
        for n in sel:
            self.find_parent_list(self._key(n)).remove(n)
        lst.insert(min(i, len(lst)), merged)
        self.selection = {self._key(merged)}
        self.redraw()

    def split_fastener(self):
        sel = [n for n in self._selected_fasteners() if len(n['fasteners']) > 1]
        if not sel:
            return
        self.snapshot()
        for n in sel:
            lst = self.find_parent_list(self._key(n))
            i = next(i for i, m in enumerate(lst) if m is n)
            lst[i:i + 1] = [self._make_fasten([f], n['bodies'], n.get('axis')) for f in n['fasteners']]
        self.selection.clear()
        self.redraw()

    def delete_fasteners(self):
        sel = self._selected_fasteners()
        if not sel:
            return
        self.snapshot()
        for n in sel:
            self.find_parent_list(self._key(n)).remove(n)
        self.selection.clear()
        self.redraw()

    # ---- group commands ----------------------------------------------------
    def toggle_type(self):
        groups = [self.node_by_key(k) for k in self.selection if _is_group(self.node_by_key(k))]
        if not groups:
            return
        self.snapshot()
        for g in groups:
            if assembly_order.is_set(g):
                g.pop('type', None)
            else:
                g['type'] = 'set'
        self.redraw()

    def _ask(self, title, label, text=''):
        value, ok = messages.get_text(self, title, label, text)
        return value.strip() if ok and value.strip() else None

    def group_selected(self, kind=None):
        movers = self.top_selected()
        if not movers:
            return
        name = self._ask('Set' if kind else 'Sub-assembly', 'Name:')
        if not name:
            return
        self.snapshot()
        nodes = [self.node_by_key(m) for m in movers]
        flat_order = {self._key(n): i for i, n in enumerate(self._walk())}
        nodes.sort(key=lambda n: flat_order.get(self._key(n), 0))
        first_list = self.find_parent_list(self._key(nodes[0]))
        idx = next(i for i, n in enumerate(first_list) if n is nodes[0])
        for n in nodes:
            self.find_parent_list(self._key(n)).remove(n)
        group = {'name': name, 'children': nodes}
        if kind == 'set':
            group['type'] = 'set'
        first_list.insert(min(idx, len(first_list)), group)
        self.selection = {self._key(group)}
        self.redraw()

    def new_group(self):
        name = self._ask('Sub-assembly', 'Name of the new empty group:')
        if not name:
            return
        self.snapshot()
        self.tree.append({'name': name, 'children': [], 'keep': True})
        self.redraw()

    def ungroup(self):
        groups = [k for k in self.top_selected() if _is_group(self.node_by_key(k))]
        if not groups:
            return
        self.snapshot()
        for k in groups:
            g = self.node_by_key(k)
            lst = self.find_parent_list(k)
            i = next(i for i, n in enumerate(lst) if n is g)
            lst[i:i + 1] = g['children']
        self.selection.clear()
        self.redraw()

    def rename_selected(self):
        for k in self.selection:
            n = self.node_by_key(k)
            if _is_group(n):
                name = self._ask('Rename', 'Name:', n['name'])
                if name:
                    self.snapshot()
                    n['name'] = name
                    self.redraw()
                return

    def set_all_collapsed(self, collapsed):
        self.collapsed = {self._key(n) for n in self._walk() if _is_group(n)} if collapsed else set()
        self.redraw()

    def suggest(self):
        self.snapshot()
        self.warn.setPlainText('Working out a suggested order...')

        def work():
            with self._lock:
                return copy.deepcopy(self.analyzer.proposed_tree())

        def done(tree, error):
            if error is not None:
                messages.warning(self, 'Could not suggest an order', str(error))
                return
            self.tree = tree
            self.fasteners_edited = False
            self.show_fasteners = False
            self.fastener_box.blockSignals(True)
            self.fastener_box.setChecked(False)
            self.fastener_box.blockSignals(False)
            self.selection.clear()
            self.redraw()

        self._bg.run(work, done)

    def reset(self):
        assembly_order.clear_manual_order(self.project_dir)
        self.suggest()

    def save(self):
        tree, missing = assembly_order.normalize_tree(self.tree, self.analyzer.parts, self.analyzer.fasteners)
        tree.extend(missing)
        assembly_order.save_tree(self.project_dir, tree, self.fasteners_edited)
        self.accept()
