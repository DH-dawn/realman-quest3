# RM65 π0.5 云端推理服务

本目录在笔记本上的位置：`/home/dh/API2_Demo/realman-quest/tools/infer/`。
部署到云服务器的位置：`/data2/dh/openpi/tools/infer/`。
云端加载模型并返回动作序列；相机采集、实际机器人反馈、时间对齐、轨迹规划和运动下发由笔记本完成。
配套训练代码与说明独立放在`/home/dh/API2_Demo/realman-quest/tools/train/`，
云端训练入口为`/data2/dh/openpi/tools/train/server_train.py`。

## 快速使用

**先启动云端服务，再启动笔记本客户端。** 以下命令以服务器`192.168.96.202`、5000步权重与GPU 7为例；
实际权重或GPU变更时，修改对应参数。

在云端训练环境执行：

```bash
cd /data2/dh/openpi
source /data2/dh/openpi/.venv/bin/activate

# 只检查配置、权重目录及归一化统计
python /data2/dh/openpi/tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7 --check-only

# 启动，等待出现READY；保持当前终端运行
python /data2/dh/openpi/tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7
```

在笔记本另开终端验证，然后运行本地预览：

```bash
curl --connect-timeout 3 --max-time 5 http://192.168.96.202:8001/healthz
conda activate vr
python /home/dh/API2_Demo/realman-quest/examples/inference/infer.py --seconds 10
```

云端代码不会打开相机或控制机械臂；实际设备运行方法见
`/home/dh/API2_Demo/realman-quest/docs/inference.md`。

## 云端部署

将项目的整个`tools/`目录放在云端openpi项目根目录，参见[云端工具说明](../README.md)。
本目录包含云端服务源码，不包含openpi源码、JAX训练环境或模型权重。
需要放进openpi项目的`/data2/dh/openpi/tools/infer/`，并使用与训练相匹配的环境。
权重目录必须同时具有完整`params/`和`assets/rm65_dual_aligned/norm_stats.json`。

复制后的服务入口为`/data2/dh/openpi/tools/infer/server.py`，配置为同目录的`config.json`。
按下面的配置说明填写checkpoint、GPU和监听地址。整个`tools/`目录可以独立部署，不需要笔记本的`src/`。
模型权重和归一化统计需要另外准备；文件复制只用于部署，模型通信使用WebSocket。

## 1. 网络结构与文件分工

```text
云服务器 192.168.96.202:8001
            ⇅ WebSocket：图像、状态、任务文字 / 预测动作
笔记本 /home/dh/API2_Demo/realman-quest/examples/inference/infer.py
            ⇅ 网线 TCP/JSON：实际反馈 / movej_follow与夹爪指令
RM65 192.168.0.18:8080
```

笔记本分别访问两个网段，应用程序负责数据中转。服务器无需直接访问机械臂，不使用SSH端口转发或系统网桥。
云端`host`填写监听地址`0.0.0.0`，本地客户端填写`ws://192.168.96.202:8001`。

| 文件 | 用途 |
|---|---|
| `server.py` | WebSocket长连接、单客户端会话、推理调度、健康检查及错误处理 |
| `model.py` | 恢复训练时的LoRA配置、加载checkpoint和归一化统计、预热模型 |
| `protocol.py` | 通信协议，双RGB图像、7维状态、关节顺序与动作时间定义 |
| `client.py` | 异步客户端及NPZ连续通信入口，可在笔记本独立运行 |
| `config.json` | checkpoint、GPU、监听地址、端口、任务文字及服务端推理超时 |
| `requirements.txt` | 通信依赖；模型依赖使用训练环境 |
| `__main__.py`、`__init__.py` | 包入口和包标识 |

## 2. 配置与环境

使用与模型训练相同的Python环境，示例路径为`/data2/dh/openpi/.venv`：

```bash
cd /data2/dh/openpi
source /data2/dh/openpi/.venv/bin/activate
```

如果训练时使用的是Conda环境，则激活对应环境；不需要再新建模型环境。
缺少通信依赖时，在云端训练环境中执行：

```bash
python -m pip install -r /data2/dh/openpi/tools/infer/requirements.txt
```

以下配置示例使用5000步权重、物理GPU 7、8001端口。云端配置文件
`/data2/dh/openpi/tools/infer/config.json`的对应示例为：

```json
{
  "checkpoint": "checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000",
  "asset_id": null,
  "host": "0.0.0.0",
  "port": 8001,
  "gpu": "7",
  "num_steps": 10,
  "default_prompt": "pick the bottle and place in the light brown box",
  "inference_timeout_s": 30.0
}
```

本目录的公共配置与上述5000步、GPU 7、8001端口示例一致。不同服务器请调整权重路径和GPU。
更新既有云端部署时保留服务器自己的config.json；启动命令也可显式覆盖关键参数。

- `checkpoint`应指向完整步数目录，保留其整个`params/`和`assets/`，不能只复制某个OCDBT碎片。
- 相对checkpoint路径以`--openpi-root`为基准，默认是`/data2/dh/openpi`。
- 传入实验目录时，脚本选择其中最大已完成数字步数；运行期间不自动更换权重。
- `asset_id=null`时寻找唯一的归一化统计；统计来自选中的checkpoint。
- `num_steps=10`是每次推理的去噪步数，与训练5000步或动作序列长度不同。
- `host`只填写IP或可解析主机名，不包含`ws://`、端口、路径或空格；端口单独填写`port`。
- `--gpu 7`通过`CUDA_VISIBLE_DEVICES`选择物理GPU 7，进程内可能显示`CudaDevice(id=0)`。

### 启动参数

命令行参数优先于配置文件；不指定参数时使用与`server.py`同目录的`config.json`。

| 参数 | 用途 |
|---|---|
| `--config /绝对路径/config.json` | 选择其他服务配置文件 |
| `--openpi-root /data2/dh/openpi` | 指定openpi源码与相对checkpoint路径的根目录 |
| `--checkpoint /绝对路径/步数` | 选择已完成的权重目录，不是单个权重碎片文件 |
| `--asset-id rm65_dual_aligned` | 多组归一化统计时显式指定资产名称 |
| `--host 0.0.0.0`、`--port 8001` | 允许内网客户端连接的监听地址和端口 |
| `--gpu 7` | 选择物理GPU编号 |
| `--num-steps 10` | 设置每次推理的去噪步数 |
| `--check-only` | 检查后退出，不启动模型或网络服务 |

## 3. 云端启动

先检查地址、checkpoint目录和归一化统计。此命令不加载模型、不启动监听：

```bash
python /data2/dh/openpi/tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7 --check-only
```

然后启动常驻服务：

```bash
python /data2/dh/openpi/tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7
```

脚本先打印实际配置文件及生效参数，再加载权重和归一化统计，并用合成输入预热JAX。
预热结果丢弃，出现下面日志后才表示网络服务就绪：

```text
READY ws://0.0.0.0:8001
```

保持终端运行。服务就绪后安静等待客户端是正常现象；客户端每次请求都会调用已经加载的模型。
云端配置已保存正确时，也可直接运行`python /data2/dh/openpi/tools/infer/server.py`。
当前本地执行器只尝试启动连接一次，因此等云端READY后再启动本地。
本地使用examples/inference/infer.py，包内源码为src/inference；配置为examples/inference/config/infer.json。

## 4. 内网连通与本地运行

在笔记本执行健康查询：

```bash
curl --connect-timeout 3 --max-time 5 http://192.168.96.202:8001/healthz
```

`ready=true`、`fault=null`表示服务就绪；`connected=false`表示当前没有策略客户端连接。
HTTP健康查询不会执行模型。真正的WebSocket推理由下面的本地入口发起：

```bash
# 笔记本：只读预览，打开真实相机和机器人状态查询
conda activate vr
python /home/dh/API2_Demo/realman-quest/examples/inference/infer.py --seconds 10
```

本地地址保存在`/home/dh/API2_Demo/realman-quest/examples/inference/config/infer.json`的`uri`中。
实际运动模式、夹爪检查、运行时长与保护条件见本地
`/home/dh/API2_Demo/realman-quest/docs/inference.md`。

服务支持可选的`RM65_INFER_TOKEN`环境变量。启用时，两端使用相同值；WebSocket和健康接口都需要Bearer认证。
未设置该变量时可直接使用上面的健康查询。

## 5. 输入输出与时间对齐

协议为JSON/base64 RGB的`rm65-pi05-v2`，与openpi默认的MsgPack协议不同。
握手提供checkpoint、输入规格、动作长度、关节顺序、单位和动作区间定义。
客户端在一个会话内使用递增请求编号，校验响应的会话与请求编号；服务只接一个策略客户端，每次只处理一条推理，不积压输入。

| 字段 | 规格与含义 |
|---|---|
| `observation/image` | front相机，RGB uint8，224×224×3 |
| `observation/wrist_image` | wrist相机，RGB uint8，224×224×3 |
| `observation/state` | `[J1..J6弧度, gripper_opening]`，7维 |
| `prompt` | 非空任务文字，使用与任务一致的指令 |
| `actions` | 当前模型为10×7；每行是六关节绝对角度弧度及夹爪绝对开度0~1 |

训练中的关节delta变换由官方策略输出变换恢复为绝对角度；本地不再叠加当前关节角，也不做逆解。
云端保留原始预测，夹爪回归超出0~1时额外返回`gripper_out_of_range_count`，是否采用由本地决定。

每行对应一个100ms动作区间，即模型动作采样率10Hz；去噪步数和动作采样率是两回事。
时间参考是观测对应的笔记本相机接收时间，由笔记本保留，不比较云端与笔记本的单调时钟。
例如观测到采用结果已过230ms，本地跳过序号0、1，从序号2开始选取，不重新播放过去的区间。
实际限速、制动、反馈和是否下发由笔记本50Hz控制循环处理。

## 6. 只调用模型的NPZ通信入口

在笔记本使用已保存的真实观测文件，可单独检查模型响应及网络，不打开相机或机器人：

```bash
python /home/dh/API2_Demo/realman-quest/examples/inference/request_policy.py \
  --uri ws://192.168.96.202:8001 \
  --input /实际路径/observation.npz \
  --count 3 --interval 0.1 --timeout 5
```

输入路径应指向实际存在的观测文件，也可用
`/home/dh/API2_Demo/realman-quest/tools/infer/single.py --prepare-only`准备输入。
该入口重复文件观测，不代表实时控制。`--count 0`持续运行，Ctrl+C结束。
每次响应后等待`--interval`再发下一次请求，不保证固定10Hz。
输出保存到启动目录下的`inference_outputs/remote_时间.jsonl`，可用`--output`指定一个新文件。

在其他脚本中集成时，使用`PolicyClient`异步接口并保持同一个连接。
`captured_at`应是笔记本上最旧参与输入的真实单调时间，禁止在发送前重新标记旧观测。
发送前或打包期间输入超过时限时抛出`StaleObservationError`，丢弃该帧、保留连接并重新采集。
请求发出后的超时、错误响应或断连关闭连接，不自动重连或重放；GPU调用结束前不会接受另一个控制客户端。

## 7. 云端日志保存

默认只输出终端日志，不自动生成云端日志文件。需要保存时，在云端训练环境使用下面的启动方式；
它与普通启动二选一，不要同时运行两个服务占用同一端口。

```bash
mkdir -p /data2/dh/openpi/tools/infer/logs
set -o pipefail
python -u /data2/dh/openpi/tools/infer/server.py \
  --checkpoint /data2/dh/openpi/checkpoints/pi05_rm65_dual_lora/rm65_smoke/5000 \
  --host 0.0.0.0 --port 8001 --gpu 7 \
  2>&1 | tee /data2/dh/openpi/tools/infer/logs/server_$(date +%Y%m%dT%H%M%S).log
```

`tee`同时显示并保存标准输出、标准错误；每次启动生成新的时间戳文件。
云端日志包括配置、checkpoint加载、预热、READY、连接会话、请求编号、模型耗时、动作形状和推理异常。
原始动作、实际关节反馈、预测采用/拒绝和停止结果在笔记本的运行日志中。

## 8. 常见问题

| 现象 | 含义与处理 |
|---|---|
| `address already in use` | 端口被占用。用`ss -ltnp 'sport = :8001'`确认占用者；关闭占用端口的服务或两端一起改用另一端口 |
| `Name or service not known` | 检查生效日志里的host；内网监听填`0.0.0.0`，URI中的`ws://`与端口不要放入host |
| `READY ws://127.0.0.1:...` | 只监听云服务器自身；本机直连应改为`--host 0.0.0.0` |
| READY后没有推理日志 | 检查本地程序是否已启动；本地夹爪预检失败时还不会建立策略连接 |
| `/healthz`可访问但WebSocket失败 | 核对通信协议、输入/输出定义、认证配置，以及是否已有另一个策略客户端 |
| 本地报告预测过期 | 本地总观测年龄上限与云端请求超时不同；查看两端日志，区分模型耗时与网络/采集年龄 |
| 服务进入`fault` | 模型异常、无效输出或推理超时后会闭锁，需要排查并重启服务 |

云端默认允许一次推理最多30秒；这不表示本地允许机器人继续执行30秒旧动作。
具体动作有效年龄、关节跳变和失联停止由本地执行器单独约束。
