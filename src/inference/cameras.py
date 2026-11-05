"""Latest-only dual-D405 RGB capture in independent processes; no disk image queues."""
from __future__ import annotations

import asyncio
import fcntl
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import time

import numpy as np
from recording.dual_camera_recording import DualCameraConfig


def _worker(view, config, shared_rgb, stamp, lock, shutdown, errors, root, owner_pid):
    import pyrealsense2 as rs
    from PIL import Image
    from recording.episode_recording import RGBDPairFilter, frame_info

    pipeline = None
    device_lock = (Path(root) / f'.realsense-{config.cameras[view]}.lock').open('a+')
    try:
        fcntl.flock(device_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pipeline, profile = rs.pipeline(), rs.config()
        serial = config.cameras[view]
        profile.enable_device(serial)
        for kind, fmt in ((rs.stream.color, rs.format.bgr8), (rs.stream.depth, rs.format.z16)):
            profile.enable_stream(kind, config.width, config.height, fmt, config.capture_fps)
        device = pipeline.start(profile).get_device()
        if device.get_info(rs.camera_info.serial_number) != serial or 'D405' not in device.get_info(rs.camera_info.name):
            raise ValueError('D405 role/serial mismatch')
        pair_filter = RGBDPairFilter()
        previous = None
        output = np.frombuffer(shared_rgb, dtype=np.uint8).reshape(224, 224, 3)
        for _ in range(30):
            if shutdown.is_set():
                return
            pipeline.wait_for_frames(1000)
        while not shutdown.is_set():
            if os.getppid() != owner_pid:
                raise RuntimeError("Policy camera owner exited")
            frames = pipeline.wait_for_frames(1000)
            received = time.monotonic()
            color, depth = frames.get_color_frame(), frames.get_depth_frame()
            if not color or not depth:
                raise RuntimeError('Incomplete D405 frameset')
            c, d = frame_info(color), frame_info(depth)
            numbers = (c['frame_number'], d['frame_number'])
            if previous is not None and any(new <= old for new, old in zip(numbers, previous)):
                continue
            previous = numbers
            if not pair_filter.accept(c, d, received):
                continue
            rgb = np.asanyarray(color.get_data())[:, :, ::-1].copy()
            image = Image.fromarray(rgb)
            image.thumbnail((224, 224), Image.Resampling.LANCZOS)
            canvas = Image.new('RGB', (224, 224))
            canvas.paste(image, ((224 - image.width) // 2, (224 - image.height) // 2))
            with lock:
                output[:] = np.asarray(canvas)
                stamp.value = received  # Never refresh timestamp for resize or an old frame.
    except BaseException as exc:
        errors.put((view, f'{type(exc).__name__}: {exc}'))
    finally:
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass
        device_lock.close()


class DualCameras:
    def __init__(self, config, root):
        self.config = config
        self.root = root
        context = multiprocessing.get_context('spawn')
        self.shutdown = context.Event()
        self.errors = context.Queue(maxsize=4)
        self.buffers, self.stamps, self.locks, self.processes, self.cache = {}, {}, {}, {}, {}
        for view in ('front', 'wrist'):
            self.buffers[view] = context.RawArray('B', 224 * 224 * 3)
            self.stamps[view] = context.RawValue('d', 0.)
            self.locks[view] = context.Lock()
            self.processes[view] = context.Process(target=_worker,
                args=(view, config, self.buffers[view], self.stamps[view], self.locks[view], self.shutdown, self.errors, str(root), os.getpid()),
                name=f'PolicyCamera-{view}', daemon=True)

    async def start(self, stop=None):
        for process in self.processes.values():
            process.start()
        deadline = time.monotonic() + 15.
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise InterruptedError("Camera startup cancelled")
            self.check(stale_after=None)
            if self.pair(max_age=.15) is not None:
                return
            await asyncio.sleep(.01)
        raise TimeoutError('Dual cameras did not produce aligned fresh frames within 15s')

    def check(self, stale_after=.15):
        try:
            view, error = self.errors.get_nowait()
        except Empty:
            pass
        else:
            raise RuntimeError(f'{view} camera: {error}')
        now = time.monotonic()
        for view, process in self.processes.items():
            if process.pid is not None and not process.is_alive():
                raise RuntimeError(f'{view} camera process exited: {process.exitcode}')
            if self.locks[view].acquire(False):
                try:
                    stamp = self.stamps[view].value
                    if stamp and (view not in self.cache or stamp != self.cache[view][0]):
                        image = np.frombuffer(self.buffers[view], dtype=np.uint8).reshape(224, 224, 3).copy()
                        self.cache[view] = (stamp, image)
                finally:
                    self.locks[view].release()
            if stale_after is not None and (view not in self.cache or now - self.cache[view][0] > stale_after):
                raise RuntimeError(f'{view} camera data stale')

    def pair(self, max_age=.1):
        self.check(stale_after=None)
        if len(self.cache) != 2:
            return None
        front, wrist = self.cache['front'], self.cache['wrist']
        if abs(front[0] - wrist[0]) > self.config.max_camera_skew_s or time.monotonic() - min(front[0], wrist[0]) > max_age:
            return None
        return {'observation/image': front[1], 'observation/wrist_image': wrist[1]}, min(front[0], wrist[0]), max(front[0], wrist[0])

    async def close(self):
        self.shutdown.set()
        for process in self.processes.values():
            if process.pid is not None:
                await asyncio.to_thread(process.join, 1.5)
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, .5)
        self.errors.close()
