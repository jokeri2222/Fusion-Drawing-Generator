"""Clickable 3D view of every body in a project, used to pick bodies visually.

Rendered by VTK on an off-screen window (the GPU does the work; only the
finished frame is copied into this widget), so it needs no native VTK/Qt
bridge. Mouse: left click picks a body, left drag rubber-bands a box, right
drag orbits, middle drag pans, wheel zooms. Ctrl/Shift+click adds to the
selection, V hides/shows the selection (or everything if nothing is picked).
"""
import math

from PyQt5 import QtCore, QtGui, QtWidgets

NORMAL_COLOR = (0.24, 0.43, 0.65)
SELECTED_COLOR = (0.18, 0.49, 0.96)
NORMAL_OPACITY = 0.45
SELECTED_OPACITY = 0.85

# Same direction tuples as drawing_generator.VIEW_DIRECTIONS (and its ISO
# direction), so a preset shows the same side of the part as the drawings do.
VIEW_PRESETS = {
    'ISO': ((1, -1, 1), (0, 0, 1)),
    'Front': ((0, -1, 0), (0, 0, 1)),
    'Top': ((0, 0, 1), (0, 1, 0)),
    'Right': ((1, 0, 0), (0, 0, 1)),
}


def _hex_to_rgb(color):
    color = color.lstrip('#')
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4))


class BodyPicker3D(QtWidgets.QWidget):
    selectionChanged = QtCore.pyqtSignal(list)        # names, in no particular order
    visibilityChanged = QtCore.pyqtSignal(str, bool)  # name, visible

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMinimumSize(280, 280)
        self.setMouseTracking(False)
        self._vtk = None
        self._actors = {}
        self._colors = {}
        self._selected = set()
        self._image = None
        self._drag = None
        self._render_pending = False
        self._background = (0.93, 0.93, 0.95)
        self._error = None
        self._normal, self._selected_color, self._box = NORMAL_COLOR, SELECTED_COLOR, QtGui.QColor('#2a5db0')

    def set_colors(self, normal, selected, box):
        self._normal, self._selected_color = _hex_to_rgb(normal), _hex_to_rgb(selected)
        self._box = QtGui.QColor(box)

    # ---- setup -----------------------------------------------------------
    def load_bodies(self, step_paths, names, default_colors=None, progress=None):
        """Triangulate each STEP file into the scene. `progress(i, n, name)`
        is called per body. Returns False (and shows why) if VTK is missing."""
        try:
            import vtk
        except ImportError:
            self._error = 'The 3D view needs VTK (pip install vtk). Use the table instead.'
            self.update()
            return False
        import step_loader
        self._vtk = vtk
        self._colors = {k: _hex_to_rgb(v) for k, v in (default_colors or {}).items()}
        self._renderer = vtk.vtkRenderer()
        self._renderer.SetBackground(*self._background)
        self._window = vtk.vtkRenderWindow()
        self._window.SetOffScreenRendering(1)
        self._window.AddRenderer(self._renderer)
        self._window.SetSize(max(self.width(), 64), max(self.height(), 64))
        self._pending = None
        self._finish_setup()
        return True

    def load_bodies_async(self, step_paths, names, default_colors=None, progress=None, done=None):
        """Like load_bodies, but the STEP files are meshed in a separate process
        and each body appears as soon as its mesh arrives. `progress(i, n,
        name)` and `done()` run on the UI thread."""
        from .runner import Job
        if not self.load_bodies([], [], default_colors):
            if done:
                done()
            return
        self._progress, self._done, self._total = progress, done, len(names)
        job = self._job = Job('mesh', (list(step_paths), list(names)), self)
        job.data.connect(lambda payload: self._on_mesh(job, *payload))
        job.finished_ok.connect(lambda _r: self._on_meshed(job))
        job.failed.connect(lambda _m: self._on_meshed(job))
        job.start()

    def stop_loading(self):
        job = getattr(self, '_job', None)
        if job is not None:
            job.kill()
            self._job = None

    def _on_meshed(self, job):
        if job is not getattr(self, '_job', None):
            return
        self._job = None
        self._renderer.ResetCamera()
        self.set_view('ISO')
        if self._done:
            self._done()

    def _on_mesh(self, job, index, name, vertices, triangles):
        if job is not getattr(self, '_job', None):
            return
        if self._progress:
            self._progress(index, self._total, name)
        if triangles is not None:
            self._add_body(name, vertices, triangles)
            self._schedule_render()

    def _add_body(self, name, vertices, triangles):
        vtk = self._vtk
        from vtk.util import numpy_support
        points = vtk.vtkPoints()
        points.SetData(numpy_support.numpy_to_vtk(vertices, deep=True))
        cells = vtk.vtkCellArray()
        cells.ImportLegacyFormat(numpy_support.numpy_to_vtkIdTypeArray(triangles, deep=True))
        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(poly)
        normals.ComputePointNormalsOn()
        normals.Update()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*self._colors.get(name, self._normal))
        actor.GetProperty().SetOpacity(NORMAL_OPACITY)
        self._renderer.AddActor(actor)
        self._actors[name] = actor

    def _finish_setup(self):
        vtk = self._vtk
        self._picker = vtk.vtkCellPicker()
        self._picker.SetTolerance(0.005)
        self._area_picker = vtk.vtkAreaPicker()
        self._renderer.ResetCamera()
        self._renderer.GetActiveCamera().SetParallelProjection(1)
        self.set_view('ISO')
        return True

    def set_background(self, hex_color):
        self._background = _hex_to_rgb(hex_color)
        if self._vtk:
            self._renderer.SetBackground(*self._background)
            self._schedule_render()

    # ---- state -----------------------------------------------------------
    def names(self):
        return list(self._actors)

    def selection(self):
        return set(self._selected)

    def set_selection(self, names, emit=True):
        names = {n for n in names if n in self._actors}
        if names == self._selected:
            return
        changed = self._selected | names
        self._selected = names
        for name in changed:
            self._apply_look(name)
        self._schedule_render()
        if emit:
            self.selectionChanged.emit(sorted(self._selected))

    def set_visible(self, name, visible):
        if name in self._actors:
            self._actors[name].SetVisibility(bool(visible))
            self._schedule_render()

    def _apply_look(self, name):
        prop = self._actors[name].GetProperty()
        selected = name in self._selected
        prop.SetColor(*(self._selected_color if selected else self._colors.get(name, self._normal)))
        prop.SetOpacity(SELECTED_OPACITY if selected else NORMAL_OPACITY)

    # ---- camera ----------------------------------------------------------
    def set_view(self, preset):
        if not self._vtk or not self._actors:
            return
        direction, up = VIEW_PRESETS[preset]
        renderer, camera = self._renderer, self._renderer.GetActiveCamera()
        b = renderer.ComputeVisiblePropBounds()
        cx, cy, cz = (b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2
        diag = math.sqrt((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) or 100.0
        dx, dy, dz = direction
        norm = math.sqrt(dx * dx + dy * dy + dz * dz)
        dist = diag * 1.5
        camera.SetFocalPoint(cx, cy, cz)
        camera.SetPosition(cx + dx / norm * dist, cy + dy / norm * dist, cz + dz / norm * dist)
        camera.SetViewUp(*up)
        renderer.ResetCameraClippingRange()
        camera.SetParallelScale(diag * 0.6)
        self._schedule_render()

    # ---- rendering -------------------------------------------------------
    def _schedule_render(self):
        if not self._render_pending:
            self._render_pending = True
            QtCore.QTimer.singleShot(0, self._render)

    def _render(self):
        self._render_pending = False
        if not self._vtk:
            return
        from vtkmodules.util.numpy_support import vtk_to_numpy
        w, h = max(self.width(), 64), max(self.height(), 64)
        self._window.SetSize(w, h)
        self._window.Render()
        grab = self._vtk.vtkWindowToImageFilter()
        grab.SetInput(self._window)
        grab.SetInputBufferTypeToRGB()
        grab.Update()
        data = grab.GetOutput()
        iw, ih, _ = data.GetDimensions()
        arr = vtk_to_numpy(data.GetPointData().GetScalars()).reshape(ih, iw, 3)[::-1]
        arr = arr.copy()
        self._image = QtGui.QImage(arr.data, iw, ih, iw * 3, QtGui.QImage.Format_RGB888).copy()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_render()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        if self._image is not None:
            p.drawImage(0, 0, self._image)
        else:
            p.fillRect(self.rect(), QtGui.QColor.fromRgbF(*self._background))
            if self._error:
                p.setPen(self.palette().color(QtGui.QPalette.Text))
                p.drawText(self.rect().adjusted(16, 16, -16, -16), QtCore.Qt.TextWordWrap | QtCore.Qt.AlignCenter,
                           self._error)
        if self._drag and self._drag['button'] == QtCore.Qt.LeftButton and self._drag['moved']:
            r = QtCore.QRect(self._drag['start'], self._drag['pos']).normalized()
            p.setPen(QtGui.QPen(self._box, 1, QtCore.Qt.DashLine))
            fill = QtGui.QColor(self._box)
            fill.setAlpha(40)
            p.setBrush(fill)
            p.drawRect(r)
        p.setPen(QtGui.QColor(128, 128, 128, 200))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    # ---- mouse / keyboard --------------------------------------------------
    def mousePressEvent(self, event):
        if not self._vtk:
            return
        self.setFocus()
        self._drag = {'button': event.button(), 'start': event.pos(), 'pos': event.pos(), 'last': event.pos(),
                      'moved': False}

    def mouseMoveEvent(self, event):
        d = self._drag
        if not d or not self._vtk:
            return
        d['pos'] = event.pos()
        delta = event.pos() - d['last']
        if (event.pos() - d['start']).manhattanLength() > 3:
            d['moved'] = True
        camera = self._renderer.GetActiveCamera()
        if d['button'] == QtCore.Qt.RightButton:
            camera.Azimuth(-delta.x() * 0.5)
            camera.Elevation(delta.y() * 0.5)
            camera.OrthogonalizeViewUp()
            self._renderer.ResetCameraClippingRange()
            self._schedule_render()
        elif d['button'] == QtCore.Qt.MiddleButton:
            self._pan(delta.x(), delta.y())
        else:
            self.update()
        d['last'] = event.pos()

    def mouseReleaseEvent(self, event):
        d, self._drag = self._drag, None
        if not d or not self._vtk or d['button'] != QtCore.Qt.LeftButton:
            self.update()
            return
        h = max(self.height(), 64)
        additive = bool(event.modifiers() & (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier))
        if not d['moved']:
            self._picker.Pick(event.x(), h - event.y(), 0, self._renderer)
            actor = self._picker.GetActor()
            name = next((n for n, a in self._actors.items() if a is actor), None)
            hit = {name} if name else set()
            if additive and name:
                hit = self._selected ^ {name}
            elif additive:
                hit = set(self._selected)
        else:
            x0, x1 = sorted((d['start'].x(), event.x()))
            y0, y1 = sorted((h - d['start'].y(), h - event.y()))
            self._area_picker.AreaPick(x0, y0, x1, y1, self._renderer)
            picked = set(self._area_picker.GetProp3Ds())
            hit = {n for n, a in self._actors.items() if a in picked}
            if additive:
                hit |= self._selected
        self.set_selection(hit)
        self.update()

    def _pan(self, dx, dy):
        r, camera = self._renderer, self._renderer.GetActiveCamera()
        w, h = max(self.width(), 64), max(self.height(), 64)
        r.SetDisplayPoint(w / 2, h / 2, 0)
        r.DisplayToWorld()
        a = r.GetWorldPoint()
        r.SetDisplayPoint(w / 2 - dx, h / 2 + dy, 0)
        r.DisplayToWorld()
        b = r.GetWorldPoint()
        if b[3] and a[3]:
            shift = [b[i] / b[3] - a[i] / a[3] for i in range(3)]
            camera.SetFocalPoint(*[f + s for f, s in zip(camera.GetFocalPoint(), shift)])
            camera.SetPosition(*[q + s for q, s in zip(camera.GetPosition(), shift)])
            self._schedule_render()

    def wheelEvent(self, event):
        if not self._vtk:
            return
        factor = 1.1 if event.angleDelta().y() > 0 else 1 / 1.1
        camera = self._renderer.GetActiveCamera()
        camera.SetParallelScale(camera.GetParallelScale() / factor)
        self._renderer.ResetCameraClippingRange()
        self._schedule_render()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_V and self._actors:
            for name in (self._selected or set(self._actors)):
                visible = not self._actors[name].GetVisibility()
                self._actors[name].SetVisibility(visible)
                self.visibilityChanged.emit(name, bool(visible))
            self._schedule_render()
        else:
            super().keyPressEvent(event)
