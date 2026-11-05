# 双相机配置

`cameras_dual.json`保存front/wrist相机序列号、图像格式和采集参数。
由`python examples/teleop/teleop.py init-config`创建缺失文件，填写实际设备序列号后使用。
实际JSON被Git忽略。

采集共用`../../teleop/config/realman_teleop.json`与`realman_follow.json`中的机器人、校准与运动参数。
可选的`realman_recording_initial.json`也归本目录；程序不自动创建它。
