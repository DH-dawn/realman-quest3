# 源码目录与调用关系

`src/`是Python源码目录，下面直接存放按功能划分的软件包。
日常从`examples/`启动，或使用安装后的`realman-quest`、`realman-infer`命令。

| 目录 | 文件 | 用途 |
|---|---|---|
| `teleop/` | `cli.py`、`native_launcher.py` | 命令解析、启动PC Service、ADB转发与运行模式分派 |
| `teleop/` | `realman_follow.py` | 6D握柄跟随，以及采集和策略复用的运动学、连续关节规划、反馈检查 |
| `teleop/` | `realman_teleop.py` | 遥操执行器、机器人配置、基础RPC、预检、运动许可与停止流程 |
| `teleop/` | `direction_calibration.py`、`calibrate_camera_axes.py` | 方向映射与手势校准 |
| `teleop/` | `realman_shadow.py` | 只读映射入口及数据包、反馈解析 |
| `recording/` | `follow_recording.py` | 异步写入事件日志，供遥操和推理共用 |
| `recording/` | `episode_recording.py`、`episode_session.py` | 单相机采集进程、分段录制与数据对齐 |
| `recording/` | `dual_camera_recording.py`、`realman_follow_dual.py` | 双相机采集进程、双相机遥操示范录制 |
| `inference/` | `run_policy.py` | 本地云端策略运行、观测、预测采用与真机执行 |
| `inference/` | `client.py`、`protocol.py` | WebSocket客户端、数据编码与协议校验 |
| `inference/` | `planner.py`、`cameras.py` | 策略时间对齐、关节目标规划与推理相机进程 |
| `vr/` | `xrobotoolkit_input.py`、`quest_client.py` | Quest手柄读取、输入采样及专用客户端安装 |
| `transport/` | `follow_transport.py`、`gripper_transport.py` | 独立反馈/运动通道和夹爪命令通道 |
| `common/` | `paths.py`、`device_config.py`、`runtime_paths.py` | 工作路径、配置初始化、设备选择与离线环境检查 |
| `common/defaults/` | 公共JSON模板 | 安装包内的配置资源，由init-config复制到examples对应目录 |
| `convert/` | `convert_dual_to_lerobot.py` | 本地将采集记录转换成LeRobot训练数据集 |

各功能目录的`__init__.py`标记软件包，不在导入时启动设备。
`teleop/__main__.py`提供`python -m teleop`入口，`inference/__main__.py`提供`python -m inference`入口。

## 调用关系

```text
examples/teleop/teleop.py
  → teleop.cli → teleop.native_launcher
    → vr.xrobotoolkit_input / teleop.calibrate_camera_axes / teleop.realman_follow

examples/recording/record_dual.py
  → teleop.native_launcher → recording.realman_follow_dual
    → teleop.realman_follow + recording的相机和分段录制模块

examples/recording/capture_dual.py
  → recording.dual_camera_recording

examples/inference/infer.py
  → inference.run_policy
    → inference的客户端、相机、规划器
    → teleop中的共用运动学/规划/预检 + transport通信 + recording事件日志

examples/convert/convert_dataset.py
  → convert.convert_dual_to_lerobot.main
  → 读取本地采集记录 → 校验与转换 → LeRobot数据集
```

共用运动类位于`teleop/`，采集和推理通过导入复用运动学、规划和预检功能。
云端训练和模型服务单独放在`tools/`，整个目录放到云端openpi根目录即可，部署说明见[tools/README](../tools/README.md)。

## 导入与启动

安装项目或将`src/`加入Python搜索路径后，其他脚本可直接导入功能包，例如：

```python
from teleop.realman_follow import FollowConfig
from recording.dual_camera_recording import DualCameraConfig
from inference.client import PolicyClient
from common.paths import config_file
```

在项目根目录执行`python -m pip install --no-deps -e .`可登记已有依赖环境的控制台入口。
直接运行examples会自动导入当前src，无需先安装。

相机子进程使用`python -m recording.…`启动；推理相机使用可导入的
`inference.cameras`模块，适用于multiprocessing的spawn方式。
运行日志记录相关源码文件的哈希，便于确认运行代码。
