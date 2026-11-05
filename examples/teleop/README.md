# 遥操

从仓库根目录运行：

```bash
python examples/teleop/teleop.py --help
python examples/teleop/teleop.py init-config
python examples/teleop/teleop.py doctor
python examples/teleop/teleop.py monitor --seconds 15
python examples/teleop/teleop.py calibrate --seconds 180
python examples/teleop/teleop.py preview --seconds 10
python examples/teleop/teleop.py run
```

`doctor`只做离线检查；`monitor`读取手柄；`calibrate`读取设备进行方向校准；
`preview`读取真实反馈但不下发运动；`run`在预检通过后允许右握柄控制真机。

机器人、运动与相机安装配置位于本目录的`config/`。
采集和推理共用其中的机器人与运动配置。完整操作见[遥操说明](../../docs/teleoperation.md)。
