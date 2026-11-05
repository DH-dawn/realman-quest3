#!/usr/bin/env python3
"""Convert realman-quest dual RGB episodes to openpi's pinned LeRobot format."""
from __future__ import annotations

import argparse
import bisect
from collections import Counter
from dataclasses import dataclass
import inspect
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image

SCHEMA = "realman_quest_dual_d405_episode_v1"
NAMES = [f"joint_{i}_rad" for i in range(1, 7)] + ["gripper_opening"]


def rows(path):
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as error:
                    raise ValueError(f"{path}:{line_number}: invalid JSON") from error


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Expected finite number, got {value!r}")
    return float(value)


def joints(value):
    if not isinstance(value, list) or len(value) != 6:
        raise ValueError("Expected exactly 6 joint values")
    return np.asarray([finite(v) for v in value], dtype=np.float32)


def joint_state(robot):
    if "joint_rad" in robot:
        return joints(robot["joint_rad"])
    return np.deg2rad(joints(robot["joint_deg"]))


def gripper_state(obs, max_age):
    g = obs.get("gripper") or {}
    if (g.get("enable_state", g.get("enable")) != 1 or g.get("status") != 1
            or g.get("error") != 0):
        raise ValueError("Missing/disabled/faulted gripper feedback")
    age = finite(obs.get("gripper_age_s"))
    if not 0 <= age <= max_age:
        raise ValueError("Stale gripper feedback")
    pos = finite(g.get("actpos"))
    # Same endpoint tolerance as realman_follow.check_gripper, then clamp.
    lo = -5 if g.get("mode") in (2, 3, 4, 5, 6) else 0
    hi = 1005 if g.get("mode") in (1, 3, 4, 5, 6) else 1000
    if not lo <= pos <= hi:
        raise ValueError("Gripper feedback out of range")
    return float(np.clip(pos / 1000., 0., 1.))


def image_path(episode, value):
    if not isinstance(value, str) or Path(value).is_absolute():
        raise ValueError("Image path must be relative to episode")
    path = (episode / value).resolve()
    if not path.is_relative_to(episode.resolve()) or not path.is_file():
        raise ValueError(f"Missing or unsafe image path: {value}")
    with Image.open(path) as im:
        if im.mode != "RGB":
            raise ValueError(f"Expected RGB image: {path}, got {im.mode}")
        im.verify()
    return path


def load_image(path, size):
    with Image.open(path) as im:
        im = im.convert("RGB")
        # Letterbox, preserving aspect ratio. PNG decodes to RGB; no BGR swap.
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (size, size))
        canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
        return np.asarray(canvas, dtype=np.uint8).copy()


@dataclass
class Sample:
    time: float
    bucket: int
    state: np.ndarray
    observation: dict
    images: dict


def parse_sample(row, episode, config, args):
    if row.get("alignment_valid") is not True or row.get("camera_pair_valid") is not True:
        raise ValueError("Invalid camera/robot alignment")
    if row.get("robot_recording") is not True:
        raise ValueError("Camera-only sample")
    cameras, obs = row["cameras"], row["observation"]
    stamp = finite(row["host_reference"])
    times, images = {}, {}
    for view in ("front", "wrist"):
        frame = cameras[view]
        if frame["view"] != view or frame["serial"] != config["cameras"][view]:
            raise ValueError("Camera role/serial mismatch")
        times[view] = finite(frame["host_received"])
        images[view] = image_path(episode, frame["rgb_path"])
    if abs(stamp - max(times.values())) > 1e-6:
        raise ValueError("Incorrect host_reference")
    if abs(times["front"] - times["wrist"]) > config["max_camera_skew_s"]:
        raise ValueError("Camera skew exceeds configured limit")
    start, end = finite(obs["query_started"]), finite(obs["query_finished"])
    if not 0 <= end - start <= .15 or end > stamp or not 0 <= stamp - (start + end) / 2 <= config["max_robot_age_s"]:
        raise ValueError("Future/stale robot observation")
    bucket = cameras["wrist"]["sample_bucket"]
    if type(bucket) is not int or bucket < 0:
        raise ValueError("Invalid sample bucket")
    state = np.r_[joint_state(obs["robot"]), gripper_state(obs, args.max_gripper_age)].astype(np.float32)
    return Sample(stamp, bucket, state, obs, images)


class Commands:
    def __init__(self, episode):
        self.arm, self.grip = [], []
        for row in rows(episode / "events.jsonl"):
            if row.get("event") == "joint_sent":
                self.arm.append((finite(row["monotonic"]), np.deg2rad(joints(row["joint_deg"]))))
            elif row.get("event") == "gripper_sent":
                self.grip.append((finite(row.get("sent_at", row["monotonic"])), row["request"]))
        self.arm.sort(key=lambda item: item[0])
        self.grip.sort(key=lambda item: item[0])
        self.arm_times = [t for t, _ in self.arm]
        self.grip_times = [t for t, _ in self.grip]

    def action(self, sample, next_sample):
        # Label with the final sent target in this control interval. If no new
        # command arrives, the last absolute target remains the controller goal.
        k = bisect.bisect_right(self.arm_times, next_sample.time) - 1
        if k >= 0:
            q = self.arm[k][1]
        elif sample.observation.get("active") is False:
            q = sample.state[:6]  # Explicit inactive hold, never a missing moving target.
        else:
            raise ValueError("No sent joint target available for active sample")
        k = bisect.bisect_right(self.grip_times, next_sample.time) - 1
        request = self.grip[k][1] if k >= 0 else sample.observation.get("gripper_last_command")
        if request:
            command = request.get("command")
            if command not in ("set_gripper_pick", "set_gripper_release"):
                raise ValueError(f"Unsupported gripper command: {command}")
            opening = 0. if command == "set_gripper_pick" else 1.
        else:
            opening = sample.state[6]  # Before any command, hold observed aperture.
        return np.r_[q, opening].astype(np.float32)


def discover(source):
    if (source / "episode.json").is_file():
        return [source]
    return sorted(p.parent for p in source.rglob("episode.json"))


def prepare(args):
    paths = discover(args.input)
    if not paths:
        raise ValueError(f"No episode.json found under {args.input}")
    segments, report = [], {"action_source": args.action_source, "fps": args.fps, "episodes": [],
                            "vector_names": NAMES, "segments": [], "image_size": args.image_size}
    for episode in paths:
        meta = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
        info = {"source": str(episode), "segments": 0, "frames": 0, "rejected": {}}
        report["episodes"].append(info)
        if meta.get("schema") != SCHEMA:
            raise ValueError(f"Unsupported episode schema: {episode}: {meta.get('schema')}")
        if meta.get("recording_complete") is not True or meta.get("camera_complete") is not True:
            info["skipped"] = "recording incomplete"
            continue
        if meta.get("task_success") is not True:
            info["skipped"] = "not labeled successful"
            continue
        if meta.get("robot_recording") is not True:
            info["skipped"] = "camera-only recording"
            continue
        task = args.task or meta.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"Missing task text: {episode}; supply --task")
        config = meta["camera_config"]
        if config["record_fps"] != args.fps:
            raise ValueError(f"{episode}: record_fps={config['record_fps']} differs from --fps={args.fps}; no implicit resampling")
        commands = Commands(episode) if args.action_source == "sent-target" else None
        if commands is not None and not commands.arm:
            raise ValueError(f"{episode}: no joint_sent events; cannot export sent-target labels (preview/camera-only data?)")
        rejected, runs, run = Counter(), [], []
        previous_time = -math.inf
        for row in rows(episode / "samples.jsonl"):
            try:
                sample = parse_sample(row, episode, config, args)
            except (ValueError, KeyError, TypeError, OSError) as error:
                rejected[str(error)] += 1
                if run:
                    runs.append(run)
                    run = []
                continue
            if sample.time <= previous_time:
                raise ValueError(f"Non-increasing sample timestamps: {episode}")
            previous_time = sample.time
            if run and (sample.bucket != run[-1].bucket + 1 or sample.time - run[-1].time > 1.6 / args.fps):
                runs.append(run)
                run = []
                rejected["temporal gap (split)"] += 1
            run.append(sample)
        if run:
            runs.append(run)
        for run in runs:
            frames = []
            for sample, following in zip(run, run[1:]):
                try:
                    action = commands.action(sample, following) if commands else following.state.copy()
                except ValueError as error:
                    raise ValueError(f"{episode}, t={sample.time}: {error}") from error
                frames.append((sample, action))
            if len(frames) < args.min_frames:
                rejected["short segment frames"] += len(frames)
                continue
            segments.append((task.strip(), frames))
            report["segments"].append({"episode_index": len(segments) - 1, "source": str(episode),
                                       "first_bucket": frames[0][0].bucket, "frames": len(frames),
                                       "first_host_reference": frames[0][0].time,
                                       "last_host_reference": frames[-1][0].time, "task": task.strip()})
            info["segments"] += 1
            info["frames"] += len(frames)
        info["rejected"] = dict(rejected)
    report["total_segments"] = len(segments)
    report["total_frames"] = sum(len(frames) for _, frames in segments)
    if not segments:
        raise ValueError("No eligible training segments. Audit report:\n" + json.dumps(report, ensure_ascii=False, indent=2))
    return segments, report


def require_lerobot():
    import datasets
    if datasets.__version__ != "3.6.0":
        raise RuntimeError(
            f"Converter requires datasets==3.6.0, found {datasets.__version__}. "
            "Use the dedicated data-conversion environment documented in src/convert/README.md. "
            "Do not upgrade the server to datasets 4.x."
        )
    # Match the exact API used by openpi's pinned LeRobot dependency; deliberately
    # do not silently switch to LeRobot v3, whose storage layout differs.
    try:
        from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as error:
        raise RuntimeError(
            f"LeRobot import failed in {sys.executable}: {error}\n"
            "This converter requires openpi's pinned LeRobot v2.1 API (lerobot.common).\n"
            "Install the matching dependency in the interpreter selected in VS Code:\n"
            'python -m pip install "lerobot @ git+https://github.com/huggingface/lerobot.git@'
            '0cf864870cf29f4738d3ade893e6fd13fbd7cdb5"'
        ) from error
    return LeRobotDataset


def export(args, segments, report):
    LeRobotDataset = require_lerobot()
    if args.output.exists():
        raise FileExistsError(f"Output already exists; refusing overwrite: {args.output}")
    features = {key: {"dtype": "image", "shape": (args.image_size, args.image_size, 3),
                      "names": ["height", "width", "channel"]} for key in ("image", "wrist_image")}
    features.update({key: {"dtype": "float32", "shape": (7,), "names": NAMES} for key in ("state", "actions")})
    dataset = LeRobotDataset.create(repo_id=args.repo_id, root=args.output, robot_type="realman_rm65",
                                   fps=args.fps, features=features, use_videos=False,
                                   image_writer_processes=0, image_writer_threads=4)
    # Old pinned APIs accept task at save_episode; newer v2 APIs accept it in add_frame.
    task_in_save = "task" in inspect.signature(dataset.save_episode).parameters
    try:
        for index, (task, frames) in enumerate(segments):
            for sample, action in frames:
                frame = {"image": load_image(sample.images["front"], args.image_size),
                         "wrist_image": load_image(sample.images["wrist"], args.image_size),
                         "state": sample.state, "actions": action}
                if not task_in_save:
                    frame["task"] = task
                dataset.add_frame(frame)
            dataset.save_episode(**({"task": task} if task_in_save else {}))
            print(f"Saved segment {index + 1}/{len(segments)}: {len(frames)} frames", flush=True)
        if hasattr(dataset, "consolidate"):
            dataset.consolidate()
        dataset.stop_image_writer()
        # Validate with the same unmodified LeRobot reader used during training.
        # Its constructor stacks all timestamps and checks episode continuity.
        loaded = LeRobotDataset(repo_id=args.repo_id, root=args.output,
                               delta_timestamps={"actions": [i / args.fps for i in range(10)]})
        if len(loaded) != report["total_frames"]:
            raise ValueError("Read-back frame count differs from conversion report")
        for i in (0, len(loaded) - 1):
            row = loaded[i]
            if tuple(row["state"].shape) != (7,) or tuple(row["actions"].shape) != (10, 7):
                raise ValueError("Read-back state/action chunk shape mismatch")
            for key in ("image", "wrist_image"):
                if tuple(row[key].shape) != (3, args.image_size, args.image_size):
                    raise ValueError(f"Read-back image shape mismatch: {key}")
        from importlib.metadata import version
        report["export_versions"] = {name: version(name) for name in ("datasets", "pyarrow", "numpy", "lerobot")}
        report["reader_validation"] = "passed: unmodified LeRobot v2.1, full timestamps, image decoding, action chunks"
        (args.output / "conversion_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Conversion and training-reader validation complete.", flush=True)
    except BaseException:
        print(f"Conversion incomplete; output retained for diagnosis: {args.output}. Use a new output path on retry.", file=sys.stderr)
        raise
    finally:
        if hasattr(dataset, "stop_image_writer"):
            dataset.stop_image_writer()


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    default_config = Path(__file__).with_name("conversion_config.json")
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", type=Path)
    initial, _ = bootstrap.parse_known_args(argv)
    config_path = initial.config or (default_config if not argv else None)
    defaults = {}
    if config_path is not None:
        config_path = config_path.expanduser().resolve()
        try:
            defaults = json.loads(config_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            bootstrap.error(f"Cannot read config {config_path}: {error}")
        if not isinstance(defaults, dict):
            bootstrap.error("Configuration must be a JSON object")
        allowed = {"input", "output", "repo_id", "fps", "image_size", "min_frames",
                   "max_gripper_age", "task", "action_source", "dry_run"}
        unknown = defaults.keys() - allowed
        if unknown:
            bootstrap.error(f"Unknown configuration keys: {sorted(unknown)}")
        for key in ("input", "output"):
            value = defaults.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                bootstrap.error(f"Config '{key}' must be a nonempty path string or null")
            if value is not None:
                path = Path(value).expanduser()
                defaults[key] = path if path.is_absolute() else config_path.parent / path
        for key in ("fps", "image_size", "min_frames"):
            if key in defaults and type(defaults[key]) is not int:
                bootstrap.error(f"Config '{key}' must be an integer")
        if "max_gripper_age" in defaults and type(defaults["max_gripper_age"]) not in (int, float):
            bootstrap.error("Config 'max_gripper_age' must be a number")
        if "dry_run" in defaults and type(defaults["dry_run"]) is not bool:
            bootstrap.error("Config 'dry_run' must be true or false, not a string")
        if "repo_id" in defaults and (not isinstance(defaults["repo_id"], str) or not defaults["repo_id"].strip()):
            bootstrap.error("Config 'repo_id' must be a nonempty string")
        if defaults.get("task") is not None and not isinstance(defaults["task"], str):
            bootstrap.error("Config 'task' must be a string or null")
        if "action_source" in defaults and defaults["action_source"] not in ("sent-target", "next-state"):
            bootstrap.error("Config 'action_source' must be sent-target or next-state")
        print(f"Loaded configuration: {config_path}", flush=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON configuration; used automatically beside script when run without arguments")
    parser.add_argument("--input", type=Path, help="Episode, session, or collection directory")
    parser.add_argument("--output", type=Path, help="New LeRobot dataset directory; never overwritten")
    parser.add_argument("--repo-id", default="dh/rm65_dual", help="Dataset ID, no upload is performed")
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--min-frames", type=int, default=10)
    parser.add_argument("--max-gripper-age", type=float, default=.5)
    parser.add_argument("--task", help="Override task instruction for every included episode")
    parser.add_argument("--action-source", choices=("sent-target", "next-state"), default="sent-target")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False,
                        help="Validate raw data without LeRobot or output writes; --no-dry-run enables conversion")
    parser.set_defaults(**defaults)
    args = parser.parse_args(argv)
    if args.input is None:
        parser.error(f"Set 'input' to your raw recording directory in {config_path}" if config_path else "--input is required")
    args.input = args.input.expanduser()
    if args.output is not None:
        args.output = args.output.expanduser()
    if args.fps <= 0 or args.image_size <= 0 or args.min_frames <= 0 or not math.isfinite(args.max_gripper_age) or args.max_gripper_age <= 0:
        parser.error("Numeric parameters must be positive and finite")
    if not args.input.is_dir():
        parser.error("--input must be an existing directory")
    if not args.dry_run and args.output is None:
        parser.error("--output is required unless --dry-run")
    args.input = args.input.resolve()
    if args.output is not None:
        args.output = args.output.resolve()
        if args.output.exists():
            parser.error("--output already exists; choose a new path")
    return args


def main(argv=None):
    args = parse_args(argv)
    print(f"Python: {sys.executable}\nInput: {args.input}\nMode: {'validate only' if args.dry_run else 'convert'}", flush=True)
    if args.output is not None:
        print(f"Output: {args.output}", flush=True)
    if not args.dry_run:
        require_lerobot()
    segments, report = prepare(args)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.dry_run:
        export(args, segments, report)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        sys.exit(str(error))
