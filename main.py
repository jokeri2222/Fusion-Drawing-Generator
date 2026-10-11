"""Fusion Drawing Generator - start the program with:  python main.py   (or pythonw main.py for no console).

First time on a computer? Run  python setup.py  once to install what it needs."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'src'))   # the program's modules live in src/

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()   # the background jobs start extra processes from this file
    from gui import main
    main()
