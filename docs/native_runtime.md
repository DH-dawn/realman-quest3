# 原生运行库与Quest客户端

源码分发包不包含DEB、APK、wheel或已解压的SDK。安装包可以本地存放于`Android_APK/`，其说明见
[Android_APK/README.md](../Android_APK/README.md)。策略推理不需要Quest服务；手柄遥操需要服务与专用客户端。

## XRoboToolkit PC Service / SDK

优先使用已安装的`/opt/apps/roboticsservice`，或者设置：

```bash
export REALMAN_SERVICE_DIR=/实际安装路径/roboticsservice
# 单独指定SDK时可设置：
export XROBOT_SDK_LIBRARY=/实际路径/libPXREARobotSDK.so
```

也可以下载官方Ubuntu 22.04 amd64 v1.0.0发行包：

https://github.com/XR-Robotics/XRoboToolkit-PC-Service/releases/download/v1.0.0/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb

从仓库根目录运行：

```bash
bash scripts/install_pc_service.sh Android_APK/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb
python examples/teleop/teleop.py doctor
```

安装脚本核对固定SHA256，仅提取至本目录被Git忽略的`.native/pc-service/`，不启动服务、不连接机器人。
服务路径依次为显式环境变量、本项目`.native`、系统`/opt`。遥操启动器按需启动服务。

系统组件可在需要时安装：

```bash
sudo apt install python3.10-venv adb usbutils iproute2 libgl1 libglib2.0-0 \
  libxkbcommon0 libdbus-1-3 libxcb-cursor0 libgssapi-krb5-2 libudev1 libusb-1.0-0
sudo install -m 644 scripts/99-realman-quest.rules /etc/udev/rules.d/99-realman-quest.rules
sudo udevadm control --reload-rules
```

## Quest专用客户端

遥操要求带逐手柄有效性字段和A/B按键的`com.xrobotoolkit.client.quest.realman`客户端。
客户端需要提供运动授权字段。已安装所需客户端的头显不必重复安装。

将客户端APK放到`Android_APK/XRoboToolkit-Quest-RealMan-1.0.1-r2.apk`。
客户端的源码参考与补丁见`third_party/quest_client/`，MIT许可见`licenses/`。
自行构建需要对应的上游Unity工程、Unity和Meta SDK。

```bash
python examples/teleop/teleop.py install-quest
# 如APK存放在其他位置，可覆盖默认路径：
REALMAN_QUEST_APK=/实际路径/客户端.apk python examples/teleop/teleop.py install-quest
```

安装命令会运行ADB安装并启动应用，但不驱动机器人。多头显时加`--serial`或设置`QUEST_SERIAL`。
遥操启动器通过ADB建立头显到本机PC Service的USB反向映射，头显中PC Service IP使用127.0.0.1。
这与笔记本到云端的WebSocket连接无关。保持应用前台、佩戴有效且右控制器追踪正常。
开发者模式、USB授权、手工映射和排障的完整步骤见[Quest USB ADB指南](quest_usb_adb.md)。
