# 本地推理执行

从仓库根目录运行：

```bash
python examples/inference/infer.py --help
python examples/inference/infer.py --check-only
python examples/inference/infer.py --seconds 10
python examples/inference/infer.py --execute --seconds 10
```

`--check-only`仅解析配置，不打开设备。
默认模式连接相机、机器人与云端进行预览，不下发运动；`--execute`允许驱动真机。
云端先按[云端说明](../../tools/infer/README.md)启动服务并等待READY。

本地策略配置位于`config/infer.json`，其中引用`../teleop/config/`的机器人与运动配置、
`../recording/config/`的相机配置。首次使用通过`python examples/teleop/teleop.py init-config`创建。
设置云端uri后，先用`--check-only`确认实际路径和参数。

`request_policy.py`可发送文件中的观测并查看预测，不打开机器人或相机；使用`--help`查看参数。
日志保存在工作目录的`run/infer_时间/`。完整流程见[本地推理说明](../../docs/inference.md)。
