"""Record Quest-operated dual-D405 demonstrations; requires --task."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from teleop.native_launcher import main

if __name__ == '__main__':
    try:
        raise SystemExit(main(['--follow', '--direct', '--record', '--dual-cameras', *sys.argv[1:]]))
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc))
