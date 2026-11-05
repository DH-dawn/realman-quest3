"""Allow examples to run directly from a clone or PyCharm without installation."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = str(ROOT/'src')
sys.path.insert(0, SOURCE)
os.environ.setdefault('REALMAN_PROJECT_ROOT', str(ROOT))
os.environ['PYTHONPATH'] = SOURCE + (os.pathsep+os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')
