# 遥操和双相机数据采集

从仓库根目录使用Python 3.10，先完成README中的依赖、原生SDK和配置准备。
Quest首次安装与有线连接先按[USB ADB指南](quest_usb_adb.md)完成；安装包说明在[Android_APK](../Android_APK/README.md)。

## 设备配置

默认机器人192.168.0.18:8080，笔记本有线接口需能够直接访问该地址。
先在`examples/recording/config/cameras_dual.json`指定front与wrist序列号，保持腕部相机与运动配置的`camera_serial`一致。

```bash
adb devices -l
python examples/teleop/teleop.py configure --robot-ip 192.168.0.18 --camera-serial 260322270634
python examples/teleop/teleop.py check
```

`configure`读取已授权Quest、写入本机配置并清除旧方向参考；双相机时明确指定腕部序列号。
`check`会进行机器人只读查询与设备枚举，和完全离线的`doctor`不同。

## 校准、预览和执行

```bash
python examples/teleop/teleop.py monitor --seconds 15
python examples/teleop/teleop.py calibrate --seconds 180
python examples/teleop/teleop.py preview --seconds 10
python examples/teleop/teleop.py run
```

校准按提示在同一Quest会话中记录右、上、前三个方向，松Grip回位；它读取状态，不驱动机器人。
相机支架外参由`camera_mount.json`给定，手势校准只确定VR会话方向，不替代手眼标定。
头显应用重启导致PID/参考系变化时重新校准。公共配置模板不带旧会话方向。

`run`是真机模式，不回Home；Grip作为离合，松开时停止。转腕比例由`realman_follow.json`中的`rotation_scale`设置。
握Grip期间，Trigger越过0.8力控闭合、降到0.2以下张开，松Grip不会主动松爪。
单相机分段采集保留为`python examples/teleop/teleop.py record --task "任务文字"`。

## 双D405采集

```bash
python examples/recording/record_dual.py --task "pick the bottle and place in the light brown box"
```

该入口复用相同遥操控制器，是真机运动模式。右A开始任务；录制中A成功结束、B失败结束。
每个episode单独保存成功/失败标签与任务文字，Ctrl+C等待机器人停止和数据保存完成。

```text
run/follow_dual_时间/
├── session.json
├── events.jsonl
├── report.json
└── episodes/episode_000001/
    ├── episode.json
    ├── events.jsonl
    ├── samples.jsonl
    └── cameras/
        ├── front/{rgb,depth_raw}/000000.png
        └── wrist/{rgb,depth_raw}/000000.png
```

两台相机各自独立进程采集640×480@30，默认各保存10组RGB-D/秒。
原始深度为uint16 PNG，米值为原始值乘各相机calibration.json的depth_scale_m_per_unit。
两相机按本机接收时间一对一配对，默认最大40ms；机器人状态只取图像参考时刻前已完成的查询。
这是软件因果时间对齐，不是硬件曝光同步。缺帧或反馈过期会标记无效，不用重复图片凑数。
一台相机失效会进入原停止流程。recording_complete只是保存完整性，不等于训练数据已经全部有效。

## 仅采集相机

```bash
python examples/recording/capture_dual.py --list
python examples/recording/capture_dual.py --seconds 5
```

该入口不连接机器人，输出`run/dualcam_时间/`，robot_recording=false，不能当作含动作的示范数据训练。
以上入口都支持各自`--help`；`--output`指定新目录，已有目录不覆盖。
训练数据转换方法见`src/convert/README.md`。
