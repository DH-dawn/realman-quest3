# Quest客户端与电脑端安装包

本目录供本机保存安装包。所有命令从项目根目录执行。

| 文件 | 安装目标 | 用法 |
|---|---|---|
| `XRoboToolkit-Quest-RealMan-1.0.1-r2.apk` | Meta Quest 3 | `python examples/teleop/teleop.py install-quest` |
| `XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb` | Ubuntu 22.04 x86_64电脑 | `bash scripts/install_pc_service.sh Android_APK/XRoboToolkit_PC_Service_1.0.0_ubuntu_22.04_amd64.deb` |

DEB是电脑端服务，不能安装到头显；Quest使用表中的APK。
已经安装专用版本的Quest可以直接使用，无需再次安装。

Quest客户端包名为`com.xrobotoolkit.client.quest.realman`。
它包含本项目需要的逐手柄追踪有效性字段；普通上游客户端可用于部分只读诊断，不能直接替代它进行本项目的真机控制。
客户端源码参考和补丁见[third_party/quest_client](../third_party/quest_client/README.md)。

```bash
conda activate vr
adb devices -l
python examples/teleop/teleop.py install-quest
```

安装命令会安装APK、建立USB映射并启动头显应用。多设备时指定USB序列号：

```bash
python examples/teleop/teleop.py install-quest --serial 你的Quest序列号
```

如果APK名称或位置不同，用环境变量指定：

```bash
REALMAN_QUEST_APK=/实际路径/客户端.apk python examples/teleop/teleop.py install-quest
```

完整的开发者模式、USB调试授权、ADB有线连接、头显设置和常见问题见
[Quest USB ADB连接指南](../docs/quest_usb_adb.md)。

APK和DEB作为独立安装资源使用，不随Python源码包分发。
