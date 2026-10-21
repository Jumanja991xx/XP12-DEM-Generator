"""PyInstaller entry point for the windowed GUI executable."""

import multiprocessing
import sys

from xp12dem.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
