#!/usr/bin/env python3
"""Register the RM65 config and run openpi's JAX norm/training entry points."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import runpy
import sys


def validate_reader_environment(dataset):
    """Check native v2 reader compatibility without registering or patching types."""
    import datasets
    import pyarrow.parquet as pq

    if datasets.__version__ != "3.6.0":
        raise RuntimeError(f"This LeRobot reader requires datasets==3.6.0, found {datasets.__version__}")

    def contains_list(value):
        if isinstance(value, dict):
            return value.get("_type") == "List" or any(contains_list(v) for v in value.values())
        if isinstance(value, list):
            return any(contains_list(v) for v in value)
        return False

    for file in sorted((dataset / "data").rglob("*.parquet")):
        metadata = pq.read_schema(file).metadata or {}
        if b"huggingface" in metadata and contains_list(json.loads(metadata[b"huggingface"])):
            raise ValueError("Dataset was exported with datasets 4.x. Use the locally regenerated rm65_dual_aligned dataset.")


def validate_dataset(dataset):
    info = json.loads((dataset / "meta/info.json").read_text(encoding="utf-8"))
    if info.get("codebase_version") != "v2.1":
        raise ValueError("Expected converted LeRobot v2.1 dataset")
    if info.get("robot_type") != "realman_rm65" or info.get("fps") != 10:
        raise ValueError("Expected realman_rm65 dataset at 10 Hz")
    for key in ("state", "actions"):
        feature = info["features"][key]
        if feature["shape"] != [7] or feature["dtype"] != "float32":
            raise ValueError(f"Unexpected {key} schema: {feature}")
    for key in ("image", "wrist_image"):
        feature = info["features"][key]
        if feature["shape"] != [224, 224, 3] or feature["dtype"] != "image":
            raise ValueError(f"Unexpected {key} schema: {feature}")
    count = info["total_episodes"]
    if count <= 0 or info["total_frames"] <= 0:
        raise ValueError("Dataset is empty")
    for index in range(count):
        path = dataset / info["data_path"].format(
            episode_chunk=index // info["chunks_size"], episode_index=index)
        if not path.is_file():
            raise FileNotFoundError(f"Dataset upload incomplete: {path}")
    report = dataset / "conversion_report.json"
    if not report.is_file():
        raise FileNotFoundError("Upload conversion_report.json together with meta and data")
    audit = json.loads(report.read_text(encoding="utf-8"))
    if audit["action_source"] not in ("sent-target", "next-state") or audit["total_frames"] != info["total_frames"]:
        raise ValueError("Conversion report does not match the dataset")
    return info


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=("check", "norm", "train"))
    p.add_argument("--openpi-root", type=Path, default=Path("/data2/dh/openpi"))
    p.add_argument("--dataset", type=Path, default=Path("/data2/dh/openpi/datasets/rm65_dual"))
    p.add_argument("--model", type=Path, default=Path("/data2/dh/openpi/models/pi05_base"))
    p.add_argument("--gpu", default="0", help="One physical GPU index; sets CUDA_VISIBLE_DEVICES before JAX import")
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--steps", type=int, default=10_000)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--experiment", default="rm65_dual_v1")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()
    if args.batch_size <= 0 or args.steps <= 0 or args.workers < 0:
        p.error("Batch size / steps must be positive; workers must be nonnegative")
    if not args.gpu.isdigit():
        p.error("--gpu must select exactly one physical GPU, e.g. --gpu 0")
    if Path(args.experiment).name != args.experiment or args.experiment in ("", ".", ".."):
        p.error("Experiment must be a directory name, not a path")
    root, dataset, model_dir = (path.expanduser().resolve() for path in (args.openpi_root, args.dataset, args.model))
    for path in (root / "scripts/train.py", root / "scripts/compute_norm_stats.py"):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not (model_dir / "params").is_dir():
        raise FileNotFoundError(f"Expected JAX base checkpoint directory: {model_dir / 'params'}")
    info = validate_dataset(dataset)
    # Must precede imports of LeRobot, openpi and JAX. Dataset ID is its folder
    # name, matching HF_LEROBOT_HOME/repo_id; no Hugging Face upload is needed.
    os.environ["HF_LEROBOT_HOME"] = str(dataset.parent)
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
    sys.path.insert(0, str(root / "src"))
    os.chdir(root)

    validate_reader_environment(dataset)
    import jax
    import numpy as np
    from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
    from openpi.training import config as config_lib
    from openpi.training import optimizer, weight_loaders

    devices = jax.devices()
    if len(devices) != 1 or devices[0].platform != "gpu":
        raise RuntimeError(f"JAX must see one CUDA GPU, got {devices}. Check JAX CUDA dependencies in conda pi05.")
    # Reuse the repository's own config / policy adapter; only change dataset,
    # absolute-joint delta conversion, local weights and tuning settings.
    base = config_lib.get_config("pi05_libero")
    model = dataclasses.replace(base.model, paligemma_variant="gemma_2b_lora",
                                action_expert_variant="gemma_300m_lora")
    config = dataclasses.replace(
        base, name="pi05_rm65_dual_lora", exp_name=args.experiment,
        model=model,
        data=dataclasses.replace(base.data, repo_id=dataset.name, extra_delta_transform=True,
                                 base_config=config_lib.DataConfig(prompt_from_task=True)),
        weight_loader=weight_loaders.CheckpointWeightLoader(str(model_dir / "params")),
        pytorch_weight_path=None, freeze_filter=model.get_freeze_filter(), ema_decay=None,
        batch_size=args.batch_size, num_train_steps=args.steps, num_workers=args.workers,
        lr_schedule=optimizer.CosineDecaySchedule(warmup_steps=min(1000, max(1, args.steps // 10)),
                    peak_lr=5e-5, decay_steps=args.steps, decay_lr=5e-6),
        assets_base_dir=str(root / "assets"), checkpoint_base_dir=str(root / "checkpoints"),
        wandb_enabled=False, overwrite=False, resume=args.resume, fsdp_devices=1,
    )
    # openpi builds this lookup at import time. Register for both get_config()
    # (normalization) and cli() (training) in this process, without editing repo code.
    config_lib._CONFIGS_DICT[config.name] = config
    print(f"Python: {sys.executable}\nGPU: {devices}\nDataset: {dataset}\n"
          f"Episodes: {info['total_episodes']}, frames: {info['total_frames']}\n"
          f"Config: {config.name}, LoRA, batch={config.batch_size}, steps={config.num_train_steps}\n"
          f"Checkpoint: {config.checkpoint_dir}", flush=True)
    if args.stage == "check":
        from openpi.training import data_loader

        transformed = config.data.create(config.assets_dirs, config.model)
        # Use the training loader itself: it expands action chunks and injects
        # prompt from task_index before RepackTransform reads the prompt key.
        ds = data_loader.create_torch_dataset(transformed, config.model.action_horizon, config.model)
        sample = ds[0]
        expected = {"state": (7,), "actions": (config.model.action_horizon, 7)}
        for key, shape in expected.items():
            values = np.asarray(sample[key])
            if values.shape != shape or not np.isfinite(values).all():
                raise ValueError(f"Invalid first sample {key}: {values}")
        if not isinstance(sample.get("prompt"), str) or not sample["prompt"].strip():
            raise ValueError("Training loader did not produce a prompt from the task")
        # Norm statistics do not exist yet during check; keep every other
        # official training transform, including tokenization and padding.
        adapted = data_loader.transform_dataset(ds, transformed, skip_norm_stats=True)[0]
        for key, shape in {"state": (config.model.action_dim,),
                           "actions": (config.model.action_horizon, config.model.action_dim)}.items():
            values = np.asarray(adapted[key])
            if values.shape != shape or not np.isfinite(values).all():
                raise ValueError(f"Training adapter returned invalid {key}: {values.shape}")
        print(f"Training data pipeline OK. Prompt: {sample['prompt']}\nNext: run norm, then train.")
        return
    if args.stage == "norm":
        sys.argv = [str(root / "scripts/compute_norm_stats.py"), "--config-name", config.name]
        runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        stats = config.assets_dirs / dataset.name / "norm_stats.json"
        if not stats.is_file():
            raise FileNotFoundError(f"Run norm first; missing: {stats}")
        sys.argv = [str(root / "scripts/train.py"), config.name]
        runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
