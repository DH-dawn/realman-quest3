"""Versioned WebSocket JSON protocol; raw uint8 RGB is encoded as base64."""
from __future__ import annotations

import base64
import binascii
import json
import math

import numpy as np

PROTOCOL = "rm65-pi05-v2"
IMAGE_SHAPE = (224, 224, 3)
IMAGE_BYTES = math.prod(IMAGE_SHAPE)
MAX_MESSAGE_BYTES = 1024 * 1024
ACTION_NAMES = [f"joint_{i}_rad" for i in range(1, 7)] + ["gripper_opening"]
# The training converter labels row i with the final target in interval i.
# The reference is retained on the PC; never compare PC and server clocks.
ACTION_TIMING = {"reference": "observation_reference", "step_s": 0.1,
                 "row_semantics": "target_for_interval", "first_index": 0}
ACTION_SEMANTICS = "absolute joint angles in radians and absolute gripper opening 0..1"
IMAGE_KEYS = ("observation/image", "observation/wrist_image")


def _bad_constant(value):
    raise ValueError(f"JSON不允许{value}")


def loads(message):
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("需要不超过1MiB的JSON文本消息")
    value = json.loads(message, parse_constant=_bad_constant)
    if not isinstance(value, dict):
        raise ValueError("消息必须是JSON对象")
    return value


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def validate_observation(observation, default_prompt=None):
    if not isinstance(observation, dict):
        raise ValueError("observation必须是字典")
    state = np.asarray(observation.get("observation/state"), dtype=np.float32)
    if state.shape != (7,) or not np.isfinite(state).all() or not 0 <= state[6] <= 1:
        raise ValueError("state需要6个关节角弧度+夹爪开度0~1，共7个有限数")
    prompt = observation.get("prompt", default_prompt)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2048:
        raise ValueError("任务指令必须是1~2048字符的非空字符串")
    result = {"observation/state": state.copy(), "prompt": prompt}
    for key in IMAGE_KEYS:
        image = np.asarray(observation.get(key))
        if image.shape != IMAGE_SHAPE or image.dtype != np.uint8:
            raise ValueError(f"{key}需要RGB uint8[224,224,3]")
        result[key] = np.ascontiguousarray(image)
    return result


def encode_request(observation, request_id):
    observation = validate_observation(observation)
    wire = {"observation/state": observation["observation/state"].tolist(), "prompt": observation["prompt"]}
    for key in IMAGE_KEYS:
        wire[key] = base64.b64encode(observation[key].tobytes()).decode("ascii")
    return dumps({"type": "infer", "protocol": PROTOCOL, "request_id": request_id,
                  "encoding": "rgb_uint8_224_base64", "observation": wire})


def decode_request(message, default_prompt=None):
    request = loads(message)
    request_id = request.get("request_id")
    if type(request_id) is not int or not 1 <= request_id <= 2**63 - 1:
        raise ValueError("request_id需要1~2^63-1的递增整数")
    if request.get("type") != "infer" or request.get("protocol") != PROTOCOL:
        raise ValueError("请求类型或协议版本不匹配")
    if request.get("encoding") != "rgb_uint8_224_base64":
        raise ValueError("图像编码不匹配")
    wire = request.get("observation")
    if not isinstance(wire, dict):
        raise ValueError("缺少observation")
    observation = {"observation/state": wire.get("observation/state"),
                   "prompt": wire.get("prompt", default_prompt)}
    for key in IMAGE_KEYS:
        data = wire.get(key)
        if not isinstance(data, str) or len(data) != (IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError(f"{key}的图像字节数错误")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"{key}的base64无效") from exc
        if len(raw) != IMAGE_BYTES:
            raise ValueError(f"{key}的解码字节数错误")
        observation[key] = np.frombuffer(raw, dtype=np.uint8).reshape(IMAGE_SHAPE).copy()
    return request_id, validate_observation(observation, default_prompt)


def validate_actions(actions, horizon):
    actions = np.asarray(actions, dtype=np.float32)
    if actions.shape != (horizon, 7) or not np.isfinite(actions).all():
        raise ValueError(f"模型动作必须为有限数[{horizon},7]，实际{actions.shape}")
    return actions


def load_observation(path):
    """Read server_infer.py's observation.npz; object/pickle arrays are forbidden."""
    with np.load(path, allow_pickle=False) as archive:
        observation = {key: archive[key] for key in (*IMAGE_KEYS, "observation/state", "prompt")}
    observation["prompt"] = str(observation["prompt"].item())
    return validate_observation(observation)
