"""Live cloud policy preview by default; --execute enables physical motion."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from inference.run_policy import main

if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        raise SystemExit(str(exc))
