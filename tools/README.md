# 云端训练与推理工具

整个`tools/`目录放在云端openpi项目根目录下即可，例如`/data2/dh/openpi/tools/`。
它包含模型训练与推理服务的代码；机械臂通信、遥操、相机采集和策略执行由笔记本的`src/`负责。
本地数据集转换入口为笔记本项目中的`src/convert/convert_dual_to_lerobot.py`，用法见该目录的README。

## 放置位置

```text
/data2/dh/openpi/
├── src/openpi/
├── scripts/
├── models/pi05_base/params/
├── datasets/rm65_dual_aligned/
├── checkpoints/
└── tools/
    ├── README.md
    ├── train/
    │   ├── server_train.py
    │   ├── requirements.txt
    │   └── README.md
    └── infer/
        ├── server.py
        ├── model.py
        ├── protocol.py
        ├── client.py
        ├── single.py
        ├── config.json
        ├── requirements.txt
        ├── __init__.py
        ├── __main__.py
        └── README.md
```

复制时将`tools`作为openpi根目录的直接子目录，不要嵌套成`tools/tools/`。
如果openpi已有`tools/`，将本目录内容合并进去，并保留其他工具和服务器自己的配置。

云端只需这份`tools/`，无需复制或安装笔记本的`src/`软件包。
仍需另行准备openpi源码、与训练兼容的Python/JAX环境、基础模型和完整数据集；推理时需要已训练的checkpoint。
配置文件中的路径、GPU编号和网络地址应按服务器实际情况填写。

## 运行顺序

1. 笔记本使用`src/convert/convert_dual_to_lerobot.py`转换数据，上传完整的数据集目录。
2. 云端按[训练说明](train/README.md)运行`check`、`norm`、`train`。
3. 云端按[推理说明](infer/README.md)配置checkpoint并启动服务，等待READY。
4. 笔记本运行`examples/inference/infer.py`，通过WebSocket连接云端并执行预测。

模型加载和计算在云端；真实相机、机器人反馈和运动下发在笔记本。
云端代码中的客户端和协议支持独立部署及文件观测通信，本地执行器使用自己的通信模块；两端协议须一致。

## 入口示例

在云端openpi根目录激活模型环境后：

```bash
source .venv/bin/activate
python tools/train/server_train.py --help
python tools/infer/server.py --help
```

实际训练参数见`train/README.md`。模型服务使用`infer/config.json`，也可由命令行覆盖：

```bash
python tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7
```

checkpoint必须是实际存在且保存完成的目录。笔记本连接URI使用服务器可访问的IP，例如`ws://192.168.96.202:8001`。
