"""Quest relative translation -> bounded, nonqueued RM65 MoveL segments.

Default: read-only. --execute additionally requires a local terminal START.
Reference architecture: GZF123/arx-televr, commit cd557afbb9ec280df2d9e12e1582219b85ee1a53.
This is an independent RealMan/WebXR implementation, not an ARX CAN adapter.
No auto-enable, Home, gravity mode, pose rotation, gripper commands or auto-return.
"""
from __future__ import annotations
from common.paths import config_file

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import struct
import sys
import time

from .realman_shadow import decode_packet, decode_state, parse_axes

from common.paths import ROOT


def norm(v):
    return math.sqrt(sum(x * x for x in v))


def distance(a, b):
    return norm([x - y for x, y in zip(a, b)])


@dataclass(frozen=True)
class Config:
    robot_ip: str = "192.168.0.18"
    robot_port: int = 8080
    udp_port: int = 5005
    axis_map: str = "-x,z,y"
    position_scale: float = .1
    workspace_radius_mm: float = 10.
    step_mm: float = .5
    target_rate_hz: float = 2.
    controller_speed_percent: int = 1
    input_timeout_s: float = .1
    feedback_timeout_s: float = .2
    release_dwell_s: float = 1.
    max_duration_s: float = 300.
    expected_work_frame: str = "Base"
    expected_tool_name: str = "tcp"
    expected_tool_pose_raw: tuple = (0, 0, 145000, 0, 0, 0)
    expected_payload_raw: int = 500
    expected_collision_stage: int = 4
    expected_product: str = "RM65-BI"
    expected_firmware: str = "V1.7.4"

    def validate(self):
        parse_axes(self.axis_map)
        bounds = {
            "position_scale": (.01, .3), "workspace_radius_mm": (1., 10.),
            "step_mm": (.05, .5), "target_rate_hz": (.5, 2.),
            "input_timeout_s": (.03, .1), "feedback_timeout_s": (.05, .2),
            "release_dwell_s": (1., 5.), "max_duration_s": (1., 600.),
        }
        for name, (low, high) in bounds.items():
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not low <= v <= high:
                raise ValueError(f"{name} must be within [{low}, {high}]")
        if type(self.controller_speed_percent) is not int or self.controller_speed_percent != 1:
            raise ValueError("This initial hardware profile permits only 1% controller speed")
        for port in (self.robot_port, self.udp_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid port")
        if (self.expected_work_frame != "Base" or self.expected_tool_name != "tcp"
                or list(self.expected_tool_pose_raw) != [0, 0, 145000, 0, 0, 0]
                or self.expected_payload_raw != 500 or self.expected_collision_stage != 4
                or self.expected_product != "RM65-BI" or self.expected_firmware != "V1.7.4"):
            raise ValueError("Hardware profile changed; this version supports the checked RM65-BI profile only")
        return self

    @classmethod
    def load(cls, path):
        values = json.loads(Path(path).read_text())
        unknown = set(values) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown config fields: {sorted(unknown)}")
        return cls(**values).validate()


class Gate:
    """Observe EVERY received packet, including releases between control ticks.

    A fault invalidates the anchor. Recovery while buttons remain held cannot
    resume motion; both controls must be released under valid tracking for 1 s.
    Freshness refers to host receipt of new sequence numbers, not sensor latency.
    """
    controls_label = "right Grip + Trigger"

    @property
    def dwell_s(self):
        return self.c.release_dwell_s

    def control_value(self, hand):
        return min(hand["grip"], hand["trigger"])

    def is_released(self, hand):
        return max(hand["grip"], hand["trigger"]) < .2

    def __init__(self, config):
        self.c = config
        self.axes = parse_axes(config.axis_map)
        self.seq = None
        self.received = None
        self.hand = None
        self.released_since = None
        self.ready = False
        self.active = False
        self.anchor = None
        self.target = None
        self.epoch = 0
        self.reason = "waiting for valid tracking and both controls released"
        self.packets = self.duplicates = self.rejected = 0

    def trip(self, reason):
        if self.active or self.ready or self.target is not None:
            self.epoch += 1
        self.active = self.ready = False
        self.anchor = self.target = self.released_since = None
        self.reason = reason

    def observe(self, hand, now, robot_pose):
        seq = hand["seq"]
        if seq < 0 or seq > 16000000:
            self.trip("invalid sequence")
            return
        if seq == self.seq:
            self.duplicates += 1
            # A safety transition is never hidden by sequence deduplication.
            if not hand["valid"] or (self.active and self.control_value(hand) < .8):
                self.trip("tracking lost or control released")
            return
        previous, prior_time, prior_seq = self.hand, self.received, self.seq
        self.seq, self.received, self.hand = seq, now, hand
        self.packets += 1
        if (prior_seq is not None and seq < prior_seq):
            self.trip("XR session/sequence reset; release both controls")
            return
        if prior_time is not None and now - prior_time > self.c.input_timeout_s:
            self.trip("input gap; release both controls")
            return
        if not hand["valid"]:
            self.trip("tracking lost; release both controls")
            return
        if self.active and self.control_value(hand) < .8:
            self.trip("control released")
            return
        if previous and previous["valid"] and prior_time is not None:
            moved = distance(hand["position_m"], previous["position_m"])
            # Reject discontinuous reacquisition and implausibly fast hand jumps.
            if moved > .03 or moved > 1.5 * max(now - prior_time, .002) + .003:
                self.trip("hand pose jump; release both controls")
                return
        if not self.active:
            if self.is_released(hand):
                if self.released_since is None:
                    self.released_since = now
                if now - self.released_since >= self.dwell_s:
                    self.ready, self.reason = True, f"READY: hold {self.controls_label}"
                else:
                    self.reason = (f"ARMING: both controls released "
                                   f"{now-self.released_since:.1f}/{self.dwell_s:.1f}s")
            elif self.ready and self.control_value(hand) >= .8:
                self.active, self.ready = True, False
                self.anchor = (hand["position_m"][:], robot_pose[:])
                self.reason = "following"
            elif not self.ready:
                self.released_since = None
                self.reason = f"WAIT RELEASE: fully release {self.controls_label}"
        if self.active:
            origin, tcp = self.anchor
            delta = [self.c.position_scale * sign * (hand["position_m"][index] - origin[index])
                     for index, sign in self.axes]
            self.target = [tcp[i] + delta[i] for i in range(3)] + tcp[3:]

    def fresh(self, now):
        return (self.received is not None and 0 <= now - self.received <= self.c.input_timeout_s
                and self.hand is not None and self.hand["valid"])

    def watchdog(self, now):
        if not self.fresh(now):
            self.trip("no fresh valid right-hand data; release both controls")


class SimpleGate(Gate):
    """Immediate Grip clutch with a zero-displacement startup anchor.

    The command authorizes startup. First valid input can anchor while held;
    faults still require release / squeeze and never resume a held Grip.
    """
    controls_label = "right Grip"

    def __init__(self, config):
        super().__init__(config)
        self.initial_valid_seen = False

    def observe(self, hand, now, robot_pose):
        if hand["valid"] and not self.initial_valid_seen:
            self.initial_valid_seen = True
            self.seq = self.received = self.hand = None
            self.ready = True
        super().observe(hand, now, robot_pose)

    @property
    def dwell_s(self):
        return 0.

    def control_value(self, hand):
        return hand["grip"]

    def is_released(self, hand):
        return hand["grip"] < .2

    def trip(self, reason):
        super().trip(reason.replace("both controls", "Grip"))


READS = {
    "get_current_arm_state", "get_current_work_frame", "get_current_tool_frame",
    "get_arm_software_info", "get_arm_run_mode", "get_collision_stage",
    "get_joint_en_state", "get_joint_min_pos", "get_joint_max_pos",
    "get_program_run_state", "get_arm_current_trajectory",
}


class MotionPermitExpired(RuntimeError):
    """Input interlock revoked before any motion bytes were sent."""


class ArmRPC:
    """Only explicitly enabled instances can send MoveL or stop commands."""
    def __init__(self, config, execute=False):
        self.c, self.execute = config, execute
        self.motion_attempts = 0
        self.stop_attempts = 0
        self.motion_writer = None
        self.read_lock = asyncio.Lock()

    async def close_motion_connection(self):
        writer, self.motion_writer = self.motion_writer, None
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), .1)

    async def call(self, command, payload=None, permit=None):
        # Serialize ordinary queries. STOP uses an independent connection and
        # never waits for the feedback lock or a delayed query response.
        if command in READS:
            async with self.read_lock:
                return await self._exchange(command, payload, permit)
        return await self._exchange(command, payload, permit)

    async def _exchange(self, command, payload=None, permit=None):
        if command not in READS and not (self.execute and command in {"movel", "set_arm_stop"}):
            raise RuntimeError(f"Command forbidden by transport policy: {command}")
        if command == "movel" and (not isinstance(payload, dict)
                or payload.get("v") != 1 or payload.get("r") != 0
                or payload.get("trajectory_connect") != 0
                or not isinstance(payload.get("pose"), list) or len(payload["pose"]) != 6
                or not all(type(v) is int for v in payload["pose"])):
            raise ValueError("MoveL requires six integer pose values, speed 1%, r=0, no trajectory connection")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            self.c.robot_ip, self.c.robot_port, limit=65536), .15)
        keep_open = False
        try:
            # Revalidate after connect: a release may have arrived while opening.
            if command == "movel":
                if self.motion_writer is not None:
                    raise RuntimeError("A previous motion connection still owns its segment")
                if permit is None or not permit():
                    raise MotionPermitExpired("Motion permit expired before send")
                self.motion_attempts += 1  # An ambiguous send is never retried.
            if command == "set_arm_stop":
                self.stop_attempts += 1
            writer.write(json.dumps({"command": command, **(payload or {})}).encode() + b"\r\n")
            await asyncio.wait_for(writer.drain(), .15)
            for _ in range(8):
                line = await asyncio.wait_for(reader.readline(), .15)
                if not line or len(line) > 65536:
                    raise RuntimeError("Incomplete controller reply")
                reply = json.loads(line)
                if not isinstance(reply, dict):
                    raise ValueError("Controller response must be a JSON object")
                if command == "movel":
                    if reply.get("command") == "movel":
                        if reply.get("receive_state") is not True:
                            raise RuntimeError("Controller rejected MoveL")
                        # Keep the motion session alive until feedback confirms arrival
                        # or a stop. A receive_state acknowledgement is not completion.
                        self.motion_writer, keep_open = writer, True
                        return reply
                elif command == "set_arm_stop":
                    if "arm_stop" in reply:
                        if reply["arm_stop"] is True:
                            return reply
                        raise RuntimeError("Stop was not acknowledged")
                else:
                    # Controllers may emit status notifications before the reply.
                    # Never decode an unrelated message as a fresh measurement.
                    expected = {
                        "get_current_arm_state": "arm_state", "get_current_work_frame": "frame_name",
                        "get_current_tool_frame": "tool_name", "get_arm_software_info": "Product_version",
                        "get_arm_run_mode": "mode", "get_collision_stage": "collision_stage",
                        "get_joint_en_state": "en_state", "get_joint_min_pos": "min_pos",
                        "get_joint_max_pos": "max_pos", "get_program_run_state": "run_state",
                        "get_arm_current_trajectory": "type",
                    }[command]
                    if expected in reply:
                        return reply
            raise RuntimeError(f"Expected {command} reply not received; last message: {reply}")
        finally:
            if not keep_open:
                writer.close()
                with suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), .1)

    async def snapshot(self):
        # Timestamp BEFORE queries, so time spent reading never refreshes stale data.
        stamp = time.monotonic()
        state = decode_state(await self.call("get_current_arm_state"))
        trajectory = await self.call("get_arm_current_trajectory")
        if not isinstance(trajectory.get("type"), str):
            raise ValueError("Unknown controller trajectory response")
        return {"received_at": stamp, "state": state, "trajectory": trajectory}

    async def profile(self):
        started = time.monotonic()
        result = {name: await self.call(name) for name in [
            "get_current_work_frame", "get_current_tool_frame", "get_arm_run_mode",
            "get_collision_stage", "get_joint_en_state", "get_program_run_state",
        ]}
        result["_started_at"] = started
        return result


def validate_profile(c, profile):
    work, tool = profile["get_current_work_frame"], profile["get_current_tool_frame"]
    if work.get("frame_name") != c.expected_work_frame or work.get("pose") != [0] * 6:
        raise ValueError("Current work frame must be Base with zero offset")
    if (tool.get("tool_name") != c.expected_tool_name
            or tool.get("pose") != list(c.expected_tool_pose_raw)
            or tool.get("payload") != c.expected_payload_raw or tool.get("position") != [0, 0, 0]):
        raise ValueError("Current tcp tool / 145 mm offset / 0.5 kg payload / load centre changed")
    if profile["get_arm_run_mode"].get("mode") != 1:
        raise ValueError("Controller is not in real robot mode")
    if profile["get_collision_stage"].get("collision_stage") != c.expected_collision_stage:
        raise ValueError("Expected collision stage 4; configuration is never changed automatically")
    if profile["get_joint_en_state"].get("en_state") != [1] * 6:
        raise ValueError("All six axes must already be enabled by the operator")
    if profile["get_program_run_state"].get("run_state") != 0:
        raise ValueError("An online program is running or paused")


async def preflight(rpc):
    c = rpc.c
    profile = await rpc.profile()
    validate_profile(c, profile)
    software = await rpc.call("get_arm_software_info")
    if software.get("Product_version") != c.expected_product or any(
            software.get(k, {}).get("version") != c.expected_firmware for k in ("ctrl_info", "plan_info")):
        raise ValueError("Unexpected robot product or firmware")
    low = (await rpc.call("get_joint_min_pos")).get("min_pos")
    high = (await rpc.call("get_joint_max_pos")).get("max_pos")
    if (not isinstance(low, list) or not isinstance(high, list) or len(low) != 6 or len(high) != 6
            or not all(type(v) is int for v in low + high)
            or any(a >= b for a, b in zip(low, high))):
        raise ValueError("Invalid controller joint limits")
    samples = []
    for _ in range(4):
        s = await rpc.snapshot()
        if s["trajectory"]["type"] != "none":
            raise ValueError("Robot has an active trajectory; do not share control")
        samples.append(s)
        await asyncio.sleep(.1)
    anchor = samples[-1]["state"]
    for s in samples:
        if (distance(s["state"]["tcp_pose_m_rad"][:3], anchor["tcp_pose_m_rad"][:3]) > .0001
                or max(abs(a - b) for a, b in zip(s["state"]["joint_deg"], anchor["joint_deg"])) > .03):
            raise ValueError("Robot is not stationary at startup")
    bounds = ([v / 1000 for v in low], [v / 1000 for v in high])
    check_robot(c, samples[-1], anchor, bounds)
    return {"profile": profile, "software": software, "anchor": anchor, "bounds": bounds}


def check_robot(c, snapshot, anchor, bounds):
    state = snapshot["state"]
    pose, origin = state["tcp_pose_m_rad"], anchor["tcp_pose_m_rad"]
    if distance(pose[:3], origin[:3]) > c.workspace_radius_mm / 1000 + .0003:
        raise ValueError("Measured TCP left the startup workspace")
    if any(abs(math.atan2(math.sin(a - b), math.cos(a - b))) > .01 for a, b in zip(pose[3:], origin[3:])):
        raise ValueError("TCP orientation changed")
    if max(abs(a - b) for a, b in zip(state["joint_deg"], anchor["joint_deg"])) > 3:
        raise ValueError("Joint displacement exceeds the initial 3 degree envelope")
    low, high = bounds
    if any(q < lo + 5 or q > hi - 5 for q, lo, hi in zip(state["joint_deg"], low, high)):
        raise ValueError("Less than 5 degree margin to a joint limit")


def next_segment(c, target, current_command, origin):
    if distance(target[:3], origin[:3]) > c.workspace_radius_mm / 1000:
        raise ValueError("Requested target outside startup sphere; release both controls")
    d = [a - b for a, b in zip(target[:3], current_command[:3])]
    length = norm(d)
    if length < .0002:
        return None
    fraction = min(1., c.step_mm / 1000 / length)
    candidate = [current_command[i] + fraction * d[i] for i in range(3)] + origin[3:]
    # Encode units only here: micrometres and milliradians in JSON protocol.
    raw = [round(v * 1e6) for v in candidate[:3]] + [round(v * 1000) for v in candidate[3:]]
    encoded = [v / 1e6 for v in raw[:3]] + [v / 1000 for v in raw[3:]]
    if distance(encoded[:3], origin[:3]) > c.workspace_radius_mm / 1000 + .000001:
        raise ValueError("Rounded target exceeds workspace")
    return raw, encoded


class Receiver(asyncio.DatagramProtocol):
    """V2 carries an XR-frame clock and stream ID; no replay refreshes receipt age.

    Host-source clock offset is estimated using the minimum arrival offset.
    This detects increasing transport backlog, not absolute one-way latency.
    """
    def __init__(self, gate, pose, require_v2=False):
        self.gate, self.pose, self.require_v2 = gate, pose, require_v2
        self.session = self.source_time = self.offset = None

    def datagram_received(self, data, address):
        if address[0] != "127.0.0.1":
            return
        try:
            now = time.monotonic()
            if len(data) == 176 and data[:4] == b"QVR2":
                _, session, source = struct.unpack_from("<4sId", data)
                if not math.isfinite(source) or source < 0:
                    raise ValueError("Invalid source frame time")
                if session != self.session:
                    if self.session is not None:
                        self.gate.trip("new XR session; release both controls")
                    self.session, self.source_time, self.offset = session, None, None
                    self.gate.seq = self.gate.received = self.gate.hand = None
                if self.source_time is not None and source <= self.source_time:
                    return
                gap = source-self.source_time if self.source_time is not None else 0
                self.source_time = source
                offset = now-source
                self.offset = offset if self.offset is None else min(offset, self.offset)
                if gap > self.gate.c.input_timeout_s or offset-self.offset > .05:
                    self.gate.trip("source frame gap or growing network backlog; release both controls")
                    return
                data = data[16:]
            elif self.require_v2:
                raise ValueError("Real motion requires protocol=rm-teleop-v2 on the Quest URL")
            right = decode_packet(data)["right"]
            self.gate.observe(right, now, self.pose())
        except (ValueError, KeyError, TypeError) as error:
            self.gate.rejected += 1
            self.gate.trip(f"invalid Quest packet: {error}")

    def error_received(self, error):
        self.gate.trip(f"UDP receiver error: {error}")


class Recorder:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.stream = (directory / "events.jsonl").open("x", encoding="utf-8")

    def event(self, event, **values):
        self.stream.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                                     "monotonic": time.monotonic(), "event": event, **values}, ensure_ascii=False) + "\n")
        self.stream.flush()


async def controlled_stop(rpc, recorder, observer=None):
    acknowledged = False
    errors = []
    # Repeating STOP is allowed; repeating an ambiguous MoveL is never allowed.
    for _ in range(2):
        try:
            await rpc.call("set_arm_stop")
            acknowledged = True
            break
        except Exception as error:
            errors.append(str(error))
    if acknowledged:
        deadline, since, reference = time.monotonic() + 2., None, None
        while time.monotonic() < deadline:
            try:
                snap = await rpc.snapshot()
                if observer is not None:
                    observer(snap)
                q = snap["state"]["joint_deg"]
                if snap["trajectory"]["type"] == "none":
                    if reference is None or max(abs(a-b) for a,b in zip(q, reference)) > .03:
                        reference, since = q, time.monotonic()
                    if time.monotonic() - since >= .3:
                        recorder.event("stop_verified", stop_attempts=rpc.stop_attempts)
                        return
                else:
                    reference = since = None
            except Exception as error:
                errors.append(str(error))
            await asyncio.sleep(.05)
    recorder.event("stop_unverified", errors=errors)
    raise RuntimeError("Cannot verify robot stop. Use the physical E-stop and inspect the controller.")


async def run(c, execute, directory, seconds=None, native=False, direct=False):
    if direct and (not native or not execute):
        raise ValueError("Direct mode requires native input and execute mode")
    recorder = Recorder(directory)
    rpc, gate = ArmRPC(c, execute), (SimpleGate(c) if direct else Gate(c))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop_event.set)
    transport = motion = feedback = profile_task = native_task = native_source = None
    lock = None
    result = {"mode": "execute" if execute else "dry-run", "motion_commands_sent": 0,
              "hardware_motion_validated": False, "training_ready": False,
              "start_authorization": "direct_command" if direct else "manual_START" if execute else "read_only"}
    try:
        lock = (ROOT / f".realman-{c.robot_ip}-{c.robot_port}.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another teleop instance owns this robot") from error
        prepared = await preflight(rpc)
        recorder.event("preflight", **prepared)
        if execute and not direct:
            print("真机模式：空载、当前TCP半径10mm内及整臂运动空间须由操作者确认。")
            print("不会回零、使能或开闭夹爪。独立急停须可操作。输入 START 后才允许手柄控制。", flush=True)
            if not sys.stdin.isatty() or input("> ").strip() != "START":
                raise RuntimeError("Manual terminal start not provided")
            prepared = await preflight(rpc)  # Never reuse feedback from before the prompt.
            recorder.event("manual_start", **prepared)
        elif direct:
            recorder.event("direct_start", control="right Grip", **prepared)
            print("直接真机模式：运行即启动；按住右手 Grip 跟随，松开即停，无需 Trigger 或 START。", flush=True)
        anchor, bounds = prepared["anchor"], prepared["bounds"]
        origin = anchor["tcp_pose_m_rad"][:]
        latest = await rpc.snapshot()
        current_command = origin[:]
        if native:
            from vr.xrobotoolkit_input import NativeInput
            native_source = NativeInput()
            native_source.start()
            native_task = asyncio.create_task(native_source.pump(
                gate, lambda: latest["state"]["tcp_pose_m_rad"][:]))
            print("XRoboToolkit native input; explicit right poseValid required. No UDP input.", flush=True)
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: Receiver(gate, lambda: latest["state"]["tcp_pose_m_rad"][:], require_v2=execute),
                local_addr=("127.0.0.1", c.udp_port))
        print(f"{'EXECUTE' if execute else 'READ ONLY'} | {'native' if native else 'UDP '+str(c.udp_port)} | map={c.axis_map} | scale={c.position_scale}")
        if direct:
            print("有效追踪后按住侧面 Grip 小幅移动；启动时已按住也会从当前位置建立零位移起点。")
            print("Grip 松开、失联或摘下头显均停止；10mm总范围，Ctrl+C退出。", flush=True)
        else:
            print("先保持右手追踪有效且 Grip/Trigger 都松开1秒；出现 READY 后，同时握住二键移动。")
            print("松开任一键停止；丢追踪后须松开二键重新准备。Ctrl+C退出。", flush=True)
        started, last_send, last_print, last_profile = time.monotonic(), -math.inf, -math.inf, time.monotonic()
        seen_epoch, dry_target = gate.epoch, origin[:]
        motion_goal = None
        arrived_since = None
        movement_started = None
        acknowledged_at = None
        next_feedback = time.monotonic()
        while not stop_event.is_set() and time.monotonic()-started < (seconds or c.max_duration_s):
            now = time.monotonic()
            if feedback is None and now >= next_feedback:
                feedback = asyncio.create_task(rpc.snapshot())
                next_feedback = now + .1
            if feedback is not None and feedback.done():
                latest = feedback.result()
                feedback = None
                check_robot(c, latest, anchor, bounds)
                if motion_goal is not None and acknowledged_at is not None and latest["received_at"] >= acknowledged_at:
                    arrived = (latest["trajectory"]["type"] == "none"
                               and distance(latest["state"]["tcp_pose_m_rad"][:3], motion_goal[:3]) <= .0002)
                    if arrived:
                        arrived_since = arrived_since or now
                    else:
                        arrived_since = None
            if profile_task is None and now-last_profile >= .2:
                profile_task = asyncio.create_task(rpc.profile())
            if profile_task is not None and profile_task.done():
                checked_profile = profile_task.result()
                validate_profile(c, checked_profile)
                profile_task = None
                last_profile = checked_profile["_started_at"]
            if now - latest["received_at"] > c.feedback_timeout_s:
                raise RuntimeError("Robot feedback stale; stopping")
            if now - last_profile > .5:
                raise RuntimeError("Coordinate / enable / collision profile stale; stopping")
            gate.watchdog(now)
            if gate.active and gate.target is not None and distance(gate.target[:3], origin[:3]) > c.workspace_radius_mm/1000:
                gate.trip("requested target outside startup sphere; release both controls")
            if gate.epoch != seen_epoch:
                seen_epoch = gate.epoch
                recorder.event("interlock", reason=gate.reason)
                if motion is not None:
                    motion.cancel()
                    with suppress(asyncio.CancelledError, MotionPermitExpired):
                        await motion
                    motion = None
                if execute and rpc.motion_attempts:
                    await controlled_stop(rpc, recorder)
                    await rpc.close_motion_connection()
                    for task in (feedback, profile_task):
                        if task is not None:
                            task.cancel()
                            with suppress(asyncio.CancelledError, Exception):
                                await task
                    feedback = profile_task = None
                    latest = await rpc.snapshot()
                    checked_profile = await rpc.profile()
                    validate_profile(c, checked_profile)
                    last_profile = checked_profile["_started_at"]
                    gate.trip("stop verified; release Grip and squeeze again" if direct
                              else "stop verified; release both controls for 1 second")
                    seen_epoch = gate.epoch
                motion_goal = arrived_since = movement_started = acknowledged_at = None
                current_command = latest["state"]["tcp_pose_m_rad"][:]
                dry_target = current_command[:]
                # Safety I/O above awaited; use a new clock on the next tick.
                continue
            if motion is not None and motion.done():
                try:
                    motion.result()  # An ambiguous/failed receive_state latches the run off.
                except MotionPermitExpired:
                    motion = None
                    gate.trip("no fresh motion permit; release both controls")
                    continue
                motion = None
                acknowledged_at = now
            if motion_goal is not None and arrived_since is not None and now-arrived_since >= .15:
                recorder.event("arrived", target_m_rad=motion_goal)
                await rpc.close_motion_connection()
                current_command = motion_goal
                motion_goal = arrived_since = movement_started = acknowledged_at = None
            if movement_started is not None and now-movement_started > 3:
                raise RuntimeError("MoveL did not reach its target and become idle in 3 seconds")
            if (execute and motion_goal is None and motion is None
                    and latest["trajectory"]["type"] != "none"):
                raise RuntimeError("Unexpected trajectory from another controller")
            if gate.active and gate.target is not None and now-last_send >= 1/c.target_rate_hz and motion_goal is None:
                reference = current_command if execute else dry_target
                try:
                    segment = next_segment(c, gate.target, reference, origin)
                except ValueError as error:
                    gate.trip(str(error))
                    continue
                if segment is not None:
                    raw, encoded = segment
                    if execute:
                        epoch = gate.epoch
                        # The closure runs immediately before TCP write, after async connect.
                        def permit():
                            return (gate.active and gate.epoch == epoch and gate.fresh(time.monotonic())
                                    and time.monotonic()-latest["received_at"] <= c.feedback_timeout_s
                                    and time.monotonic()-last_profile <= .5)
                        recorder.event("movel_requested", pose_raw=raw, target_m_rad=encoded)
                        motion = asyncio.create_task(rpc.call("movel", {
                            "pose": raw, "v": c.controller_speed_percent, "r": 0, "trajectory_connect": 0,
                        }, permit=permit))
                        motion_goal, movement_started = encoded, now
                        arrived_since = acknowledged_at = None
                    else:
                        dry_target = encoded
                        recorder.event("dry_target", target_m_rad=encoded)
                    last_send = now
            if now-last_print >= .5:
                status = {"reason": gate.reason, "active": gate.active, "ready": gate.ready,
                          "unique_packets": gate.packets, "duplicates": gate.duplicates,
                          "rejected": gate.rejected, "right_valid": gate.fresh(now),
                          "target_m_rad": gate.target, "robot": latest["state"],
                          "motion_attempts": rpc.motion_attempts}
                status.update(right_grip=gate.hand["grip"] if gate.hand else None,
                              right_trigger=gate.hand["trigger"] if gate.hand else None,
                              release_dwell_elapsed_s=(now-gate.released_since
                                  if gate.released_since is not None else None))
                if native_source is not None:
                    status.update(native_right=native_source.last, native_error=native_source.last_error)
                recorder.event("status", **status)
                buttons = (f"Grip={gate.hand['grip']:.2f} Trigger={gate.hand['trigger']:.2f}"
                           if gate.hand else "Grip=? Trigger=?")
                print(f"{'EXECUTE' if execute else 'READONLY'} {gate.reason} packets={gate.packets} "
                      f"R={gate.fresh(now)} rejected={gate.rejected} "
                      f"{buttons} "
                      f"target_xyz={gate.target[:3] if gate.target else None}", flush=True)
                last_print = now
            await asyncio.sleep(.005)
        result["status"] = "stopped_by_operator_or_duration"
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if transport is not None:
            transport.close()
        for task in (motion, feedback, profile_task, native_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if native_source is not None:
            native_source.close()
            result.update(input_source="xrobotoolkit", native_frames=native_source.frames,
                          native_valid_tracking_frames=native_source.tracking_frames,
                          native_last_error=native_source.last_error)
        if execute and rpc.motion_attempts:
            try:
                await controlled_stop(rpc, recorder)
                result["stop_verified"] = True
            except Exception as error:
                result.update(status="failed_stop_unverified", stop_verified=False, stop_error=str(error))
                print(str(error), file=sys.stderr)
        await rpc.close_motion_connection()
        result.update(motion_commands_sent=rpc.motion_attempts, stop_commands_sent=rpc.stop_attempts,
                      quest_unique_packets=gate.packets, path_clearance_verified=False)
        (directory / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        recorder.stream.close()
        if lock is not None:
            lock.close()
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if result.get("stop_verified") is False:
        raise RuntimeError(result["stop_error"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=config_file("realman_teleop.json"))
    parser.add_argument("--native", action="store_true", help="XRoboToolkit callbacks; disables UDP input")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Manual terminal START permits real bounded MoveL")
    mode.add_argument("--direct", action="store_true", help="Native one-command real motion with Grip clutch; no START prompt")
    mode.add_argument("--check", action="store_true", help="Read-only robot preflight; no UDP binding")
    parser.add_argument("--seconds", type=float, help="Shorten the configured maximum duration")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.direct and not args.native:
        parser.error("--direct requires --native")
    c = Config.load(args.config)
    if args.seconds is not None and (not math.isfinite(args.seconds) or not 0 < args.seconds <= c.max_duration_s):
        parser.error("seconds must be positive and cannot exceed configured duration")
    if args.check:
        print(json.dumps(asyncio.run(preflight(ArmRPC(c))), ensure_ascii=False, indent=2))
        return
    directory = args.output or ROOT / "run" / datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    asyncio.run(run(c, args.execute or args.direct, directory, args.seconds, native=args.native, direct=args.direct))


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
