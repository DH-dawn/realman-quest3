# 双相机示范数据转换

入口为`src/convert/convert_dual_to_lerobot.py`，从完整episode读取图像、实际状态及已发送动作，生成原生LeRobot v2.1数据集。
日常也可使用[examples转换入口](../../examples/convert/README.md)，其功能直接调用本模块，配置放在examples对应目录。
转换不连接机器人，不加载模型，不修改原始记录或覆盖已有输出。
数据转换和云端训练使用独立于机器人vr环境的数据依赖，固定版本见`requirements-server-data.txt`。

## 环境

在项目根目录准备单独的数据环境，避免改变机械臂控制环境的NumPy版本：

```bash
conda create -n realman-data python=3.10
conda activate realman-data
python -m pip install -r src/convert/requirements-server-data.txt
```

正式导出还需要openpi配套的LeRobot读取与导出接口：

```bash
python -m pip install "lerobot @ git+https://github.com/huggingface/lerobot.git@0cf864870cf29f4738d3ade893e6fd13fbd7cdb5"
python -m pip install -r src/convert/requirements-server-data.txt
```

`--dry-run`不需要LeRobot，不创建输出数据集。

## 运行

```bash
# 仅检查数据；把路径替换为实际session或episodes目录
python src/convert/convert_dual_to_lerobot.py --input /实际路径/session/episodes --dry-run

# 正式转换，输出目录不能预先存在
python src/convert/convert_dual_to_lerobot.py \
  --input /实际路径/session/episodes \
  --output /实际路径/datasets/rm65_dual_aligned \
  --repo-id rm65_dual_aligned
```

无参数运行会读取同目录conversion_config.json，公共配置默认为input=null、dry_run=true。
先填写输入路径并检查，再明确选择正式转换。

固定使用datasets 3.6.0、NumPy 1.26.4、PyArrow 21及openpi配套的LeRobot v2.1，不在机器人vr环境内降级NumPy。
请为数据转换单独准备Python环境，并安装本目录的依赖清单。

仅保留task_success、recording_complete、camera_complete均为true且包含真实机器人数据的轨迹。
输出包含meta、data以及conversion_report.json；图像嵌入parquet。
front/wrist处理为RGB 224×224，state为六关节弧度及夹爪开度0~1。
默认sent-target标签取当前与下一帧之间最后的已发送关节目标和夹爪指令；缺帧会分段，不直接删除后拼接。
可显式选择next-state监督，但不要混用两类动作标签。

整个输出数据集一起上传云端后，按`tools/train/README.md`运行check、norm和train。
