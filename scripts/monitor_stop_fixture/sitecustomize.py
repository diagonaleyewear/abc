"""Opt-in, offline-only Docker PID1 fixture; never on the normal import path."""
import atexit
import os
from pathlib import Path

from scripts.smoke_luxoptica_stop import install_synthetic


cleanup = install_synthetic(Path(os.environ["MONITOR_SMOKE_DIRECTORY"]), os.environ["MONITOR_SMOKE_SCENARIO"])
atexit.register(cleanup)
