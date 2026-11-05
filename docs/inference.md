# 本地云端策略执行

入口：`examples/inference/infer.py`；核心模块：`src/inference/`。
云端先按`tools/infer/README.md`启动服务并等待READY。

## 运行

```bash
python examples/inference/infer.py --help
python examples/inference/infer.py --check-only
python examples/inference/infer.py --check-gripper
python examples/inference/infer.py --check-sdk

# 双相机＋实际状态＋云端模型，零运动下发
python examples/inference/infer.py --seconds 10

# 实际控制：预检通过后自动驱动，不需要Quest或握柄
python examples/inference/infer.py --execute --seconds 10
```

读取`examples/inference/config/infer.json`；其中robot_config、follow_config、camera_config相对该文件所在目录解释。
默认引用分别为`../../teleop/config/realman_teleop.json`、`../../teleop/config/realman_follow.json`、`../../recording/config/cameras_dual.json`，共用配置只保留一份。
云端地址位于uri，本地机器人地址位于`examples/teleop/config/realman_teleop.json`。笔记本分别连接两者，并不要求同网段。
用`--uri`、`--config`、`--seconds`、`--output`可覆盖本次运行参数，默认时长来自配置。
运行前用`--check-only`读取生效的配置路径和参数。

诊断模式只读，不打开相机或连接云端，不发送开合、使能或运动命令。
SDK返回码0表示查询成功，夹爪仍需enable_state/status为1且error为0。
法兰供电对应rm_get_tool_voltage，控制器对外供电对应rm_get_voltage；二者不能混用。
预览会打开真实相机、查询真实状态和推理，计算规划目标，但不发送关节、夹爪或STOP。
实际模式Ctrl+C/到时/故障撤销下发，并在已有运动时发送STOP和确认反馈。

## 数据与执行

- 输入为front/wrist RGB uint8[224,224,3]、六关节弧度＋夹爪开度0~1和任务文字。
- 模型输出是绝对关节目标弧度与绝对夹爪开度，不再做逆解或重复加当前关节角。
- 模型动作10Hz，本地运动中间目标50Hz，复用movej_follow、速度/加速度与制动路径检查。
- 参考时间为两相机较晚接收时刻；例如延迟230ms，跳过前两步，从序号2开始选择，最多保留execute_steps步。
- 发送前输入超过100ms则丢帧并保留连接；采用/执行时预测总年龄仍限制500ms。
- 选中目标相对实际关节最大偏差8°；超过则拒绝该次预测并等待新结果，不自动放宽限制。
- 夹爪开度≤0.3闭合、≥0.7张开，中间保持，通过独立命令通道发送；过时动作不补发。
- 没有可用目标时减速/保持；持续没有采用预测超过request_timeout_s+1秒则结束。
- 请求已发出后的超时、反馈故障或断连仍会停止，不自动重连后恢复运动。

运动限速、工作球半径和硬件校验条件以实际配置为准。
模型自碰撞/奇异检查不等于实物场景障碍物检测。

## 状态和日志

`PREVIEW/EXECUTE`表示模式；`FOLLOW`推进目标，`BRAKE/HOLD`减速或保持。
`predictions`是采用数，不是收到总数；`sent`包括保持指令，预览中为0。
`align/skip/action`描述动作时间选择，`limit`描述规划限制。

每次运行写入`run/infer_时间/events.jsonl`与`report.json`，不保存照片/视频。
事件包括实际反馈、原始预测、观测和往返时间、采用/拒绝原因、动作序号、下发、具体协程异常和STOP确认。
UTC日志显示北京时间需加8小时，monotonic只在笔记本本机比较。
report中的operator_or_duration不代表任务成功；failed查看error；实际运动后的stop_verified应为true。

| 常见提示 | 含义 |
|---|---|
| Prediction J...differs...limit=8deg | 模型目标与实际关节相差超阈值，不等同于控制器不可达报警 |
| Cloud prediction expired before adoption | 最旧输入到采用时超过500ms，即使网络没有超时也会拒绝 |
| No accepted fresh prediction | 连续拒绝/过期导致长期没有可采用目标，触发超时＋1秒退出 |
| 夹爪状态全0 | 查看现场供电、连接、初始化与示教器状态；不靠伪造开度绕过检查 |
| Permit expired | 查看详细提示，区分机器人、相机、夹爪、硬件配置和云端状态 |
| STOP未确认 | 在现场检查机器人并使用实体急停，不能当作已停止 |

夹爪检查以实际状态反馈为准，程序不自动触发初始化运动。

## 其他入口

```bash
# 文件观测通信，不打开设备
python examples/inference/request_policy.py --uri ws://服务器IP:8001 --input /实际路径/observation.npz --count 3

# 仅回放日志中的时间选择，不打开网络或设备
python examples/inference/infer.py --replay-events /实际路径/events.jsonl --replay-delay-ms 230
```

回放时传入需要分析的日志路径；日志缺少reference_at时会标记估计参考时间。
云端训练与推理分别见`tools/train/README.md`和`tools/infer/README.md`。
