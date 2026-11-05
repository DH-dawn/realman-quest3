"""Run the src converter with the example's local configuration."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from common.paths import config_file
from convert.convert_dual_to_lerobot import main


if __name__ == '__main__':
    args = sys.argv[1:]
    if not any(arg in ('-h', '--help', '--config') or arg.startswith('--config=') for arg in args):
        args = ['--config', str(config_file('conversion_config.json')), *args]
    try:
        main(args)
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc))
