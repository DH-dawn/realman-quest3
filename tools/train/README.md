# RM65 双相机 π0.5 LoRA 训练

将项目的整个`tools/`目录放到云端openpi根目录，最终应包含`tools/train/`和`tools/infer/`。
总览见[云端工具说明](../README.md)。

训练代码在笔记本中的位置：`/home/dh/API2_Demo/realman-quest/tools/train/`。
上传到云端后的部署位置：`/data2/dh/openpi/tools/train/`。

```text
tools/train/
├── server_train.py   # check / norm / train，注册RM65训练配置并调用openpi
├── requirements.txt  # 原生LeRobot v2.1数据读取依赖的固定版本
└── README.md         # 部署、检查、归一化、训练、恢复和日志
```

数据转换代码在`/home/dh/API2_Demo/realman-quest/src/convert/`。
云端连续推理代码在`/home/dh/API2_Demo/realman-quest/tools/infer/`，与训练目录分开。

## 1. 环境与文件准备

本脚本需要放在openpi项目中，使用训练时的Python/JAX环境，不是独立安装包。
当前云端项目位于`/data2/dh/openpi`，现场使用已有`.venv`：

```bash
cd /data2/dh/openpi
source /data2/dh/openpi/.venv/bin/activate
```

使用Conda时激活训练环境，例如`conda activate pi05`，二者选实际使用的一种。
不要在笔记本控制机械臂的`vr`环境中安装云端训练依赖。

数据读取与导出使用原生LeRobot v2.1，固定依赖版本记录在本目录`requirements.txt`：

```text
numpy==1.26.4
pyarrow==21.0.0
datasets==3.6.0
fsspec==2025.3.0
dill==0.3.8
multiprocess==0.70.16
```

它只记录数据相关依赖，不重新安装或升级JAX、Torch和openpi。已有正确版本时不用重复安装；
缺少这些数据依赖或版本不一致时，在云端训练环境执行：

```bash
python -m pip install -r /data2/dh/openpi/tools/train/requirements.txt
```

该脚本使用LeRobot v2.1读取器，不直接升级为LeRobot v3或datasets 4.x。
脚本要求数据使用Sequence特征；datasets 4.x的List特征不满足读取要求。

## 2. 放置云端目录

将整个`tools/`文件夹复制到云端openpi根目录，例如`/data2/dh/openpi/`。
训练入口的最终路径为`/data2/dh/openpi/tools/train/server_train.py`。
无需复制笔记本的`src/`，数据集和基础模型需要单独准备。

在云端准备好：

```text
/data2/dh/openpi/
├── scripts/train.py
├── scripts/compute_norm_stats.py
├── src/openpi/
├── models/pi05_base/params/
├── datasets/rm65_dual_aligned/
│   ├── meta/
│   ├── data/
│   └── conversion_report.json
└── tools/train/
    ├── server_train.py
    ├── requirements.txt
    └── README.md
```

上传数据时保留完整`meta/`、`data/`及转换报告，不只上传parquet或图片。
基础权重为JAX π0.5 base checkpoint，`--model`指向包含`params/`的父目录。

## 3. 检查训练链路

以下命令在云端训练环境运行，显式指定转换后的`rm65_dual_aligned`数据集。
示例使用物理GPU 7；使用其他GPU时替换每条命令的`--gpu`。

```bash
python /data2/dh/openpi/tools/train/server_train.py check \
  --gpu 7 \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned
```

`check`检查数据结构、全部episode文件是否齐全、转换报告与依赖版本，
并用openpi训练读取器检查第一条样本、任务文字、图像适配、动作序列、tokenization和padding。
它会初始化JAX GPU和数据变换，但不加载完整模型参数、不做梯度训练、不保存checkpoint。
需要基础权重目录存在。看到`Training data pipeline OK`后继续下一步。

也可先查看帮助，此操作不读取数据、不初始化GPU：

```bash
python /data2/dh/openpi/tools/train/server_train.py --help
```

## 4. 计算归一化统计

```bash
python /data2/dh/openpi/tools/train/server_train.py norm \
  --gpu 7 \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned
```

脚本在当前进程注册RM65配置，再调用openpi的归一化入口，不修改官方配置源码。
输出为：

```text
/data2/dh/openpi/assets/pi05_rm65_dual_lora/rm65_dual_aligned/norm_stats.json
```

必须使用当前数据集与训练变换计算统计。修改数据集、动作含义或训练变换后，重新核对统计；
不要把其他任务或LIBERO数据的统计直接用于本任务。

## 5. 训练与断点恢复

首次检查整条训练链路，可用一个新的实验名运行20步：

```bash
python /data2/dh/openpi/tools/train/server_train.py train \
  --gpu 7 \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned \
  --steps 20 --batch-size 4 --experiment rm65_pipeline_check
```

20步用于检查权重加载、编译、反向传播和保存，不代表模型任务效果。
不同训练任务使用不同实验名，避免混用训练输出。

正式训练示例：

```bash
python /data2/dh/openpi/tools/train/server_train.py train \
  --gpu 7 \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned \
  --batch-size 4 --steps 10000 --experiment rm65_dual_v1
```

W&B默认关闭，训练指标打印到终端；已有同名实验不会自动覆盖。
显存不足时可选择更小的batch，例如`--batch-size 2`，并使用不同实验名区分设置。

中断后恢复，在同一个实验名下添加`--resume`，保持数据、batch、总步数和模型配置一致：

```bash
python /data2/dh/openpi/tools/train/server_train.py train \
  --gpu 7 \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned \
  --batch-size 4 --steps 10000 --experiment rm65_dual_v1 --resume
```

20步链路检查与10000步训练应使用不同实验名；总步数影响学习率计划。
恢复需要完整训练checkpoint，包括训练状态；仅有推理用`params/`不足以恢复全部训练状态。

## 6. 参数与训练配置

| 参数 | 默认值/含义 |
|---|---|
| `stage` | 必填，`check`、`norm`或`train` |
| `--openpi-root` | `/data2/dh/openpi`，官方源码、assets与checkpoints根目录 |
| `--dataset` | 默认`/data2/dh/openpi/datasets/rm65_dual`；示例显式指定`rm65_dual_aligned` |
| `--model` | `/data2/dh/openpi/models/pi05_base`，包含`params/`的基础模型目录 |
| `--gpu` | `0`，示例显式选`7`；必须是单个物理GPU编号 |
| `--batch-size` | `4` |
| `--steps` | `10000`，本次总训练步数 |
| `--workers` | `2`，数据读取worker数量；允许0 |
| `--experiment` | `rm65_dual_v1`，只填目录名，不填路径 |
| `--resume` | 恢复同名实验；默认不恢复、不覆盖 |

模型基于openpi的`pi05_libero`配置，复用LIBERO的双图像字段适配器，基础权重为π0.5 base：

- 配置名为`pi05_rm65_dual_lora`，PaliGemma与action expert均使用LoRA。
- 每条状态/动作7维，动作序列长度继承模型配置，当前为10；图像为front/wrist RGB 224×224。
- 前6维是按控制器J1~J6顺序排列的关节角弧度，夹爪维是0~1的开度。
- 数据存绝对关节目标；训练变换对前6维减去当前关节角，夹爪保持绝对开度。
- 输出策略会把前6维加回，推理返回绝对目标；本地执行器不能再次叠加当前关节角。
- 学习率计划峰值`5e-5`、末值`5e-6`，warmup随总步数计算并最多1000步。
- 训练配置只在当前进程注册，`overwrite=False`、`ema_decay=None`、`fsdp_devices=1`。

脚本设置`HF_LEROBOT_HOME`为数据集父目录，使用数据集文件夹名作为repo_id，
不要求上传Hugging Face。GPU选择在导入JAX前生效，不结束其他GPU上的进程。

## 7. 输出权重和训练日志

checkpoint目录为：

```text
/data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/实验名/步数/
├── params/        # 该步模型权重，内部文件需完整保留
├── assets/        # 配套归一化统计
└── train_state/   # 断点恢复所需训练状态
```

数字目录标识训练步数，例如`5000/params/`是5000步的整组分片权重，没有单独的5000.pth。
尚在保存的`*.orbax-checkpoint-tmp-*`不是已完成checkpoint；等待保存完成后再用于推理。
保存频率和保留策略继承openpi的基础配置，不是每一步都生成一份完整目录。

默认训练日志显示在终端。需要保存文件时，在云端运行：

```bash
mkdir -p /data2/dh/openpi/tools/train/logs
set -o pipefail
python -u /data2/dh/openpi/tools/train/server_train.py train \
  --gpu 7 \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned \
  --batch-size 4 --steps 10000 --experiment rm65_dual_v1 \
  2>&1 | tee /data2/dh/openpi/tools/train/logs/train_$(date +%Y%m%dT%H%M%S).log
```

此命令与普通训练启动二选一，不要对同一实验同时启动两次；恢复时同样增加`--resume`。
训练loss、grad_norm和参数范数可用于观察训练过程，不能单独证明真机任务成功率。

## 8. 与推理目录的衔接

云端常驻推理入口为`/data2/dh/openpi/tools/infer/server.py`，
笔记本侧源码和说明在`/home/dh/API2_Demo/realman-quest/tools/infer/README.md`。
它使用与本训练脚本相同的模型与数据变换，直接从选定checkpoint加载统计。
推理时显式选已完成的数字步数目录，例如：

```bash
python /data2/dh/openpi/tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_dual_v1/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7
```

样例步数目录必须实际存在。不要只把`pi05_rm65_dual_lora`名称传给未注册该配置的原始`serve_policy.py`。
单帧推理入口与说明在`/home/dh/API2_Demo/realman-quest/tools/infer/single.py`
和`/home/dh/API2_Demo/realman-quest/docs/single_inference.md`；真机反馈与控制由本项目的`examples/inference/infer.py`进入。

## 9. 常见问题

| 情况 | 检查 |
|---|---|
| 找不到`server_train.py` | 检查部署目录`/data2/dh/openpi/tools/train/server_train.py`和启动命令 |
| `Dataset was exported with datasets 4.x` | 使用原生datasets 3.6.0重新转换的aligned数据，不修改读取库伪装List特征 |
| `Dataset upload incomplete` | 核对全部episode parquet、meta及conversion_report是否一起上传 |
| `Run norm first` | 先对相同数据集运行`norm`，核对assets目录 |
| JAX看不到一张CUDA GPU | 在训练环境核对JAX/CUDA及`--gpu`；进程内GPU 0可能对应指定的物理GPU 7 |
| 已有实验目录或无法恢复 | 新训练使用新实验名；恢复使用原实验名及`--resume`，不要手动删除训练状态 |
| 推理关节单位或方向异常 | 检查实际输入顺序、弧度、工具/相机配置及绝对目标解释，不重复做delta叠加 |

