# 本地策略配置

`infer.json`保存云端WebSocket地址、任务、时长、预测限制与共用配置路径。
通过`python examples/teleop/teleop.py init-config`创建缺失文件，实际JSON被Git忽略。

三个引用均相对本目录解析：

- `robot_config`：`../../teleop/config/realman_teleop.json`
- `follow_config`：`../../teleop/config/realman_follow.json`
- `camera_config`：`../../recording/config/cameras_dual.json`

云端模型服务自己的配置在`tools/infer/config.json`，与本地策略配置分别维护。
