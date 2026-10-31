"""Точка входа переносимого комплекта Windows."""

import os
from pathlib import Path
import sys

APP = Path(__file__).resolve().parent
RUNTIME = APP.parent / "runtime"
if sys.platform == "win32" and RUNTIME.exists():
    dll_directory = os.add_dll_directory(str(RUNTIME / "DLLs"))
    os.environ["TCL_LIBRARY"] = str(RUNTIME / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(RUNTIME / "tcl" / "tk8.6")
sys.dont_write_bytecode = True
sys.path.insert(0, str(APP))

from pulsar_gui import main

main()
