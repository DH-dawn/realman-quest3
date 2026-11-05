"""CLI: doctor, init-config, monitor, calibrate, preview, run and record."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from teleop.cli import entry_point

if __name__ == '__main__':
    raise SystemExit(entry_point())
