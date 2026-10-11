"""Where the program's files are, whether it runs from source or as one
compiled file (which unpacks its bundled files into a temporary folder)."""
import os
import shutil
import sys

FROZEN = bool(getattr(sys, 'frozen', False))
_SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_folder():
    """The folder the program lives in (the one with main.py, or the compiled file)."""
    return os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else _SOURCE_ROOT


def resource(*parts):
    """A bundled read-only file such as assets/donate_qr.svg."""
    base = getattr(sys, '_MEIPASS', None) if FROZEN else None
    return os.path.join(base or _SOURCE_ROOT, *parts)


def export_script_folder():
    """The Fusion export script folder. A compiled program copies it out of its
    bundle next to itself the first time, so it can be added to Fusion."""
    target = os.path.join(app_folder(), 'scripts', 'Export Project For Drawings')
    if FROZEN and not os.path.isdir(target):
        try:
            shutil.copytree(resource('scripts', 'Export Project For Drawings'), target)
        except OSError:
            return resource('scripts', 'Export Project For Drawings')
    return target
