"""Separate dual-camera recording entry; original teleop controller is reused."""
from common.paths import config_file
import argparse
import asyncio
from datetime import datetime
from functools import partial
from pathlib import Path

from .dual_camera_recording import DualCameraConfig, DualCameraSession, align_dual_episode
from .episode_session import EpisodeSession
from teleop.realman_follow import FollowConfig, run_follow
from teleop.realman_teleop import Config, ROOT


class DualEpisodeSession(EpisodeSession):
    def __init__(self, *args, camera_config, **kwargs):
        super().__init__(*args, **kwargs,
            camera_factory=partial(DualCameraSession, camera_config=camera_config),
            aligner=partial(align_dual_episode, camera_config=camera_config))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=config_file('realman_follow.json'))
    p.add_argument('--robot-config', type=Path, default=config_file('realman_teleop.json'))
    p.add_argument('--camera-config', type=Path, default=config_file('cameras_dual.json'))
    p.add_argument('--direct', action='store_true')
    p.add_argument('--record', action='store_true', required=True)
    p.add_argument('--task', required=True)
    p.add_argument('--seconds', type=float)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    follow, robot = FollowConfig.load(a.config), Config.load(a.robot_config)
    cameras = DualCameraConfig.load(a.camera_config)
    if follow.direction_calibration is None:
        p.error('Direction is not calibrated; run python examples/teleop/teleop.py calibrate before dual-camera teleoperation')
    if cameras.cameras['wrist'] != follow.camera_serial:
        p.error('Wrist serial must match the existing tool/direction calibration camera')
    if not a.task.strip():
        p.error('task must not be empty')
    if a.seconds is not None and not 0 < a.seconds <= follow.max_duration_s:
        p.error('seconds exceeds configured duration')
    output = a.output or ROOT/'run'/datetime.now().strftime('follow_dual_%Y%m%dT%H%M%S_%f')
    asyncio.run(run_follow(robot, follow, output.resolve(), direct=a.direct, seconds=a.seconds,
        record=True, task=a.task, session_factory=partial(DualEpisodeSession, camera_config=cameras)))


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        raise SystemExit(str(exc))
