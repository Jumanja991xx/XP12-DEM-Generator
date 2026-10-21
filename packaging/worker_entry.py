"""PyInstaller entry point for the console worker that runs Ortho4XP."""

import sys

from xp12dem.o4x_worker import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
