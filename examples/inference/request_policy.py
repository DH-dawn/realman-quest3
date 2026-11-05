"""Request cloud predictions from a saved NPZ; never controls devices."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from inference.client import main

if __name__ == '__main__':
    main()
