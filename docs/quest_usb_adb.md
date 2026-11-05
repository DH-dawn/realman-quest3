# Meta Quest 3安装与USB ADB连接

本指南适用于Ubuntu笔记本、Meta Quest 3、XRoboToolkit PC Service和本项目的专用Quest客户端。
所有项目命令从`realman-quest`根目录运行，Python使用已有的`vr`环境。

## 1. 准备头显和USB线

在手机的Meta Horizon应用中选择配对的Quest，进入头显设置，开启开发者模式。
若看不到该开关，先按Meta说明完成开发者账号注册、验证和设备设置；菜单名称可能随系统版本变化。
使用支持数据传输的USB-C线连接Quest和笔记本，戴上头显并允许USB调试，可勾选始终允许此电脑。
开发者模式和调试授权步骤参见[Meta官方设备设置](https://developers.meta.com/horizon/documentation/native/android/mobile-device-setup/)
及[Meta官方连接说明](https://developers.meta.com/vr/documentation/unreal/ts-mqdh-getting-started/)。

此项目通过独立Quest应用和ADB传输数据，使用USB数据连接即可，无需进入Quest Link或Air Link。
USB充电正常并不代表USB数据连接正常；文件传输许可也不等于USB调试许可。

## 2. 安装ADB并确认授权

```bash
sudo apt install adb
conda activate vr
adb start-server
adb devices -l
```

正常会看到类似以下内容，实际序列号以自己的设备为准：

```text
List of devices attached
YOUR_QUEST_SERIAL    device usb:... model:Quest_3 ...
```

状态必须为`device`。`unauthorized`表示还需在头显中允许USB调试，`offline`表示连接尚未可用。
`adb devices`、设备选择和APK安装的通用语法参见[Android官方ADB文档](https://developer.android.com/tools/adb)。

如果显示`no permissions`，从项目根目录安装USB规则，然后拔插数据线：

```bash
sudo install -m 644 scripts/99-realman-quest.rules /etc/udev/rules.d/99-realman-quest.rules
sudo udevadm control --reload-rules
```

日常使用普通用户运行adb。连接多台安卓设备，或同一头显同时存在USB和无线ADB连接时，
选择带`usb:`信息的那一行，并设置其序列号，后面的命令都会使用它：

```bash
export QUEST_SERIAL=你的Quest的USB序列号
adb -s "$QUEST_SERIAL" get-state
```

只有一台已授权设备时，项目会自动选择；保存过的设备选择在`examples/teleop/config/devices.json`中。

## 3. 准备电脑端PC Service

电脑已经安装`/opt/apps/roboticsservice`且doctor通过时可直接使用。
新电脑可从`Android_APK/`中的DEB提取服务：

```bash
bash scripts/install_pc_service.sh Android_APK/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb
python examples/teleop/teleop.py doctor
```

该脚本校验官方v1.0.0安装包的SHA256，并提取到本项目的`.native/pc-service/`。
安装脚本和doctor均不启动服务；monitor等运行入口会按需启动它。
原生库路径、系统依赖和官方下载地址见[原生运行库说明](native_runtime.md)。

## 4. 安装Quest专用APK

将Quest客户端APK保存为：

```text
Android_APK/XRoboToolkit-Quest-RealMan-1.0.1-r2.apk
```

安装包区别见[目录说明](../Android_APK/README.md)。
然后执行：

```bash
python examples/teleop/teleop.py install-quest
```

此入口使用`adb install -r`安装，建立`63901`端口的USB反向映射，再启动专用应用。
多设备时可加`--serial`；其他APK路径可用`REALMAN_QUEST_APK`覆盖。
`bash scripts/install_native_client.sh`调用同一个安装实现，也支持`--serial`和`--help`。

安装后可查询专用包是否存在：

```bash
adb shell pm path com.xrobotoolkit.client.quest.realman
```

多设备时，在以上及后续手工adb命令中加上`-s "$QUEST_SERIAL"`。
安装只启动客户端，电脑端PC Service由下一步monitor启动。安装或重启头显应用后，真机遥操前重新做方向校准。

## 5. 有线传输与头显设置

推荐直接使用项目入口：

```bash
python examples/teleop/teleop.py monitor --seconds 15
```

它会选择已授权设备、按需启动PC Service、自动执行`adb reverse tcp:63901 tcp:63901`，并打开专用客户端。
这是有线模式的默认行为，不要添加`--wifi`。

在头显XRoboToolkit应用中确认：

- PC Service IP为`127.0.0.1`；若界面可填写端口，使用本项目对应的`63901`。
- 连接到PC Service，选择Controller手柄数据模式并开启Send。
- 关闭`Switch w/ A Button`，以免A键在采集时切换数据发送。
- 保持应用前台、头显处于佩戴状态、左右手柄有电且追踪正常。

客户端通常会自动选择Controller、开启Send并连接`127.0.0.1`，仍应在界面确认实际连接状态。

本项目的链路为：

```text
Quest应用 → 头显127.0.0.1:63901
          → USB ADB reverse
          → 笔记本PC Service:63901
          → 笔记本本地SDK/gRPC:60061
          → Python手柄读取
```

`127.0.0.1`通过反向映射指向电脑服务，无需在头显填写机械臂IP或云服务器IP。
`60061`是本机SDK接口，不要用它替换头显连接端口。
这套USB连接不要求Quest与电脑处于相同Wi-Fi网段；笔记本到机器人及云端的连接分别配置。

需要手工排查时，可在一个终端保持PC Service运行：

```bash
bash scripts/start_xrobotoolkit_service.sh
```

另一个终端建立映射并查看结果：

```bash
adb reverse tcp:63901 tcp:63901
adb reverse --list
```

列表包含`tcp:63901 tcp:63901`才表示映射已建立；它本身不保证应用已连接、Send已开启或数据在更新。
拔插USB、重启头显或ADB服务后，重新运行monitor等项目入口建立映射。

## 6. 从读取数据到遥操

先在monitor中移动手柄、按键，确认数值持续变化，再进行以下操作：

```bash
# 读取设备并完成当前Quest会话方向校准
python examples/teleop/teleop.py calibrate --seconds 180
# 读取真实反馈，不下发运动
python examples/teleop/teleop.py preview --seconds 10
# 真机遥操
python examples/teleop/teleop.py run
```

设备配置、工作范围和实际控制方式见[遥操与采集说明](teleoperation.md)。

## 常见问题

| 现象 | 检查方法 |
|---|---|
| 只充电，adb没有设备 | 换支持数据的线和USB口；确认开发者模式、头显唤醒及USB调试授权 |
| `unauthorized` | 戴上头显接受电脑的调试授权；文件传输授权不能替代它 |
| `offline` | 退出遥操/采集后拔插USB，再执行adb devices；仍异常时再重启ADB服务并重新建立映射 |
| `more than one device`或项目要求指定设备 | 使用USB序列号：项目加`--serial`或设置QUEST_SERIAL；手工adb加`-s` |
| Missing APK | 把专用APK放到默认位置，或设置REALMAN_QUEST_APK；DEB不能作为APK安装 |
| APK签名或版本冲突 | 核对包名、版本和签名，使用匹配的构建；先保留原应用和数据，不由脚本自动卸载 |
| 应用显示未连接 | 检查电脑PC Service正在运行、USB映射存在、头显地址为127.0.0.1 |
| 连上但位置和按键不变化／数据超时 | 检查Controller、Send、前台和佩戴状态，再看手柄追踪；仅有ADB device状态不足以证明数据链路正常 |
| 重启应用后方向校准失效 | 在新的Quest应用会话中重新运行calibrate，再预览和遥操 |

重启ADB服务的方法如下，仅在退出设备使用程序后执行：

```bash
adb kill-server
adb start-server
adb devices -l
```
