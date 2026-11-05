# 遥操和共用机器人配置

- `realman_teleop.json`：机械臂地址、硬件、工具和负载预检。
- `realman_follow.json`：运动限制、腕部相机序列号与Quest会话方向校准。
- `camera_mount.json`：相机支架手眼配置。
- `devices.json`：运行configure命令后保存的Quest设备选择。

前三份由`python examples/teleop/teleop.py init-config`从包内模板创建，已有文件不会覆盖。
实际JSON属于本机设备配置。采集与推理复用这里的机器人和运动参数，修改后会共同生效。
