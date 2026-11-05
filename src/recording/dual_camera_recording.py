"""Independent two-D405 recorder and reusable A/B episode camera backend.

Cross-camera matching uses host monotonic receipt times, never unrelated device
clocks. This module has no robot-control calls. Camera-only mode records images
without robot state/actions; record_dual.sh integrates the original teleoperator.
"""
from __future__ import annotations
from common.paths import config_file
import argparse
import asyncio
import bisect
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time

from .episode_recording import RGBDPairFilter, frame_info, load_rows, write_json
from common.paths import module_command, module_environment

from common.paths import ROOT
VIEWS = ('wrist', 'front')


@dataclass(frozen=True)
class DualCameraConfig:
    cameras: dict
    width: int = 640
    height: int = 480
    capture_fps: int = 30
    record_fps: int = 10
    max_camera_skew_s: float = .04
    max_robot_age_s: float = .1
    feedback_timeout_s: float = 1.

    def validate(self):
        if not isinstance(self.cameras, dict) or set(self.cameras) != set(VIEWS):
            raise ValueError('cameras must name wrist and front explicitly')
        if any(not isinstance(s, str) or not re.fullmatch(r'\d+', s) for s in self.cameras.values()):
            raise ValueError('Camera serials must be digit strings')
        if len(set(self.cameras.values())) != 2:
            raise ValueError('Wrist and front must use different serials')
        if (self.width, self.height, self.capture_fps) != (640, 480, 30):
            raise ValueError('Supported D405 profile is 640x480@30')
        if type(self.record_fps) is not int or not 1 <= self.record_fps <= self.capture_fps:
            raise ValueError('record_fps must be integer 1..30')
        for name, maximum in (('max_camera_skew_s', .1), ('max_robot_age_s', .2), ('feedback_timeout_s', 5.)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f'Invalid {name}')
        return self

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text())).validate()


def episode_name(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'episode_\d{6,}', value)):
        raise ValueError('Invalid episode identifier')
    return value


class DualImageWriter:
    """One camera/process owns its PNGs. Shared episode-time buckets avoid drift."""
    def __init__(self, directory, calibration, config, view, started):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory/'rgb').mkdir()
        (self.directory/'depth_raw').mkdir()
        write_json(self.directory/'calibration.json', calibration)
        self.rows = (self.directory/'frames.jsonl').open('x')
        self.config, self.view, self.started = config, view, started
        self.count, self.bucket, self.closed = 0, -1, False

    def write(self, color, depth, c, d, wait_start, received, utc):
        import cv2
        import numpy as np
        bucket = math.floor((received-self.started)*self.config.record_fps)
        if bucket < 0 or bucket <= self.bucket:
            return
        prefix = f'cameras/{self.view}'
        rgb, dep = f'rgb/{self.count:06d}.png', f'depth_raw/{self.count:06d}.png'
        for path, frame in ((rgb, color), (dep, depth)):
            if not cv2.imwrite(str(self.directory/path), np.asanyarray(frame.get_data())):
                raise OSError(f'Image write failed: {self.directory/path}')
        row = dict(index=self.count, sample_bucket=bucket, view=self.view,
                   serial=self.config.cameras[self.view], host_wait_started=wait_start,
                   host_received=received, utc=utc, color=c, depth=d,
                   rgb_path=f'{prefix}/{rgb}', depth_path=f'{prefix}/{dep}')
        self.rows.write(json.dumps(row, allow_nan=False)+'\n')
        self.rows.flush()
        self.bucket, self.count = bucket, self.count+1

    def close(self, error=None):
        if not self.closed:
            self.rows.close()
            write_json(self.directory/'report.json', dict(saved=self.count, complete=error is None, error=error))
            self.closed = True


def camera_worker(job_path):
    import pyrealsense2 as rs
    job = json.loads(Path(job_path).read_text())
    config = DualCameraConfig(**job['camera_config']).validate()
    view, serial = job['view'], config.cameras[job['view']]
    root, control = Path(job['directory']), Path(job['control'])
    runtime = Path(job_path).parent
    lock = (ROOT/f'.realsense-{serial}.lock').open('a+')
    pipeline = writer = None
    started = stopped = False
    error, active_id, revision = None, None, 0
    saved = rejected_duplicates = 0
    numbers = None
    pair_filter = RGBDPairFilter()
    rejected = (runtime/'rejected_pairs.jsonl').open('x')
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        context = rs.context()
        found = {d.get_info(rs.camera_info.serial_number): d for d in context.query_devices()}
        if serial not in found:
            raise RuntimeError(f'{view} D405 {serial} not found; available: {list(found)}')
        if 'D405' not in found[serial].get_info(rs.camera_info.name):
            raise RuntimeError(f'{view} serial {serial} is not a D405')
        pipeline, settings = rs.pipeline(context), rs.config()
        settings.enable_device(serial)
        for kind, fmt in ((rs.stream.color, rs.format.bgr8), (rs.stream.depth, rs.format.z16)):
            settings.enable_stream(kind, config.width, config.height, fmt, config.capture_fps)
        try:
            profile = pipeline.start(settings)
        except RuntimeError as exc:
            raise RuntimeError(f'{view} D405 {serial} could not start. Close RealSense Viewer/other recorders: {exc}') from exc
        started = True
        if profile.get_device().get_info(rs.camera_info.serial_number) != serial:
            raise RuntimeError('Camera role/serial mismatch')
        for _ in range(30):
            if stopped:
                return
            pipeline.wait_for_frames(2000)
        calibration = None
        while not stopped:
            if os.getppid() != job['owner_pid']:
                raise RuntimeError('Recorder owner exited')
            wait_start = time.monotonic()
            frames = pipeline.wait_for_frames(1000)
            received = time.monotonic()
            utc = datetime.now(timezone.utc).isoformat()
            color, depth = frames.get_color_frame(), frames.get_depth_frame()
            if not color or not depth:
                raise RuntimeError('Incomplete RGBD frameset')
            c, d = frame_info(color), frame_info(depth)
            current = (c['frame_number'], d['frame_number'])
            if numbers is not None and any(n <= old for n, old in zip(current, numbers)):
                rejected_duplicates += 1
                continue  # A duplicate never refreshes the heartbeat.
            numbers = current
            try:
                valid = pair_filter.accept(c, d, received)
            finally:
                if pair_filter.last_rejection and pair_filter.last_rejection['host_received'] == received:
                    rejected.write(json.dumps(pair_filter.last_rejection)+'\n'); rejected.flush()
            if not valid:
                continue
            if calibration is None:
                device = profile.get_device()
                ex = depth.profile.get_extrinsics_to(color.profile)
                calibration = dict(view=view, serial=serial, camera_name=device.get_info(rs.camera_info.name),
                    depth_scale_m_per_unit=device.first_depth_sensor().get_depth_scale(), color=c, depth=d,
                    depth_to_color=dict(rotation_column_major=list(ex.rotation), translation_m=list(ex.translation)),
                    rgb_storage='PNG decoded RGB; OpenCV writes BGR input',
                    depth_storage='uint16 PNG; raw depth optical frame, NOT aligned to RGB',
                    robot_or_other_camera_extrinsics=None,
                    timestamp_reference='host monotonic receipt; device timestamps preserved separately')
                write_json(runtime/'calibration.json', calibration)
            state = json.loads(control.read_text())
            identifier = episode_name(state['episode_id'])
            if identifier != active_id:
                if writer:
                    saved += writer.count; writer.close(); writer = None
                if identifier is not None:
                    writer = DualImageWriter(root/'episodes'/identifier/'cameras'/view,
                                             calibration, config, view, state['started_monotonic'])
                active_id = identifier
            revision = state['revision']
            if writer:
                writer.write(color, depth, c, d, wait_start, received, utc)
            write_json(runtime/'heartbeat.json', dict(monotonic=received, revision=revision, episode_id=active_id,
                       serial=serial, view=view, saved=saved+(writer.count if writer else 0),
                       rejected_pairs=pair_filter.rejected, rejected_duplicates=rejected_duplicates))
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if started:
            try:
                pipeline.stop()
            except Exception as exc:
                error = error or f'Pipeline cleanup failed: {exc}'
        if writer:
            saved += writer.count
            writer.close(error)
        rejected.close()
        write_json(runtime/'report.json', dict(view=view, serial=serial, saved=saved, complete=error is None,
                   error=error, rejected_duplicates=rejected_duplicates, **pair_filter.summary()))
        lock.close()


class DualCameraSession:
    """Two isolated RGBD processes; no camera I/O in the motion event loop."""
    def __init__(self, directory, config, task, segmented=True, *, camera_config):
        self.directory, self.config = Path(directory), camera_config.validate()
        self.control = self.directory/'camera_runtime'
        self.jobs, self.processes, self.logs = {}, {}, {}
        self.revision, self.closed_report = 0, None

    async def start(self):
        self.control.mkdir()
        write_json(self.control/'state.json', dict(revision=0, episode_id=None,
                   boundary_monotonic=time.monotonic(), started_monotonic=None))
        write_json(self.directory/'camera_config.json', asdict(self.config))
        for view in VIEWS:
            folder = self.control/view; folder.mkdir()
            job = folder/'job.json'
            write_json(job, dict(directory=str(self.directory.resolve()), control=str((self.control/'state.json').resolve()),
                       camera_config=asdict(self.config), owner_pid=os.getpid(), view=view))
            self.jobs[view] = folder
            self.logs[view] = (folder/'worker.log').open('w')
            self.processes[view] = await asyncio.create_subprocess_exec(*module_command('recording.dual_camera_recording'),
                '--worker', str(job.resolve()), stdout=self.logs[view], stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,env=module_environment())
        deadline = time.monotonic()+15.
        while time.monotonic() < deadline:
            for view, process in self.processes.items():
                if process.returncode is not None:
                    raise RuntimeError(f'{view} camera startup failed; see {self.jobs[view]/"worker.log"}')
            if all((p/'heartbeat.json').is_file() for p in self.jobs.values()):
                self.check()
                print('双 D405 已就绪：wrist=腕部，front=桌面。', flush=True)
                return
            await asyncio.sleep(.05)
        raise RuntimeError('Dual camera startup timed out; check USB3 and close RealSense Viewer')

    def check(self):
        if self.closed_report is not None or len(self.processes) != 2:
            raise RuntimeError('Dual camera session is not active')
        for view, process in self.processes.items():
            if process.returncode is not None:
                raise RuntimeError(f'{view} camera stopped; see {self.jobs[view]/"worker.log"}')
            data = json.loads((self.jobs[view]/'heartbeat.json').read_text())
            age = time.monotonic()-data['monotonic']
            if not 0 <= age < self.config.feedback_timeout_s:
                raise RuntimeError(f'{view} camera feedback stale ({age:.3f}s)')
            if data['serial'] != self.config.cameras[view] or data['view'] != view:
                raise RuntimeError('Camera identity mismatch')

    async def set_episode(self, identifier, boundary):
        episode_name(identifier)
        self.revision += 1
        revision = self.revision
        await asyncio.to_thread(write_json, self.control/'state.json', dict(revision=revision, episode_id=identifier,
            boundary_monotonic=boundary, started_monotonic=boundary if identifier is not None else None))
        deadline = time.monotonic()+3.
        while time.monotonic() < deadline:
            self.check()
            health = [json.loads((p/'heartbeat.json').read_text()) for p in self.jobs.values()]
            if all(d['revision'] == revision and d['episode_id'] == identifier for d in health):
                return
            await asyncio.sleep(.01)
        raise RuntimeError('Dual camera episode boundary was not acknowledged by both cameras')

    async def close(self):
        if self.closed_report is not None:
            return self.closed_report
        for p in self.processes.values():
            if p.returncode is None:
                try: p.terminate()
                except ProcessLookupError: pass
        if self.processes:
            try:
                await asyncio.wait_for(asyncio.gather(*(p.wait() for p in self.processes.values())), 5.)
            except asyncio.TimeoutError:
                for p in self.processes.values():
                    if p.returncode is None:
                        p.kill()
                await asyncio.gather(*(p.wait() for p in self.processes.values()))
        reports = {}
        for view, folder in self.jobs.items():
            p = folder/'report.json'
            reports[view] = json.loads(p.read_text()) if p.exists() else dict(complete=False, error='Worker report missing')
            if self.processes[view].returncode != 0:
                reports[view]['complete'] = False
        for log in self.logs.values():
            log.close()
        self.closed_report = dict(complete=len(reports) == 2 and all(r.get('complete') for r in reports.values()), cameras=reports)
        if self.control.exists():
            write_json(self.control/'report.json', self.closed_report)
        return self.closed_report


def align_dual_episode(directory, task, window=None, metadata=None, *, camera_config, require_robot=True):
    """One-to-one view pairs and causal (already returned) robot observations."""
    directory, config = Path(directory), camera_config.validate()
    frames = {}
    for view in VIEWS:
        rows = load_rows(directory/'cameras'/view/'frames.jsonl')
        for r in rows:
            if (r.get('view') != view or r.get('serial') != config.cameras[view]
                    or not math.isfinite(r['host_received'])):
                raise ValueError('Frame identity or timestamp mismatch')
        frames[view] = sorted((r for r in rows if window is None or window[0] <= r['host_received'] <= window[1]),
                              key=lambda r:r['host_received'])
    observations = [r for r in load_rows(directory/'events.jsonl') if r.get('event') == 'observation']
    observations = sorted((r for r in observations if (window is None or window[0] <= r['query_started'] <= r['query_finished'] <= window[1])),
                          key=lambda r:r['query_finished'])
    ends = [r['query_finished'] for r in observations]
    front_times = [r['host_received'] for r in frames['front']]
    used, paired, accepted = -1, 0, 0
    skews = []
    temporary = directory/'samples.jsonl.tmp'
    with temporary.open('x') as output:
        for wrist in frames['wrist']:
            stamp = wrist['host_received']
            i = bisect.bisect_left(front_times, stamp)
            candidates = [j for j in (i-1, i) if used < j < len(front_times)]
            j = min(candidates, key=lambda j:abs(front_times[j]-stamp)) if candidates else None
            skew = abs(front_times[j]-stamp) if j is not None else None
            pair_valid = j is not None and skew <= config.max_camera_skew_s
            front = frames['front'][j] if pair_valid else None
            if pair_valid:
                used = j; paired += 1; skews.append(skew)
            reference = max(stamp, front['host_received']) if front else stamp
            k = bisect.bisect_right(ends, reference)-1
            obs = observations[k] if k >= 0 else None
            span = obs['query_finished']-obs['query_started'] if obs else None
            age = reference-(obs['query_started']+obs['query_finished'])/2 if obs else None
            robot_valid = obs is not None and 0 <= span <= .15 and 0 <= age <= config.max_robot_age_s
            valid = pair_valid and (robot_valid or not require_robot)
            row = dict(frame=wrist, cameras=dict(wrist=wrist, front=front), task=task, host_reference=reference,
                       camera_pair_valid=pair_valid, camera_skew_s=skew, robot_age_s=age,
                       robot_recording=require_robot, alignment_valid=valid, observation=obs if robot_valid else None)
            output.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
            accepted += int(valid)
    temporary.replace(directory/'samples.jsonl')
    camera_complete = True
    for view in VIEWS:
        p = directory/'cameras'/view/'report.json'
        camera_complete &= bool(frames[view]) and p.exists() and json.loads(p.read_text()).get('complete') is True
    manifest = dict(schema='realman_quest_dual_d405_episode_v1', task=task,
        images=len(frames['wrist']), images_per_camera={v:len(f) for v,f in frames.items()}, paired_images=paired,
        unpaired_wrist_images=len(frames['wrist'])-paired, aligned_samples=accepted,
        unaligned_samples=len(frames['wrist'])-accepted, robot_observations=len(observations),
        camera_complete=bool(camera_complete), camera_config=asdict(config), robot_recording=require_robot,
        max_observed_camera_skew_s=max(skews) if skews else None,
        synchronization='one-to-one nearest host monotonic receipt times; NOT hardware/exposure synchronization',
        robot_alignment='latest query completed before both images became available; age from query midpoint',
        units=dict(joint='explicit rad/degree keys', tcp='m and Euler xyz rad', depth='uint16 * depth_scale_m_per_unit',
                   gripper='device units, not mm'),
        training_ready=False, task_success=None,
        action_contract='Raw sent joints and gripper events; pi05 state/action conversion still required')
    if metadata:
        manifest.update(metadata)
    write_json(directory/'episode.json', manifest)
    return manifest


async def record_camera_only(config, output, seconds, task):
    output.mkdir(parents=True, exist_ok=False)
    session = DualCameraSession(output, None, task, camera_config=config)
    episode = output/'episodes/episode_000001'; episode.mkdir(parents=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stopped.set)
    error, began, ended = None, None, None
    print(f'仅双摄采集，不连接机械臂。目录：{output}', flush=True)
    try:
        await session.start()
        began = time.monotonic()
        await session.set_episode('episode_000001', began)
        print('正在保存两路 RGB/原始深度；Ctrl+C 停止。', flush=True)
        while not stopped.is_set() and (seconds == 0 or time.monotonic()-began < seconds):
            session.check()
            await asyncio.sleep(.02)
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if began is not None:
            ended = time.monotonic()
            try:
                await session.set_episode(None, ended)
            except Exception as exc:
                error = error or str(exc)
        report = await session.close()
        result = align_dual_episode(episode, task, (began, ended) if began is not None else None,
            dict(recording_complete=report['complete'] and error is None, error=error),
            camera_config=config, require_robot=False)
        write_json(output/'report.json', dict(**report, error=error, recording_complete=result['recording_complete'],
                   paired_images=result['paired_images'], robot_connected=False, robot_motion_commands_sent=0))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if error is not None:
            raise RuntimeError(error)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=config_file('cameras_dual.json'))
    p.add_argument('--output', type=Path)
    p.add_argument('--seconds', type=float, default=0, help='0 = until Ctrl+C')
    p.add_argument('--task', default='dual-camera check')
    p.add_argument('--list', action='store_true', help='enumerate serials; no streaming/robot connection')
    p.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.worker:
        camera_worker(a.worker); return
    c = DualCameraConfig.load(a.config)
    if a.list:
        import pyrealsense2 as rs
        print(json.dumps([dict(name=d.get_info(rs.camera_info.name), serial=d.get_info(rs.camera_info.serial_number))
                         for d in rs.context().query_devices()], ensure_ascii=False, indent=2)); return
    if not math.isfinite(a.seconds) or a.seconds < 0:
        p.error('seconds must be finite and nonnegative')
    out = a.output or ROOT/'run'/datetime.now().strftime('dualcam_%Y%m%dT%H%M%S_%f')
    asyncio.run(record_camera_only(c, out.resolve(), a.seconds, a.task))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc))
