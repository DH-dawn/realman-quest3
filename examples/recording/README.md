# 采集

从仓库根目录运行：

```bash
python examples/recording/capture_dual.py --help
python examples/recording/capture_dual.py --seconds 5
python examples/recording/record_dual.py --help
python examples/recording/record_dual.py --task "pick the bottle and place in the light brown box"
```

`capture_dual.py`仅采集双相机图像，不连接机械臂。
`record_dual.py`执行真机遥操并按任务录制示范，需要已完成方向校准。

双相机配置在`config/cameras_dual.json`；机器人与运动配置共用`../teleop/config/`。
腕部相机序列号应与`realman_follow.json`中的`camera_serial`一致。
首次使用先在仓库根目录运行`python examples/teleop/teleop.py init-config`。

图像和数据写入仓库工作目录的`run/`，详细结构见[采集说明](../../docs/teleoperation.md)。
