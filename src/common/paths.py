"""Separate installed source from writable configuration and run outputs."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

SOURCE_DIR = Path(__file__).resolve().parents[1]
DEFAULTS_DIR = Path(__file__).resolve().parent/'defaults'


def project_root():
    value = os.environ.get('REALMAN_PROJECT_ROOT')
    if value:
        return Path(value).expanduser().resolve()
    checkout = SOURCE_DIR.parent
    if (checkout/'pyproject.toml').is_file() and (checkout/'src/common/paths.py').is_file():
        return checkout
    return Path.cwd().resolve()


ROOT = project_root()

CONFIG_LAYOUT = {
    'realman_teleop.json': 'teleop',
    'realman_follow.json': 'teleop',
    'camera_mount.json': 'teleop',
    'devices.json': 'teleop',
    'cameras_dual.json': 'recording',
    'realman_recording_initial.json': 'recording',
    'infer.json': 'inference',
    'conversion_config.json': 'convert',
}


def config_file(name):
    """Each configuration has one owner; recording/inference share robot profiles."""
    return ROOT/'examples'/CONFIG_LAYOUT[name]/'config'/name


def initialize_configs():
    """Copy packaged examples only when missing; never replace local calibration."""
    copied = []
    for source in sorted(DEFAULTS_DIR.glob('*.json')):
        destination = config_file(source.name)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied.append(str(destination))
    return copied


def module_command(name):
    return [sys.executable, '-u', '-m', name]


def module_environment():
    """Make subprocess and camera workers import this same source tree."""
    env = os.environ.copy()
    source_root = str(SOURCE_DIR)
    env['PYTHONPATH'] = source_root + (os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    env['REALMAN_PROJECT_ROOT'] = str(ROOT)
    return env
