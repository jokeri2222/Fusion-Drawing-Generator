"""One-time setup: installs the libraries the program needs and adds a desktop
shortcut that starts it without a console window.

    python setup.py                 install libraries and create the shortcut
    python setup.py --no-shortcut   install libraries only
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

REQUIREMENTS = [
    'cadquery==2.8.0',    # STEP loading and OpenCascade geometry (hidden-line views)
    'reportlab==4.4.10',  # PDF output
    'scipy>=1.11',        # hidden-line cross-check
    'PyQt5>=5.15',        # the desktop interface
    'vtk==9.6.2',         # the 3D body picker
    'Pillow',             # writes the application icon
]


def install():
    if sys.version_info < (3, 9):
        sys.exit('Python 3.9 or newer is required.')
    print('Installing: ' + ', '.join(REQUIREMENTS))
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--upgrade'] + REQUIREMENTS)


def make_icon():
    sys.path.insert(0, os.path.join(ROOT, 'src'))
    from PyQt5 import QtWidgets
    QtWidgets.QApplication([])
    from ui import icon
    os.makedirs(os.path.join(ROOT, 'assets'), exist_ok=True)
    icon.save_files(os.path.join(ROOT, 'assets'))


def shortcut():
    if sys.platform != 'win32':
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    ico = os.path.join(ROOT, 'assets', 'icon.ico')
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        "[Environment]::GetFolderPath('Desktop')+'\Fusion Drawing Generator.lnk');"
        f"$s.TargetPath='{pythonw}';$s.Arguments='\"{os.path.join(ROOT, 'main.py')}\"';"
        f"$s.WorkingDirectory='{ROOT}';"
        + (f"$s.IconLocation='{ico}';" if os.path.exists(ico) else '') + "$s.Save()")
    subprocess.check_call(['powershell', '-NoProfile', '-Command', script])
    print('Desktop shortcut created.')


if __name__ == '__main__':
    install()
    try:
        make_icon()
    except Exception as exc:   # the icon is cosmetic
        print(f'Icon not written: {exc}')
    if '--no-shortcut' not in sys.argv:
        try:
            shortcut()
        except Exception as exc:
            print(f'Shortcut not created: {exc}')
    print('Done. Start the program with:  python main.py')
