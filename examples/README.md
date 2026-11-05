# 按功能运行示例

以下命令在仓库根目录执行，也可在PyCharm中直接运行对应脚本。

| 功能 | 脚本 | 配置目录 |
|---|---|---|
| 遥操、校准、预览、设备配置 | `teleop/teleop.py` | `teleop/config/` |
| 遥操＋双相机示范采集 | `recording/record_dual.py` | `recording/config/`，共用`teleop/config/` |
| 仅双相机拍摄 | `recording/capture_dual.py` | `recording/config/` |
| 本地接收云端预测、规划并控制机械臂 | `inference/infer.py` | `inference/config/`，共用机器人与相机配置 |
| 从文件发送观测、查看预测 | `inference/request_policy.py` | 使用命令行参数 |
| 检查采集记录、转换训练数据集 | `convert/convert_dataset.py` | `convert/config/` |

`_bootstrap.py`是共用的源码导入辅助文件，不是运行入口。
云端模型加载和服务入口位于`tools/infer/`。

## 配置归属

| 文件 | 所在目录 | 设置 |
|---|---|---|
| `realman_teleop.json` | `teleop/config/` | RM65地址、固件、工具与负载预检 |
| `realman_follow.json` | `teleop/config/` | 腕部相机序列号、限速、工作范围、当前Quest会话方向校准 |
| `camera_mount.json` | `teleop/config/` | 当前支架实际手眼配置 |
| `devices.json` | `teleop/config/` | configure命令生成的Quest设备选择 |
| `cameras_dual.json` | `recording/config/` | front/wrist相机序列号与采集参数 |
| `infer.json` | `inference/config/` | 云端地址、任务、时长、预测有效条件及共用配置引用 |
| `conversion_config.json` | `convert/config/` | 转换输入、输出、数据集标签及检查/导出模式 |

机器人、工具、运动限制和校准由遥操配置统一维护；采集和推理直接引用这份配置。
不要为每种功能复制一份机器人配置，否则修改TCP或限速后容易不一致。
推理JSON中的三个配置路径相对`infer.json`所在目录解释，程序不依赖启动终端所在目录。

实际JSON被Git忽略，公共模板位于`src/common/defaults/`并随Python包安装。
首次使用时执行：

```bash
python examples/teleop/teleop.py init-config
```

该命令按以上目录创建缺失配置，不覆盖已有值。随后填写真实相机序列号、云端地址和当前手眼配置，并进行方向校准。
相机支架模板仅供参数格式参考，实际使用前需核对当前安装并完成标定。

可用`REALMAN_PROJECT_ROOT`指定其他工作目录；配置仍按`examples/功能/config/`布局创建。
运行记录写入工作目录的`run/`，不会写进示例目录。
