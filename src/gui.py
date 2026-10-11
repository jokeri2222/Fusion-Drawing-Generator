"""
Desktop GUI for the drawing generator: pick the exported Fusion project
folder, set tolerances and temperatures, edit the per-project settings and
generate every drawing and the assembly instructions without the command line.

Started from main.py in the main folder.

Needs PyQt5 (the interface) and, for the 3D body picker, VTK.
"""
import os
import sys
import traceback


def _install_error_hook():
    """Under pythonw there is no console, so show unexpected errors in a dialog
    and keep a copy next to the program."""
    def hook(kind, value, tb):
        text = ''.join(traceback.format_exception(kind, value, tb))
        try:
            with open(os.path.join(__import__('paths').app_folder(), 'gui_error.log'), 'a',
                      encoding='utf-8') as f:
                f.write(text + '\n')
        except OSError:
            pass
        try:
            from PyQt5 import QtWidgets
            QtWidgets.QMessageBox.critical(None, 'Unexpected error', text[-1500:])
        except Exception:
            pass
    sys.excepthook = hook


def main():
    from PyQt5 import QtCore, QtWidgets

    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    if sys.platform == 'win32':
        try:   # own taskbar identity, so the window shows this icon rather than python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FusionDrawingGenerator.App')
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName('Fusion Drawing Generator')
    _install_error_hook()

    from ui import icon, theme
    from ui.main_window import MainWindow

    settings = QtCore.QSettings('FusionDrawingGenerator', 'DrawingGenerator')
    from ui import settings_dialog
    settings_dialog.load_units()
    theme.apply(app, settings.value('theme', 'system', str))
    app.setWindowIcon(icon.app_icon())
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
