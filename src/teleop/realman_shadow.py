"""Read Quest UDP and RealMan feedback; preview targets without robot actuation.

This program sends only get_current_arm_state. Diagnostic JSONL is NOT a
demonstration dataset: no action commands are executed and no images recorded.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import select
import socket
import struct
import time


PACKET = struct.Struct("<40f")


def decode_packet(data):
    if len(data) != PACKET.size:
        raise ValueError("VR packet must contain exactly 160 bytes")
    values = PACKET.unpack(data)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("VR packet contains non-finite values")
    result = {}
    for name, offset, hand_id in (("left", 0, 0), ("right", 20, 1)):
        hand = values[offset:offset + 20]
        if hand[1] != hand_id or hand[2] not in (0, 1):
            raise ValueError("Invalid hand ID or pose-valid flag")
        if not 0 <= hand[12] <= 1 or not 0 <= hand[13] <= 1:
            raise ValueError("Trigger and grip must be in [0, 1]")
        norm = math.sqrt(sum(value * value for value in hand[6:10]))
        if hand[2] == 1 and not 0.9 <= norm <= 1.1:
            raise ValueError("Tracked pose has an invalid quaternion")
        result[name] = {
            "seq": int(hand[0]), "valid": bool(hand[2]),
            "position_m": list(hand[3:6]), "quaternion_xyzw": list(hand[6:10]),
            "trigger": hand[12], "grip": hand[13],
        }
    return result


def decode_state(response):
    state = response["arm_state"]
    joints, pose, errors = state["joint"], state["pose"], state["err"]
    if len(joints) != 6 or len(pose) != 6 or not errors:
        raise ValueError("Incomplete robot feedback")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in joints + pose + errors):
        raise ValueError("Invalid robot feedback values")
    if any(value != 0 for value in errors):
        raise ValueError("Robot reports an error: " + str(errors))
    degrees = [value / 1000 for value in joints]
    return {
        "joint_deg": degrees,
        "joint_rad": [math.radians(value) for value in degrees],
        "tcp_pose_m_rad": [value / 1e6 for value in pose[:3]]
                            + [value / 1000 for value in pose[3:]],
        "errors": errors,
    }


def read_state(ip, port):
    started = time.monotonic_ns()
    with socket.create_connection((ip, port), timeout=1.0) as connection:
        connection.settimeout(1.0)
        connection.sendall(b'{"command":"get_current_arm_state"}\r\n')
        with connection.makefile("rb") as stream:
            line = stream.readline(65537)
    if len(line) > 65536 or not line.endswith(b"\n"):
        raise ValueError("Incomplete robot reply")
    result = decode_state(json.loads(line))
    result["query_started_monotonic_ns"] = started
    result["query_finished_monotonic_ns"] = time.monotonic_ns()
    return result


def parse_axes(text):
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 3 or any(part not in ("x", "y", "z", "+x", "+y", "+z", "-x", "-y", "-z") for part in parts):
        raise ValueError("axis-map must be a signed permutation, e.g. x,-z,y")
    if {part[-1] for part in parts} != {"x", "y", "z"}:
        raise ValueError("Each VR axis must occur exactly once")
    return [("xyz".index(part[-1]), -1 if part.startswith("-") else 1) for part in parts]


class Shadow:
    def __init__(self, axes, scale, maximum):
        self.axes, self.scale, self.maximum = axes, scale, maximum
        self.anchor = None
        self.previous_seq = None

    def reset(self):
        self.anchor = None

    def update(self, right, robot, age_s):
        # Any gap, tracking loss, sequence reset or release invalidates the anchor.
        reset_seq = self.previous_seq is not None and right["seq"] < self.previous_seq
        self.previous_seq = right["seq"]
        if reset_seq or age_s > 0.2 or not right["valid"] or right["grip"] < 0.8:
            self.reset()
            return {"clutch": False, "target_pose_m_rad": None}
        if self.anchor is None:
            self.anchor = (right["position_m"][:], robot["tcp_pose_m_rad"][:])
        origin, tcp = self.anchor
        delta = [self.scale * sign * (right["position_m"][index] - origin[index])
                 for index, sign in self.axes]
        target = [tcp[index] + delta[index] for index in range(3)] + tcp[3:]
        return {"clutch": True, "mapped_delta_m": delta,
                "within_preview_box": all(abs(value) <= self.maximum for value in delta),
                "target_pose_m_rad": target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-ip", default="192.168.0.18")
    parser.add_argument("--robot-port", type=int, default=8080)
    parser.add_argument("--udp-port", type=int, default=5005)
    parser.add_argument("--axis-map", default="x,-z,y",
                        help="Uncalibrated example: robot XYZ from signed VR axes")
    parser.add_argument("--scale", type=float, default=0.3)
    parser.add_argument("--preview-half-width", type=float, default=0.03)
    parser.add_argument("--seconds", type=float, default=0, help="0: run until Ctrl+C")
    parser.add_argument("--output", type=Path, help="New diagnostic JSONL; never overwrites")
    args = parser.parse_args()
    axes = parse_axes(args.axis_map)
    if (not all(math.isfinite(v) for v in (args.scale, args.preview_half_width, args.seconds))
            or not 0 < args.scale <= 1 or not 0 < args.preview_half_width <= 0.1
            or args.seconds < 0):
        parser.error("Invalid scale, preview bounds or duration")
    if not all(1 <= p <= 65535 for p in (args.robot_port, args.udp_port)):
        parser.error("Invalid port")
    shadow = Shadow(axes, args.scale, args.preview_half_width)
    output = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        try:
            # Exclusive receiver: do not steal packets from Console's Verify.
            receiver.bind(("127.0.0.1", args.udp_port))
        except OSError as error:
            raise RuntimeError("UDP port busy: click Stop Verify in Quest Console first") from error
        receiver.setblocking(False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            output = args.output.open("x", encoding="utf-8")
        header = {"schema": "quest_realman_readonly_shadow_v1", "robot_motion_enabled": False,
                  "training_ready": False, "images_recorded": False,
                  "axis_map_uncalibrated": args.axis_map, "scale": args.scale,
                  "preview_half_width_m": args.preview_half_width,
                  "note": "No workspace or collision acceptance; preview only"}
        print(json.dumps(header, ensure_ascii=False), flush=True)
        print("READ ONLY: robot and gripper motion commands are not implemented.", flush=True)
        print("Hold right Grip to anchor/preview; release to clear. Ctrl+C exits.", flush=True)
        if output:
            output.write(json.dumps(header) + "\n")
            output.flush()
        started = time.monotonic()
        latest = None
        received_ns = None
        packets = duplicates = rejected = 0
        prior_seq = None
        next_read = time.monotonic()
        try:
            while not args.seconds or time.monotonic() - started < args.seconds:
                ready, _, _ = select.select([receiver], [], [], 0.02)
                if ready:
                    for _ in range(1000):
                        try:
                            raw, _ = receiver.recvfrom(4096)
                        except BlockingIOError:
                            break
                        now_ns = time.monotonic_ns()
                        try:
                            packet = decode_packet(raw)
                        except ValueError:
                            rejected += 1
                            latest = received_ns = None
                            shadow.reset()
                            continue
                        seq = (packet["left"]["seq"], packet["right"]["seq"])
                        if seq == prior_seq:
                            duplicates += 1
                            continue
                        prior_seq = seq
                        packets += 1
                        latest, received_ns = packet, now_ns
                if time.monotonic() < next_read:
                    continue
                robot = read_state(args.robot_ip, args.robot_port)
                now = time.monotonic()
                age = (time.monotonic_ns() - received_ns) / 1e9 if received_ns else None
                preview = shadow.update(latest["right"], robot, age) if latest else {"clutch": False, "target_pose_m_rad": None}
                record = {"utc": datetime.now(timezone.utc).isoformat(),
                          "host_monotonic_ns": time.monotonic_ns(),
                          "vr_received_monotonic_ns": received_ns, "vr_age_s": age,
                          "unique_packets": packets, "duplicate_packets": duplicates,
                          "rejected_packets": rejected, "vr": latest,
                          "robot": robot, "preview": preview,
                          "robot_motion_enabled": False, "training_ready": False}
                if output:
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output.flush()
                tracking = "waiting"
                if latest:
                    tracking = (f"L={latest['left']['valid']} R={latest['right']['valid']} "
                                f"age={age:.3f}s trigger={latest['right']['trigger']:.2f} "
                                f"grip={latest['right']['grip']:.2f}")
                delta = [round(x * 1000, 1) for x in preview.get("mapped_delta_m", [])]
                print(f"READONLY {tracking} unique={packets} duplicates={duplicates} "
                      f"TCP(m)={robot['tcp_pose_m_rad'][:3]} preview_delta_mm={delta} "
                      f"within_box={preview.get('within_preview_box', False)}", flush=True)
                next_read = now + 0.1
        finally:
            if output:
                output.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. No robot motion commands were sent.")
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        raise SystemExit(str(error))
