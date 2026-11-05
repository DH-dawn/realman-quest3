"""D405 process and explicit host-receipt alignment for teleop episodes.

RGB/depth acquisition and PNG encoding never run in the motion event loop.
Device clocks are preserved, never compared directly to the host robot clock.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from common.paths import module_command, module_environment


def write_json(path, data):
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


class CameraSession:
    def __init__(self, directory, config, task, segmented=False):
        self.directory, self.config, self.task = Path(directory), config, task
        self.camera = self.directory/"camera"
        self.process = self.log = None
        self.last_check = -float("inf")
        self.segmented = segmented
        self.revision = 0

    async def start(self):
        self.camera.mkdir()
        write_json(self.camera/"config.json", {**asdict(self.config), "segmented": self.segmented,
                                              'owner_pid': os.getpid()})
        if self.segmented:
            write_json(self.camera/"episode_state.json", dict(revision=0, episode_id=None,
                       boundary_monotonic=time.monotonic(), started_monotonic=None))
        self.log = (self.camera/"worker.log").open("w")
        self.process = await asyncio.create_subprocess_exec(
            *module_command('recording.episode_recording'), "--worker", str(self.camera),
            stdout=self.log, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            env=module_environment())
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                raise RuntimeError(f"D405 capture failed; see {self.camera/'worker.log'}")
            if (self.camera/"heartbeat.json").exists():
                self.check()
                print("D405 已就绪；等待手柄 A 开始录制。" if self.segmented
                      else "D405 已开始连续保存 RGB / 原始深度。", flush=True)
                return
            await asyncio.sleep(.05)
        raise RuntimeError("D405 startup timeout; robot control not started")

    def check(self):
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError("D405 recording process stopped")
        now = time.monotonic()
        if now-self.last_check < .1:
            return
        self.last_check = now
        heartbeat = json.loads((self.camera/"heartbeat.json").read_text())
        if not 0 <= now-heartbeat["monotonic"] < 1.:
            raise RuntimeError("D405 recording stalled; stopping arm")

    async def set_episode(self, episode_id, boundary):
        if not self.segmented:
            raise RuntimeError("Segmented camera mode is not enabled")
        self.revision += 1
        revision = self.revision
        await asyncio.to_thread(write_json, self.camera/"episode_state.json",
                                dict(revision=revision, episode_id=episode_id,
                                     boundary_monotonic=boundary,
                                     started_monotonic=boundary if episode_id else None))
        deadline = time.monotonic()+2.
        while time.monotonic() < deadline:
            self.check()
            ack = json.loads((self.camera/"heartbeat.json").read_text())
            if ack.get("revision") == revision and ack.get("episode_id") == episode_id:
                return
            await asyncio.sleep(.01)
        raise RuntimeError("D405 episode boundary acknowledgement timed out")

    async def close(self):
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.log:
            self.log.close()
        if self.segmented:
            path = self.camera/"report.json"
            return json.loads(path.read_text()) if path.exists() else dict(complete=False)
        return await asyncio.to_thread(align_episode, self.directory, self.task)


def frame_info(frame):
    p = frame.profile.as_video_stream_profile()
    intr = p.get_intrinsics()
    return dict(frame_number=frame.get_frame_number(), device_timestamp_ms=frame.get_timestamp(),
                timestamp_domain=str(frame.get_frame_timestamp_domain()),
                format=str(p.format()), intrinsics=dict(width=intr.width, height=intr.height,
                    fx=intr.fx, fy=intr.fy, ppx=intr.ppx, ppy=intr.ppy,
                    distortion_model=str(intr.model), coefficients=list(intr.coeffs)))


class RGBDPairFilter:
    """Reject mismatched frames without ending a session on an isolated drop.

    Rejected pairs never refresh the camera heartbeat or become training images.
    Continuous mismatch still fails within the existing one-second health bound.
    """
    def __init__(self):
        self.accepted = self.rejected = 0
        self.invalid_since = None
        self.last_rejection = None

    def accept(self, color, depth, received):
        c, d = color['device_timestamp_ms'], depth['device_timestamp_ms']
        valid = (color['timestamp_domain'] == depth['timestamp_domain']
                 and all(type(t) in (int, float) and math.isfinite(t) for t in (c, d))
                 and abs(c-d) <= 20.)
        if valid:
            self.accepted += 1
            self.invalid_since = None
            return True
        self.rejected += 1
        self.invalid_since = received if self.invalid_since is None else self.invalid_since
        self.last_rejection = dict(host_received=received, color=color, depth=depth)
        if received-self.invalid_since >= 1.:
            raise RuntimeError('Persistent RGB/depth clock domain or timing mismatch')
        return False

    def summary(self):
        return dict(accepted_pairs=self.accepted, rejected_pairs=self.rejected,
                    last_rejection=self.last_rejection)


class ImageWriter:
    """One episode owns its files; the camera pipeline outlives this writer."""
    def __init__(self, directory, calibration, fps, existing=False):
        self.directory, self.fps = Path(directory), fps
        self.directory.mkdir(parents=True, exist_ok=existing)
        (self.directory/"rgb").mkdir()
        (self.directory/"depth_raw").mkdir()
        write_json(self.directory/"calibration.json", calibration)
        self.rows = (self.directory/"frames.jsonl").open("x")
        self.count, self.due = 0, -float("inf")

    def write(self, color, depth, c, d, wait_start, received, utc_received):
        import cv2
        import numpy as np
        if received < self.due:
            return
        rgb_path, depth_path = f"rgb/{self.count:06d}.png", f"depth_raw/{self.count:06d}.png"
        for path, image in ((rgb_path, np.asanyarray(color.get_data())),
                            (depth_path, np.asanyarray(depth.get_data()))):
            if not cv2.imwrite(str(self.directory/path), image):
                raise OSError("Image write failed: "+path)
        row = dict(index=self.count, host_wait_started=wait_start, host_received=received,
                   utc=utc_received, color=c, depth=d,
                   rgb_path="camera/"+rgb_path, depth_path="camera/"+depth_path)
        self.rows.write(json.dumps(row)+"\n")
        self.rows.flush()
        self.count += 1
        self.due = received+1/self.fps-.001

    def close(self, error=None):
        self.rows.close()
        write_json(self.directory/"report.json", dict(saved=self.count, complete=error is None, error=error))


def camera_worker(directory):
    import cv2
    import numpy as np
    import pyrealsense2 as rs
    config = json.loads((directory/"config.json").read_text())
    owner_pid = config.get('owner_pid', os.getppid())
    pipeline, settings = rs.pipeline(), rs.config()
    settings.enable_device(config["camera_serial"])
    for kind, fmt in ((rs.stream.color, rs.format.bgr8), (rs.stream.depth, rs.format.z16)):
        settings.enable_stream(kind, config["camera_width"], config["camera_height"], fmt, config["camera_fps"])
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    started = False
    writer = None
    count, error, calibration = 0, None, None
    pair_filter = RGBDPairFilter()
    rejected = (directory/'rejected_pairs.jsonl').open('x')
    segmented = config.get("segmented", False)
    active_id, revision = None, 0
    try:
        profile = pipeline.start(settings)
        started = True
        for _ in range(30):
            pipeline.wait_for_frames(2000)
            if stopped:
                return
        while not stopped:
            if os.getppid() != owner_pid:
                raise RuntimeError('Recorder owner exited before camera finalization')
            wait_start = time.monotonic()
            frames = pipeline.wait_for_frames(1000)
            received = time.monotonic()
            utc_received = datetime.now(timezone.utc).isoformat()
            color, depth = frames.get_color_frame(), frames.get_depth_frame()
            if not color or not depth:
                raise RuntimeError("Incomplete RGBD frameset")
            c, d = frame_info(color), frame_info(depth)
            try:
                pair_valid = pair_filter.accept(c, d, received)
            finally:
                if pair_filter.last_rejection and pair_filter.last_rejection['host_received'] == received:
                    rejected.write(json.dumps(pair_filter.last_rejection)+'\n')
                    rejected.flush()
            if not pair_valid:
                continue
            if calibration is None:
                device = profile.get_device()
                ex = depth.profile.get_extrinsics_to(color.profile)
                calibration = dict(
                    serial=device.get_info(rs.camera_info.serial_number),
                    camera_name=device.get_info(rs.camera_info.name),
                    depth_scale_m_per_unit=device.first_depth_sensor().get_depth_scale(),
                    color=c, depth=d, depth_to_color=dict(rotation_column_major=list(ex.rotation),
                        translation_m=list(ex.translation)), rgb_storage="PNG, decoded RGB (OpenCV storage conversion BGR)",
                    depth_storage="PNG uint16, raw depth optical frame, NOT aligned to RGB")
                write_json(directory/"calibration.json", calibration)
            if segmented:
                state = json.loads((directory/"episode_state.json").read_text())
                episode_id = state["episode_id"]
                if episode_id is not None and (Path(episode_id).name != episode_id or not episode_id.startswith("episode_")):
                    raise ValueError("Invalid camera episode id")
                if episode_id != active_id:
                    if writer:
                        count += writer.count
                        writer.close()
                        writer = None
                    if episode_id is not None:
                        writer = ImageWriter(directory.parent/"episodes"/episode_id/"camera",
                                             calibration, config["record_fps"])
                    active_id = episode_id
                revision = state["revision"]
                if writer and received >= state["started_monotonic"]:
                    writer.write(color, depth, c, d, wait_start, received, utc_received)
            else:
                if writer is None:
                    writer = ImageWriter(directory, calibration, config["record_fps"], existing=True)
                writer.write(color, depth, c, d, wait_start, received, utc_received)
            write_json(directory/"heartbeat.json", dict(monotonic=received,
                       saved=count+(writer.count if writer else 0), revision=revision, episode_id=active_id,
                       rejected_pairs=pair_filter.rejected))
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if started:
            pipeline.stop()
        if writer:
            count += writer.count
            writer.close(error)
        rejected.close()
        write_json(directory/"report.json", dict(saved=count, complete=error is None, error=error,
                                               **pair_filter.summary()))


def load_rows(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # A killed process may leave a partial trailing line. Keep failure visible.
            raise ValueError(f"Incomplete JSONL recording: {path}")
    return result


def align_episode(directory, task, window=None, metadata=None):
    directory = Path(directory)
    frames = load_rows(directory/"camera/frames.jsonl")
    events = load_rows(directory/"events.jsonl")
    raw_images = len(frames)
    observations = [r for r in events if r["event"] == "observation"]
    if window is not None:
        start, end = window
        frames = [f for f in frames if start <= f["host_received"] <= end]
        observations = [r for r in observations if start <= r["query_started"]
                        and r["query_finished"] <= end]
    clocks = [(r["query_started"]+r["query_finished"])/2 for r in observations]
    accepted, rejected = 0, 0
    samples = directory/'samples.jsonl'
    temporary_samples = directory/'samples.jsonl.tmp'
    with temporary_samples.open("w") as output:
        for frame in frames:
            timestamp = frame["host_received"]
            i = bisect.bisect_left(clocks, timestamp)
            choices = [j for j in (i-1, i) if 0 <= j < len(clocks)]
            nearest = min(choices, key=lambda j: abs(clocks[j]-timestamp)) if choices else None
            obs = observations[nearest] if nearest is not None else None
            skew = abs(clocks[nearest]-timestamp) if obs else None
            query_span = obs["query_finished"]-obs["query_started"] if obs else None
            valid = (obs is not None and skew <= .1 and 0 <= query_span <= .15)
            row = dict(frame=frame, task=task, alignment_valid=valid,
                       host_alignment_skew_s=skew, robot_query_span_s=query_span,
                       observation=obs if valid else None)
            output.write(json.dumps(row, ensure_ascii=False)+"\n")
            accepted += int(valid)
            rejected += int(not valid)
    temporary_samples.replace(samples)
    camera_report = directory/"camera/report.json"
    camera_complete = (bool(frames) and camera_report.exists()
                       and json.loads(camera_report.read_text()).get("complete") is True)
    manifest = dict(schema="realman_quest_d405_episode_v1", task=task,
                    images=len(frames), raw_images=raw_images,
                    excluded_boundary_images=raw_images-len(frames),
                    robot_observations=len(observations), aligned_samples=accepted,
                    unaligned_samples=rejected, camera_complete=camera_complete,
                    synchronization="nearest host receipt / robot query midpoint; software alignment, NOT hardware sync",
                    units=dict(joint="radian and degree, explicit keys", tcp="meter, Euler xyz radian",
                               depth="uint16 times depth_scale_m_per_unit", gripper="actpos device aperture units, NOT mm"),
                    actions="events.jsonl joint_sent and gripper_acknowledged; targets distinct from measured feedback",
                    task_success=None, training_ready=False,
                    action_contract="Raw joint targets + arm/gripper feedback; choose and validate training action schema before conversion")
    if metadata:
        manifest.update(metadata)
    write_json(directory/"episode.json", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    camera_worker(parser.parse_args().worker)
