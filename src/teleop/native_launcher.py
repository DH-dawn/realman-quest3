"""One command: PC Service + optional USB reverse + native read-only teleop."""
from common.paths import config_file
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
from common.device_config import resolve_quest_serial

from common.paths import ROOT, module_command, module_environment


def wait_for_child(args):
    child = subprocess.Popen(args, cwd=ROOT, env=module_environment())
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    def forward(sig, _frame):
        if child.poll() is None:
            print('正在停止并保存采集数据，请等待进程退出。', flush=True)
            try:
                child.send_signal(sig)
            except ProcessLookupError:
                pass
    try:
        for sig in signals:
            signal.signal(sig, forward)
        return child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def check_idle():
    profile = json.loads((config_file("realman_teleop.json")).read_text())
    paths = [ROOT / ".xrobotoolkit-input.lock",
             ROOT / f".realman-{profile['robot_ip']}-{profile['robot_port']}.lock"]
    for path in paths:
        with path.open("a+") as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("已有采样或遥操进程运行，请先退出它，再启动直接模式") from error


def listening():
    try:
        with socket.create_connection(("127.0.0.1", 60061), timeout=.2):
            return True
    except OSError:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--monitor", action="store_true", help="仅检查手柄，不连接机械臂")
    p.add_argument("--calibrate-right", action="store_true", help="只读记录向右手势，对齐D405画面方向")
    p.add_argument("--calibrate-axes", action="store_true", help="只读连续记录右、上、前，对齐并固定三个方向")
    p.add_argument('--third-direction', choices=('forward', 'backward'), default='forward',
                   help='三方向校准的第三次动作：forward前推，backward后拉')
    p.add_argument("--wifi", action="store_true", help="不配置 USB 转发，使用头显中的电脑 Wi-Fi IP")
    p.add_argument("--serial", help="Quest 序列号；默认读取 examples/teleop/config/devices.json 或检测唯一已授权设备")
    p.add_argument("--app-package", help="Override installed Quest package for diagnostics")
    p.add_argument("--seconds", type=float)
    p.add_argument("--output", type=Path)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--direct", action="store_true", help="直接真机启动，仅用 Grip 控制，无需 START")
    p.add_argument("--check", action="store_true")
    p.add_argument("--follow", action="store_true", help="6D连续关节跟随 + Trigger夹爪")
    p.add_argument("--preview", action="store_true", help="仅用于 --follow；不发送运动或夹爪指令")
    p.add_argument("--config", type=Path, help="连续跟随配置")
    p.add_argument("--record", action="store_true", help="手柄分段采集 D405/机械臂：A 开始或成功结束，B 失败结束")
    p.add_argument("--dual-cameras", action="store_true", help="独立双摄采集入口，需 --follow --record")
    p.add_argument("--camera-config", type=Path, help="双摄角色/序列号配置")
    p.add_argument("--task", default="", help="本次演示的任务描述")
    a = p.parse_args(argv)
    if a.dual_cameras and not (a.follow and a.record):
        p.error('--dual-cameras requires --follow --record')
    if a.camera_config and not a.dual_cameras:
        p.error('--camera-config requires --dual-cameras')
    calibration = a.calibrate_right or a.calibrate_axes
    if a.calibrate_right and a.calibrate_axes:
        p.error('Choose --calibrate-right or --calibrate-axes')
    if a.third_direction != 'forward' and not a.calibrate_axes:
        p.error('--third-direction requires --calibrate-axes')
    if (a.record or a.preview or a.task) and not a.follow:
        p.error("--record/--preview/--config/--task require --follow")
    if a.config and not (a.follow or calibration):
        p.error("--config requires --follow or --calibrate-right")
    if calibration and (a.follow or a.direct or a.execute or a.monitor or a.check or a.wifi):
        p.error("--calibrate-right is a read-only USB mode; do not combine with control modes")
    if a.follow and (a.monitor or a.check or a.execute):
        p.error("--follow cannot combine with --monitor/--check/--execute")
    if a.preview:
        a.direct = False
    if a.follow:
        from .realman_follow import FollowConfig
        c = FollowConfig.load(a.config or config_file("realman_follow.json"))
        if a.seconds is not None and not 0 < a.seconds <= c.max_duration_s:
            p.error("seconds exceeds configured duration")
        if a.record and not a.task.strip():
            p.error("--record requires --task")
    if a.direct and (a.monitor or a.check or a.execute):
        p.error("--direct cannot combine with --monitor/--check/--execute")
    if a.monitor and (a.execute or a.check):
        p.error("--monitor cannot combine with --execute/--check")
    service = None
    log = None
    try:
        if a.direct or a.follow or calibration:
            check_idle()
        if not a.wifi:
            a.serial = resolve_quest_serial(a.serial)
        if not listening():
            (ROOT / "run").mkdir(exist_ok=True)
            log = (ROOT / "run/native_service.log").open("a")
            service = subprocess.Popen([*module_command('common.runtime_paths'), '--run-service'],
                                       stdout=log, stderr=subprocess.STDOUT,env=module_environment())
            for _ in range(50):
                if service.poll() is not None:
                    raise RuntimeError("PC Service 启动失败，见 run/native_service.log")
                if listening():
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("PC Service 未就绪，见 run/native_service.log")
        if not a.wifi:
            subprocess.run(["adb", "-s", a.serial, "get-state"], check=True)
            subprocess.run(["adb", "-s", a.serial, "reverse", "tcp:63901", "tcp:63901"], check=True)
            preferred = "com.xrobotoolkit.client.quest.realman"
            installed = subprocess.run(["adb", "-s", a.serial, "shell", "pm", "path", preferred],
                                       capture_output=True, text=True)
            needs_realman = a.execute or a.direct or a.follow or calibration
            if needs_realman and "package:" not in installed.stdout:
                raise RuntimeError("缺少 XRoboToolkit RealMan 专用客户端；先执行 python examples/teleop/teleop.py install-quest，再运行校准/预览/遥操")
            package = a.app_package or (preferred if "package:" in installed.stdout else "com.xrobotoolkit.client.quest")
            if needs_realman and package != preferred:
                raise RuntimeError("需先安装带逐手柄追踪字段的 XRoboToolkit RealMan 客户端；原版仅支持只读诊断")
            if a.direct and not a.follow:
                subprocess.run(["adb", "-s", a.serial, "shell", "am", "force-stop", package], check=True)
            subprocess.run(["adb", "-s", a.serial, "shell", "am", "start", "-n",
                            package + "/com.unity3d.player.UnityPlayerActivity"], check=True)
            if a.follow and a.direct and c.direction_calibration:
                expected = c.direction_calibration.get('quest_app_pid')
                current = subprocess.run(['adb', '-s', a.serial, 'shell', 'pidof', package],
                                         capture_output=True, text=True)
                if expected and (not current.stdout.strip() or int(current.stdout.split()[0]) != expected):
                    raise RuntimeError('头显应用已重启，方向参考可能改变；请先运行 --calibrate-right')
            print("USB 已转发：头显 PC Service IP 填 127.0.0.1，Controller + Send 开启。", flush=True)
        entry = ("teleop.calibrate_camera_axes" if calibration else "teleop.realman_follow" if a.follow
                 else "vr.xrobotoolkit_input" if a.monitor else "teleop.realman_teleop")
        if a.dual_cameras:
            entry = 'recording.realman_follow_dual'
        args = module_command(entry)
        if a.dual_cameras:
            if a.camera_config:
                args += ['--camera-config', str(a.camera_config.resolve())]
        if not a.monitor and not a.follow and not calibration:
            args += ["--native"]
        for name in ("seconds", "output"):
            if getattr(a, name) is not None:
                args += ["--" + name, str(getattr(a, name))]
        if a.execute:
            args += ["--execute"]
        if a.direct:
            args += ["--direct"]
        if a.check:
            args += ["--check"]
        if a.follow:
            if a.config:
                args += ["--config", str(a.config)]
            if a.record:
                args += ["--record", "--task", a.task]
        if calibration:
            args += ['--serial', a.serial]
            if a.calibrate_axes:
                args += ['--three-axes', '--third-direction', a.third_direction]
            if a.config:
                args += ['--config', str(a.config)]
        # Inherit terminal: real execution still requires the existing START gate.
        return wait_for_child(args)
    finally:
        if service is not None:
            service.terminate()
            try:
                service.wait(timeout=4)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait()
        if log is not None:
            log.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
