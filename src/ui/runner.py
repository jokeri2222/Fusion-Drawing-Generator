"""Runs the heavy jobs (drawing generation, the assembly PDF, the assembly
analysis and the project scan) in a separate *process*, so the window never
shares the interpreter lock with them and stays fully responsive, and so
Cancel can kill the job at once instead of waiting for the next checkpoint.

The tasks are module-level functions looked up by name, because a spawned
process can only receive things it can import."""
import multiprocessing
import queue
import sys
import threading

from PyQt5 import QtCore


# ---- tasks (run in the child process) ---------------------------------------
def task_generate(report, project, output, include_tolerances, length_unit):
    import drawing_generator
    return drawing_generator.generate_all(project, output, progress=report,
                                          include_tolerances=include_tolerances, length_unit=length_unit)


def task_guide(report, project, path):
    import assembly_guide
    assembly_guide.generate_assembly_instructions_pdf(project, path)
    return path


def task_order(report, project):
    """The assembly analysis. It holds solid geometry that cannot leave its
    process, so this one task runs on a thread of the window's own process."""
    import assembly_order
    analyzer = assembly_order.OrderAnalyzer(project)
    analyzer.effective_order()
    return analyzer


def task_scan(report, project):
    import glob
    import os
    report(0.1, 'Listing the exported bodies...')
    steps = glob.glob(os.path.join(project, '*.step'))
    report(0.3, 'Reading the project data...')
    import body_inclusion
    counts = {}
    report(0.6, 'Working out what each body is used for...')
    for state in body_inclusion.load_inclusion(project).values():
        counts[state] = counts.get(state, 0) + 1
    report(1.0, 'Done')
    return {'steps': len(steps), 'counts': counts}


IN_PROCESS = {'order'}   # results that cannot be pickled
def task_mesh(report, paths, names):
    """Triangulate each STEP file and hand the arrays back one body at a time."""
    import numpy as np
    import step_loader
    for i, (path, name) in enumerate(zip(paths, names)):
        try:
            vertices, triangles = step_loader.load_step(path).triangle_mesh(deflection=0.5)
        except Exception:
            vertices = triangles = []
        if len(triangles):
            vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
            tri = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
            triangles = np.hstack([np.full((len(tri), 1), 3, dtype=np.int64), tri]).ravel()
        else:
            vertices = triangles = None
        report.send((i, name, vertices, triangles))


TASKS = {'mesh': task_mesh, 'generate': task_generate, 'guide': task_guide, 'order': task_order, 'scan': task_scan}


class _Relay:
    """File-like stand-in for stdout/stderr in the child: forwards text to the parent."""

    def __init__(self, q):
        self._q = q

    def write(self, text):
        if text:
            self._q.put(('log', text))
        return len(text or '')

    def flush(self):
        pass

    def isatty(self):
        return False


def _child(task, args, q):
    import traceback
    sys.stdout = sys.stderr = _Relay(q)

    def report(fraction, message=''):
        q.put(('progress', float(fraction), message))
    report.send = lambda payload: q.put(('data', payload))

    try:
        result = TASKS[task](report, *args)
        q.put(('done', result))
    except BaseException as exc:   # shown to the user, not swallowed
        q.put(('error', str(exc), traceback.format_exc()))


class _ThreadProc:
    """Process-like wrapper so a Job can drive an in-process thread the same way."""

    def __init__(self, task, args, q):
        self._thread = threading.Thread(target=self._run, args=(task, args, q), daemon=True)

    @staticmethod
    def _run(task, args, q):
        import traceback

        def report(fraction, message=''):
            q.put(('progress', float(fraction), message))
        try:
            q.put(('done', TASKS[task](report, *args)))
        except BaseException as exc:
            q.put(('error', str(exc), traceback.format_exc()))

    def start(self):
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def terminate(self):
        pass   # a thread cannot be killed; its result is simply dropped

    def join(self, timeout=None):
        pass


# ---- parent side --------------------------------------------------------------
class Job(QtCore.QObject):
    """One background job. Signals mirror what the window needs; `cancel()`
    stops it immediately."""
    progress = QtCore.pyqtSignal(float, str)
    log = QtCore.pyqtSignal(str)
    finished_ok = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    cancelled = QtCore.pyqtSignal()
    data = QtCore.pyqtSignal(object)

    def __init__(self, task, args=(), parent=None):
        super().__init__(parent)
        self._threaded = task in IN_PROCESS
        if self._threaded:
            self._queue = queue.Queue()
            self._proc = _ThreadProc(task, tuple(args), self._queue)
        else:
            ctx = multiprocessing.get_context('spawn')
            self._queue = ctx.Queue()
            self._proc = ctx.Process(target=_child, args=(task, tuple(args), self._queue), daemon=True)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)
        self._over = False

    def start(self):
        self._proc.start()
        self._timer.start()

    def cancel(self):
        if self._over:
            return
        self._over = True
        self._timer.stop()
        if self._proc.is_alive():
            self._proc.terminate()
        self._proc.join(2)
        self.cancelled.emit()

    def kill(self):
        """Stop quietly (window closing)."""
        self._over = True
        self._timer.stop()
        if self._proc.is_alive():
            self._proc.terminate()

    def _poll(self):
        if self._over:
            return
        alive = self._proc.is_alive()   # sampled first: anything it sent before exiting is already queued
        for _ in range(200):   # keep each tick short so the window stays smooth
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == 'progress':
                self.progress.emit(message[1], message[2])
            elif kind == 'data':
                self.data.emit(message[1])
            elif kind == 'log':
                self.log.emit(message[1])
            elif kind == 'done':
                self._finish()
                self.finished_ok.emit(message[1])
                return
            elif kind == 'error':
                self._finish()
                self.log.emit(message[2])
                self.failed.emit(message[1])
                return
        if not alive and self._queue.empty():
            self._finish()
            self.failed.emit('The background process stopped unexpectedly.')

    def _finish(self):
        self._over = True
        self._timer.stop()
        self._proc.join(2)
