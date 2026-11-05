"""Load tools/train/server_train.py's RM65 LoRA/model/transforms once per process."""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
import sys
import time


def select_checkpoint(path, asset_id=None):
    path = path.expanduser().resolve()
    if not path.is_dir() or any("orbax-checkpoint-tmp" in part for part in path.parts):
        raise ValueError(f"需要已保存完成的checkpoint目录：{path}")
    if not (path / "params").is_dir():
        candidates = [p for p in path.iterdir() if p.is_dir() and p.name.isdigit()
                      and (p / "params").is_dir() and (p / "assets").is_dir()]
        if not candidates:
            raise ValueError(f"找不到包含params和assets的步数目录：{path}")
        path = max(candidates, key=lambda p: int(p.name))
    assets = path / "assets"
    if asset_id is None:
        stats = [p for p in assets.rglob("norm_stats.json")
                 if not any("orbax-checkpoint-tmp" in part for part in p.parts)]
        if len(stats) != 1:
            raise ValueError("需要唯一的checkpoint归一化统计，或显式设置asset_id")
        asset_id = stats[0].parent.relative_to(assets).as_posix()
    stats = (assets / asset_id / "norm_stats.json").resolve()
    if not stats.is_relative_to(assets.resolve()) or not stats.is_file():
        raise ValueError(f"缺少checkpoint归一化统计：{stats}")
    if any("orbax-checkpoint-tmp" in part for part in stats.parts):
        raise ValueError("归一化统计尚未保存完成")
    return path, asset_id


def build_config(root, checkpoint, asset_id):
    from openpi.training import config as config_lib

    base = config_lib.get_config("pi05_libero")
    model = dataclasses.replace(base.model, paligemma_variant="gemma_2b_lora",
                                action_expert_variant="gemma_300m_lora")
    data = dataclasses.replace(base.data, repo_id=asset_id, extra_delta_transform=True,
        assets=config_lib.AssetsConfig(assets_dir=str(checkpoint / "assets"), asset_id=asset_id),
        base_config=config_lib.DataConfig(prompt_from_task=True))
    return dataclasses.replace(base, name="pi05_rm65_dual_lora", model=model, data=data,
        freeze_filter=model.get_freeze_filter(), ema_decay=None, pytorch_weight_path=None,
        assets_base_dir=str(root / "assets"), checkpoint_base_dir=str(root / "checkpoints"),
        wandb_enabled=False, fsdp_devices=1)


def load_policy(root, settings):
    # Must run before importing JAX/openpi; model loads never occur in client.py.
    os.environ["CUDA_VISIBLE_DEVICES"] = "" if settings["gpu"] == "cpu" else settings["gpu"]
    if settings["gpu"] == "cpu":
        os.environ["JAX_PLATFORMS"] = "cpu"
    os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    if not (root / "src/openpi/policies/policy_config.py").is_file():
        raise ValueError(f"openpi根目录不正确：{root}")
    checkpoint = Path(settings["checkpoint"]).expanduser()
    if not checkpoint.is_absolute():
        checkpoint = root / checkpoint
    checkpoint, asset_id = select_checkpoint(checkpoint, settings["asset_id"])
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(1, str(root / "packages/openpi-client/src"))
    import jax
    import numpy as np
    from openpi.policies import policy_config
    from .protocol import IMAGE_KEYS, validate_actions

    devices = jax.devices()
    platform = "cpu" if settings["gpu"] == "cpu" else "gpu"
    if len(devices) != 1 or devices[0].platform != platform:
        raise ValueError(f"期望一张{platform}设备，实际{devices}；请使用训练的conda pi05环境")
    config = build_config(root, checkpoint, asset_id)
    logging.info("加载 %s，设备 %s；首次启动需要加载权重并编译", checkpoint, devices)
    policy = policy_config.create_trained_policy(config, checkpoint,
        sample_kwargs={"num_steps": settings["num_steps"]}, default_prompt=settings["default_prompt"])
    # Compile before opening the port. Synthetic warm-up outputs are discarded, never sent to a client.
    observation = {key: np.zeros((224, 224, 3), dtype=np.uint8) for key in IMAGE_KEYS}
    observation.update({"observation/state": np.zeros(7, dtype=np.float32), "prompt": settings["default_prompt"]})
    started = time.monotonic()
    validate_actions(policy.infer(observation)["actions"], config.model.action_horizon)
    logging.info("模型预热完成，耗时 %.1fs；已丢弃预热输出", time.monotonic() - started)
    return policy, {"config": config.name, "checkpoint": str(checkpoint), "asset_id": asset_id,
        "action_horizon": config.model.action_horizon, "num_steps": settings["num_steps"], "sample_rate_hz": 10,
        "action_semantics": "absolute joint angles in radians and absolute gripper opening 0..1"}
