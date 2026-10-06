"""PyInstaller entry point (the frozen exe runs this file)."""
import sys

from engine.main import run_and_pause

sys.exit(run_and_pause())
