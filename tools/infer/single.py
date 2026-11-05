#!/usr/bin/env python3
"""Run pi05_rm65_dual_lora inference on a dataset frame or two RGB images."""
from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
import time


ACTION_NAMES = [f"joint_{i}_rad" for i in range(1, 7)] + ["gripper_opening"]


def rgb_image(source):
    """Match convert_dual_to_lerobot.load_image: RGB, letterbox, 224x224."""
    import numpy as np
    from PIL import Image

    with Image.open(source) as image:
        image = image.convert("RGB")
        image.thumbnail((224, 224), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (224, 224))
        canvas.paste(image, ((224 - image.width) // 2, (224 - image.height) // 2))
        return np.asarray(canvas, dtype=np.uint8).copy()


def validate_observation(observation):
    import numpy as np

    state = np.asarray(observation["observation/state"], dtype=np.float32)
    if state.shape != (7,) or not np.isfinite(state).all() or not 0 <= state[6] <= 1:
        raise ValueError("state必须是7个有限数：6个关节角（弧度）+夹爪开度（0~1）")
    observation["observation/state"] = state
    for key in ("observation/image", "observation/wrist_image"):
        image = np.asarray(observation[key])
        if image.shape != (224, 224, 3) or image.dtype != np.uint8:
            raise ValueError(f"{key}必须是RGB uint8[224,224,3]")
    prompt = observation.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("必须提供非空任务指令，或使用数据集tasks.jsonl中的任务")
    return observation


def dataset_observation(dataset, episode, frame, prompt=None):
    """Read just one batch from the requested episode; no training actions enter inference."""
    import pyarrow.parquet as pq

    dataset = dataset.expanduser().resolve()
    info = json.loads((dataset / "meta/info.json").read_text(encoding="utf-8"))
    if info.get("codebase_version") != "v2.1" or info.get("robot_type") != "realman_rm65" or info.get("fps") != 10:
        raise ValueError("需要训练使用的10Hz realman_rm65 LeRobot v2.1数据集")
    for key in ("state", "actions"):
        if info["features"][key]["shape"] != [7]:
            raise ValueError(f"数据集{key}维度必须为7")
    if not 0 <= episode < info["total_episodes"]:
        raise ValueError(f"episode超出范围：0~{info['total_episodes'] - 1}")
    relative = info["data_path"].format(episode_chunk=episode // info["chunks_size"], episode_index=episode)
    parquet = (dataset / relative).resolve()
    if not parquet.is_relative_to(dataset):
        raise ValueError("数据文件必须位于数据集目录内")
    reader = pq.ParquetFile(parquet)
    if not 0 <= frame < reader.metadata.num_rows:
        raise ValueError(f"frame超出当前episode范围：0~{reader.metadata.num_rows - 1}")
    row = None
    for index, batch in enumerate(reader.iter_batches(batch_size=1,
            columns=["image", "wrist_image", "state", "task_index"])):
        if index == frame:
            row = batch.to_pylist()[0]
            break
    if row is None:
        raise ValueError("未读到指定数据帧")
    images = {}
    for key in ("image", "wrist_image"):
        value = row[key]
        if not isinstance(value, dict) or not value.get("bytes"):
            raise ValueError(f"{key}缺少嵌入图像；请使用训练用的完整导出数据集")
        images[key] = rgb_image(io.BytesIO(value["bytes"]))
    if prompt is None:
        tasks = [json.loads(line) for line in (dataset / "meta/tasks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        matches = [entry["task"] for entry in tasks if entry["task_index"] == row["task_index"]]
        if len(matches) != 1:
            raise ValueError("当前帧的task_index没有唯一对应的任务指令")
        prompt = matches[0]
    observation = validate_observation({"observation/state": row["state"],
        "observation/image": images["image"], "observation/wrist_image": images["wrist_image"], "prompt": prompt})
    return observation, {"kind": "dataset", "dataset": str(dataset), "episode": episode, "frame": frame}


def select_checkpoint(path, asset_id=None):
    """Accept an explicit completed step, or select the latest completed numeric step."""
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"checkpoint不存在：{path}")
    if any("orbax-checkpoint-tmp" in part for part in path.parts):
        raise ValueError("不能读取正在保存的Orbax临时目录；请等待保存完成")
    if not (path / "params").is_dir():
        candidates = [p for p in path.iterdir() if p.is_dir() and p.name.isdigit()
                      and (p / "params").is_dir() and (p / "assets").is_dir()]
        if not candidates:
            raise FileNotFoundError("没有已完成的步数目录；需要params/与assets/，不能只提供pi05_base")
        path = max(candidates, key=lambda p: int(p.name))
    assets = path / "assets"
    if asset_id is None:
        stats = list(assets.rglob("norm_stats.json"))
        if len(stats) != 1:
            raise ValueError("checkpoint中必须有唯一一份norm_stats.json，否则用--asset-id明确指定")
        asset_id = stats[0].parent.relative_to(assets).as_posix()
    stats_path = (assets / asset_id / "norm_stats.json").resolve()
    if not stats_path.is_relative_to(assets.resolve()) or not stats_path.is_file():
        raise FileNotFoundError(f"checkpoint缺少归一化统计：{stats_path}")
    if any("orbax-checkpoint-tmp" in part for part in stats_path.parts):
        raise ValueError("归一化统计仍位于临时目录，请等待checkpoint保存完成")
    return path, asset_id


def build_config(root, asset_id):
    """Match tools/train/server_train.py model/data settings without global registration."""
    from openpi.training import config as config_lib

    base = config_lib.get_config("pi05_libero")
    model = dataclasses.replace(base.model, paligemma_variant="gemma_2b_lora",
                                action_expert_variant="gemma_300m_lora")
    data = dataclasses.replace(base.data, repo_id=asset_id, extra_delta_transform=True,
        assets=config_lib.AssetsConfig(asset_id=asset_id),
        base_config=config_lib.DataConfig(prompt_from_task=True))
    return dataclasses.replace(base, name="pi05_rm65_dual_lora", model=model, data=data,
        freeze_filter=model.get_freeze_filter(), ema_decay=None, pytorch_weight_path=None,
        assets_base_dir=str(root / "assets"), checkpoint_base_dir=str(root / "checkpoints"),
        wandb_enabled=False, fsdp_devices=1)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openpi-root", type=Path, default=Path("/data2/dh/openpi"))
    parser.add_argument("--checkpoint", type=Path, help="已完成步数目录，或包含步数目录的实验目录")
    parser.add_argument("--dataset", type=Path, default=Path("/data2/dh/openpi/datasets/rm65_dual_aligned"))
    parser.add_argument("--episode", type=int, default=0)
    parser.add_argument("--frame", type=int, default=0, help="episode内从0开始的帧索引")
    parser.add_argument("--front-image", type=Path)
    parser.add_argument("--wrist-image", type=Path)
    parser.add_argument("--state", type=float, nargs=7, metavar="VALUE", help="q1..q6弧度 + gripper_opening(0~1)")
    parser.add_argument("--prompt", help="覆盖数据集任务；图片模式必须填写")
    parser.add_argument("--asset-id", help="通常自动从checkpoint/assets目录确定")
    parser.add_argument("--gpu", default="0", help="物理GPU编号，例如0；cpu仅用于无GPU环境调试")
    parser.add_argument("--num-steps", type=int, default=10, help="模型采样去噪步数，不是训练步数或动作长度")
    parser.add_argument("--output", type=Path, help="新建输出目录，已有目录不覆盖")
    parser.add_argument("--prepare-only", action="store_true", help="只准备真实输入，不导入JAX/加载模型/生成预测")
    args = parser.parse_args(argv)
    if args.episode < 0 or args.frame < 0 or args.num_steps <= 0:
        parser.error("episode/frame必须非负，num-steps必须为正")
    if args.gpu != "cpu" and not args.gpu.isdigit():
        parser.error("--gpu必须为单个GPU编号或cpu")
    manual = (args.front_image is not None, args.wrist_image is not None, args.state is not None)
    if any(manual) and (not all(manual) or not args.prompt or not args.prompt.strip()):
        parser.error("图片输入需同时提供--front-image、--wrist-image、--state和--prompt")
    if not args.prepare_only and args.checkpoint is None:
        parser.error("推理必须指定--checkpoint；只准备输入可使用--prepare-only")
    return args


def main(argv=None):
    args = parse_args(argv)
    # 必须在任何JAX/openpi导入前选择设备，不复用遥操作的vr环境。
    os.environ["CUDA_VISIBLE_DEVICES"] = "" if args.gpu == "cpu" else args.gpu
    if args.gpu == "cpu":
        os.environ["JAX_PLATFORMS"] = "cpu"
    os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    import numpy as np

    checkpoint = asset_id = None
    if args.checkpoint is not None:
        checkpoint, asset_id = select_checkpoint(args.checkpoint, args.asset_id)
    if args.front_image is not None:
        observation = validate_observation({"observation/state": args.state,
            "observation/image": rgb_image(args.front_image.expanduser()),
            "observation/wrist_image": rgb_image(args.wrist_image.expanduser()), "prompt": args.prompt})
        source = {"kind": "images", "front_image": str(args.front_image), "wrist_image": str(args.wrist_image)}
    else:
        observation, source = dataset_observation(args.dataset, args.episode, args.frame, args.prompt)
    root = args.openpi_root.expanduser().resolve()
    if not args.prepare_only and not (root / "src/openpi/policies/policy_config.py").is_file():
        raise FileNotFoundError(f"--openpi-root不正确：{root}")
    output = (args.output or Path("inference_outputs") / datetime.now().strftime("rm65_%Y%m%d_%H%M%S_%f")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(output / "observation.npz", **observation)
    metadata = {"source": source, "checkpoint": str(checkpoint) if checkpoint else None, "asset_id": asset_id,
        "config": "pi05_rm65_dual_lora", "prompt": observation["prompt"],
        "state": observation["observation/state"].tolist(), "action_names": ACTION_NAMES,
        "action_semantics": "absolute joint targets in radians; absolute gripper opening 0..1; no extra delta addition",
        "model_loaded": False, "prepared_only": args.prepare_only}
    print(f"输入：{source}\n任务：{observation['prompt']}\n状态：{observation['observation/state'].tolist()}", flush=True)
    if args.prepare_only:
        (output / "input.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"输入已保存：{output}；未加载模型或生成预测。")
        return 0
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(1, str(root / "packages/openpi-client/src"))
    import jax
    from openpi.policies import policy_config

    devices = jax.devices()
    expected = "cpu" if args.gpu == "cpu" else "gpu"
    if len(devices) != 1 or devices[0].platform != expected:
        raise RuntimeError(f"期望一张{expected}设备，实际为{devices}，请检查conda pi05环境")
    config = build_config(root, asset_id)
    print(f"Checkpoint：{checkpoint}\n归一化统计：{asset_id}\n设备：{devices}\n正在加载模型……", flush=True)
    policy = policy_config.create_trained_policy(config, checkpoint,
        sample_kwargs={"num_steps": args.num_steps}, default_prompt=observation["prompt"])
    print("正在推理；首次调用包含JAX编译，可能较慢……", flush=True)
    started = time.perf_counter()
    prediction = policy.infer(observation)
    elapsed_ms = (time.perf_counter() - started) * 1000
    actions = np.asarray(prediction["actions"], dtype=np.float32)
    if actions.shape != (config.model.action_horizon, 7) or not np.isfinite(actions).all():
        raise ValueError(f"预测动作无效：shape={actions.shape}，预期({config.model.action_horizon},7)且全部有限")
    metadata.update(model_loaded=True, action_shape=list(actions.shape), num_steps=args.num_steps,
        total_infer_ms=elapsed_ms, policy_timing=prediction.get("policy_timing"),
        gripper_out_of_range_count=int(np.count_nonzero((actions[:, 6] < 0) | (actions[:, 6] > 1))),
        actions=actions.tolist())
    np.save(output / "actions.npy", actions, allow_pickle=False)
    (output / "prediction.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    np.set_printoptions(precision=6, suppress=True)
    print(f"动作shape：{actions.shape}；总耗时：{elapsed_ms:.1f} ms\n{actions}\n结果已保存：{output}", flush=True)
    print("输出仅为模型预测，尚未接入机械臂执行；没有裁剪预测或额外加回关节状态。", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, ImportError) as exc:
        print(f"推理失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
