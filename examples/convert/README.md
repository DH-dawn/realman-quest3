# 数据集转换示例

`convert_dataset.py`调用`src/convert/convert_dual_to_lerobot.py`中的`main()`，不包含独立的转换实现。
请使用[转换模块说明](../../src/convert/README.md)中的数据转换环境，避免修改机器人控制环境的依赖。

## 准备配置

从项目根目录初始化缺失配置：

```bash
python examples/teleop/teleop.py init-config
```

配置位于`examples/convert/config/conversion_config.json`，公共模板位于`src/common/defaults/`。
填写`input`为采集的episode、session或集合目录；`output`为尚不存在的数据集输出目录。
JSON内的相对路径以该配置文件所在目录为基准，命令行路径以当前工作目录为基准。
默认输出为项目的`datasets/rm65_dual_aligned/`，默认`dry_run=true`只检查数据。

## 命令行运行

```bash
python examples/convert/convert_dataset.py --help

# 检查数据，读取默认配置并覆盖input
python examples/convert/convert_dataset.py \
  --input /实际路径/session/episodes --dry-run

# 正式转换，输出目录不能预先存在
python examples/convert/convert_dataset.py \
  --input /实际路径/session/episodes \
  --output /实际路径/datasets/rm65_dual_aligned \
  --repo-id rm65_dual_aligned --no-dry-run
```

无参数运行时读取本目录的配置；也可通过`--config /实际路径/配置.json`选择其他文件。
命令行参数覆盖配置值。正式转换必须设置`dry_run=false`或传入`--no-dry-run`。

## PyCharm运行

选择数据转换环境的Python解释器，填写配置后直接运行`convert_dataset.py`。
也可以在运行参数中填写`--input ... --dry-run`或正式转换参数。
脚本会定位项目src和默认配置，不要求将工作目录设置为示例脚本所在目录。

该入口只读取记录并校验或导出数据，不打开相机、不连接机械臂、不调用云端模型。
