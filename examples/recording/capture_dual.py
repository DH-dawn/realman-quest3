"""Dual-camera-only capture; does not connect to the robot."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from recording.dual_camera_recording import main

if __name__ == '__main__':
    main()
