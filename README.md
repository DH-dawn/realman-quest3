# RealMan Quest

Meta Quest 3 / XRoboToolkit 驱动RM65遥操、双D405示范采集，以及云端π0.5策略的本地执行。
目标环境：Ubuntu 22.04 x86_64、Python 3.10。

## 目录结构

```text
realman-quest/
├── pyproject.toml                 # Python包与命令入口
├── src/                          # 本地控制、输入、采集、规划、传输等模块
│   ├── teleop/                   # 遥操、校准与统一命令入口
│   ├── recording/                # 示范录制、双相机采集与数据对齐
│   ├── inference/                # 本地云端策略执行
│   ├── vr/                       # Quest输入和客户端安装
│   ├── transport/                # 机器人反馈、运动与夹爪通信
│   ├── common/                   # 配置路径、环境检查和defaults/公共模板
│   └── convert/                  # 本地示范数据转换
├── examples/                     # 按功能分组的入口和本机配置
│   ├── teleop/                   # 遥操，config/保存共用机器人与运动配置
│   ├── recording/                # 采集，config/保存双相机配置
│   ├── inference/                # 本地推理，config/保存云端策略配置
│   └── convert/                  # 数据转换，config/保存转换参数
├── scripts/                      # 原生服务、客户端安装与启动工具
├── Android_APK/                  # 本地存放Quest APK及PC Service安装包，附安装说明
├── tools/                        # 整个目录放到云端openpi根目录
│   ├── train/                    # 云端LoRA训练
│   └── infer/                    # 云端模型服务及单帧推理
├── docs/                         # 分功能使用说明
├── third_party/quest_client/     # Quest客户端源码补丁与版本引用
├── licenses/                     # 第三方许可证
└── THIRD_PARTY.md
```

数据集、图像、权重、运行日志、虚拟环境、SDK二进制、DEB、APK和安装缓存不在源码交付范围内。
`Android_APK/`可以在本机存放安装包，其README参与源码分发，二进制文件被Git忽略。
`.gitignore`排除本机配置、运行数据和安装缓存等生成物。

## 安装与首次配置

在项目根目录创建并激活Python 3.10环境：

```bash
conda create -n vr python=3.10
conda activate vr
python -m pip install -e ".[infer]"
python examples/teleop/teleop.py init-config
```

已有所需依赖的`vr`环境可直接激活，并使用`python -m pip install --no-deps -e .`登记项目。
本地Python依赖统一维护在`pyproject.toml`，兼容当前使用的SDK 1.1.6及RealSense 2.59。
`tools/`中的依赖清单用于云端训练、推理；本地数据转换环境见`src/convert/README.md`。
`init-config`只创建缺失文件，不覆盖已有设备配置或校准。
`doctor`完全离线：不连接机械臂、不启动头显SDK、不枚举相机、不启动PC Service。
系统依赖和原生运行库获取方法见`docs/native_runtime.md`。

初始化配置后，填写实际机械臂地址、相机序列号、云端地址和手眼配置，并进行Quest会话方向校准。
配置模板位于`src/common/defaults/`，实际配置保存在`examples/`下对应功能的`config/`目录。
参数说明见`examples/README.md`。

## Quest安装与USB连接

先按[安装包目录说明](Android_APK/README.md)准备Quest专用APK和PC Service。
Quest开发者模式、USB调试授权、安装APK、`adb reverse`映射与头显设置的完整步骤见
[Quest USB ADB连接指南](docs/quest_usb_adb.md)。

从仓库根目录运行：

```bash
adb devices -l
# 按安装包目录说明准备Quest客户端APK
python examples/teleop/teleop.py install-quest
# 头显PC Service IP设为127.0.0.1，Controller模式并开启Send
python examples/teleop/teleop.py monitor --seconds 15
```

`.deb`用于安装Ubuntu电脑端服务，`.apk`用于安装Quest客户端。
安装后日常运行monitor/calibrate/preview/run时，启动器会按需启动PC Service并自动重建USB映射。

## 常用入口

所有命令从仓库根目录运行，`--help`只显示参数。
示例会自动加入本仓库的`src/`，也可不安装包直接使用当前已具备依赖的Python环境运行。

```bash
# 只检查手柄，不连接机械臂
python examples/teleop/teleop.py monitor --seconds 15

# 方向校准：只读取设备，不驱动机械臂
python examples/teleop/teleop.py calibrate --seconds 180

# 遥操预览：读取机械臂与手柄，不发送运动指令
python examples/teleop/teleop.py preview --seconds 10

# 真机遥操：右Grip离合，Trigger抓放
python examples/teleop/teleop.py run

# 真机遥操＋双相机分任务采集
python examples/recording/record_dual.py --task "pick the bottle and place in the light brown box"

# 仅双相机保存图像，不连接机械臂
python examples/recording/capture_dual.py --seconds 5

# 检查采集记录，用数据转换环境运行
python examples/convert/convert_dataset.py --input /实际路径/session/episodes --dry-run

# 本地云端策略配置检查/只读预览
python examples/inference/infer.py --check-only
python examples/inference/infer.py --seconds 10

# 允许云端策略驱动真机，不需要Quest或握柄
python examples/inference/infer.py --execute --seconds 10
```

安装后的等价命令为`realman-quest ...`、`realman-infer ...`，也支持`python -m teleop ...`。
本地推理使用`examples/inference/config/infer.json`；遥操和采集使用`examples/teleop/config/realman_follow.json`等配置。
运行时长、限制和IP以`--check-only`及实际配置为准。

## 系统分工与数据位置

```text
Quest → XRoboToolkit PC Service → 笔记本遥操/采集 → RM65 192.168.0.18:8080
双D405 → 笔记本策略入口 ⇄ WebSocket ⇄ 云端π0.5服务
                        ↓
                 本地规划 → RM65
```

服务器与机器人可以在不同网段，由笔记本应用层中转；无需SSH端口转发或系统网桥。
RM65采用50Hz TCP JSON `movej_follow`，遥操使用官方离线运动学；策略输入为绝对关节目标，不再求逆解。
启动时检查硬件型号、TCP、负载和反馈状态，运行中执行跟踪、制动及退出保护。
同一机械臂只运行一个控制程序；不同工作目录的设备文件锁不共享。

采集照片位于`run/follow_dual_时间/episodes/episode_编号/cameras/{front,wrist}/rgb/`；
深度图在各相机的`depth_raw/`。策略日志位于`run/infer_时间/`，推理入口默认不保存照片。

## 云端部署

将本项目的整个`tools/`目录复制到云端openpi项目根目录，例如`/data2/dh/openpi/`。
最终位置应为`/data2/dh/openpi/tools/train/`和`/data2/dh/openpi/tools/infer/`。
`tools/`中的代码在云端的openpi训练环境中运行，不依赖笔记本的`src/`目录。
部署结构和运行顺序见[云端工具说明](tools/README.md)。

本地数据转换示例为`examples/convert/convert_dataset.py`，调用`src/convert/convert_dual_to_lerobot.py`；转换后的完整数据集再上传到云端。

## 详细说明

- `docs/source_layout.md`：源码模块归属和调用关系。
- `docs/native_runtime.md`：PC Service、SDK和Quest专用客户端。
- `docs/quest_usb_adb.md`：Quest安装、USB ADB连接与常见问题。
- `docs/teleoperation.md`：连接、校准、遥操及双摄录制。
- `docs/inference.md`：本地策略、时间对齐、日志和退出条件。
- `src/convert/README.md`：数据转换。
- `tools/README.md`：云端目录放置与部署。
- `tools/train/README.md`：云端训练。
- `tools/infer/README.md`：云端连续推理。
- `docs/single_inference.md`：单帧推理与输入文件准备。

## 许可证

Copyright (C) 2026 DH-dawn.

本项目原创代码采用 **GNU Affero General Public License v3.0（AGPL-3.0-only）**，完整条款见[LICENSE](LICENSE)。
第三方组件及其修改文件遵循各自适用的许可证，具体说明见[THIRD_PARTY.md](THIRD_PARTY.md)和`licenses/`。

## 第三方组件

依赖组件和许可证说明见`THIRD_PARTY.md`及`licenses/`。
