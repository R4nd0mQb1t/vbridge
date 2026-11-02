"""Frozen-exe entry point (PyInstaller). Keeps multiprocessing children from re-running the app."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from bridge.__main__ import main
    sys.exit(main())
