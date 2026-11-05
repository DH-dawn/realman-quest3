# π0.5 RM65 双相机推理

把`/home/dh/API2_Demo/realman-quest/tools/infer/single.py` 上传到服务器 `/data2/dh/openpi/tools/infer/single.py`。
使用训练时的 `conda pi05`，不需要修改 openpi 源码、安装新环境或上传额外训练配置。
训练入口为`tools/train/server_train.py`，部署到云端后为
`/data2/dh/openpi/tools/train/server_train.py`，说明见[训练README](../tools/train/README.md)。
本单帧推理脚本按该训练入口的设置重建双 LoRA、LIBERO 图像适配和绝对关节动作变换，
直接传配置对象加载策略，不依赖原始 `serve_policy.py` 认识自定义配置名称。

## 从数据集选一帧推理

```bash
conda activate pi05
cd /data2/dh/openpi
python /data2/dh/openpi/tools/infer/single.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned \
  --episode 0 --frame 0 --gpu 0
```

传入实验目录会选择其中已完成保存的最大数字步数目录；正在保存的
`*.orbax-checkpoint-tmp-*` 不参与选择。也可明确指定某一步：

```bash
python /data2/dh/openpi/tools/infer/single.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/3000 \
  --episode 0 --frame 0 --gpu 0
```

指定步数必须确实存在且已保存完成。训练可能清理旧检查点；不存在时应选择仍存在的目录。
脚本从该checkpoint的`assets/<asset_id>/norm_stats.json`加载训练归一化统计，
不使用 pi05_base 权重冒充训练结果，也不重新计算归一化统计。

默认任务从该数据帧的 `task_index` 对应的 `meta/tasks.jsonl` 读取，可用
`--prompt "pick the bottle and place in the light brown box"` 覆盖。
模型配置的动作长度为10，运行时以训练模型配置的`action_horizon`为准。
`--num-steps` 是模型去噪步数，不是动作长度或训练步数，默认10。

## 用图片与当前关节状态推理

```bash
python /data2/dh/openpi/tools/infer/single.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke \
  --front-image /path/to/front.png \
  --wrist-image /path/to/wrist.png \
  --state -0.0969 -0.0450 1.7586 0.0717 1.1553 -0.1677 1.0 \
  --prompt "pick the bottle and place in the light brown box" \
  --gpu 0
```

上面的state仅为一条数据样例，应替换为与两张图对应的当前真实状态。
前6维为关节角弧度，最后一维为归一化夹爪开度：0闭合、1张开，不能直接填角度或0~1000原始数值。
图像按采集时的front/wrist角色提供，脚本采用与数据转换相同的RGB、等比例缩小和黑边补齐到224×224。
它不连接相机、不自动采集当前机器人状态。

## 输出

默认新建 `inference_outputs/rm65_时间/`，也可用`--output 新目录`指定，已有目录不覆盖。

- `observation.npz`：输入的两路RGB图、7维状态及任务指令，可用`np.load(..., allow_pickle=False)`读取。
- `actions.npy`：`[action_horizon, 7]`预测动作序列。
- `prediction.json`：完整动作、所用检查点、输入来源、状态及耗时。

前6维输出已经恢复为绝对关节角目标（弧度），最后一维为绝对夹爪开度；不要再次加上当前关节角。
脚本检查形状和有限值，不裁剪预测，也不连接或下发机械臂。
首次推理包含JAX编译；显示的总耗时不等于后续稳定推理延迟。
从训练集抽一帧只能检查推理链路，不能据此评估泛化或实际操作成功率。

## 只准备输入，不加载模型

```bash
python /data2/dh/openpi/tools/infer/single.py --prepare-only \
  --dataset /data2/dh/openpi/datasets/rm65_dual_aligned --episode 0 --frame 0
```

该模式不需要checkpoint、JAX或GPU，只输出`observation.npz`与`input.json`，不会生成假预测。

## 云端常驻服务与本地通信

连续推理代码位于工作区根目录的 `tools/infer/README.md`。
将整个`infer/`上传到云端`/data2/dh/openpi/tools/infer/`，运行`python tools/infer/server.py`。
该目录包含模型加载、WebSocket服务、本地异步客户端与配置；单帧推理入口为`tools/infer/single.py`。
