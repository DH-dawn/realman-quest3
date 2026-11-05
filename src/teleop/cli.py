"""Ubuntu migration entry points. configure/check/monitor/calibrate never move the arm."""
from common.paths import config_file
import argparse
from datetime import datetime
import importlib
import ipaddress
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys

from common.device_config import resolve_quest_serial

from common.paths import ROOT, initialize_configs, module_command, module_environment


def write_config(path, data):
    if path.exists():
        backup = path.with_name(path.name+'.before-configure-'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        backup.write_bytes(path.read_bytes())
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temp.replace(path)


def configure(args):
    from .realman_follow import FollowConfig
    from .realman_teleop import Config
    ipaddress.ip_address(args.robot_ip)
    serial = resolve_quest_serial(args.serial)
    camera_serial = args.camera_serial
    if camera_serial is None:
        import pyrealsense2 as rs
        cameras = [d.get_info(rs.camera_info.serial_number) for d in rs.context().query_devices()
                   if 'D405' in d.get_info(rs.camera_info.name)]
        if len(cameras) != 1:
            raise RuntimeError('请连接一台 D405，或使用 --camera-serial 指定；仅遥操也可填相机原序列号')
        camera_serial = cameras[0]
    profile = json.loads((config_file('realman_teleop.json')).read_text())
    profile['robot_ip'], profile['robot_port'] = args.robot_ip, args.robot_port
    Config(**profile).validate()
    follow = json.loads((config_file('realman_follow.json')).read_text())
    follow['camera_serial'], follow['direction_calibration'] = camera_serial, None
    FollowConfig(**follow).validate()
    write_config(config_file('realman_teleop.json'), profile)
    write_config(config_file('realman_follow.json'), follow)
    write_config(config_file('devices.json'), {'quest_serial': serial})
    print(f'已保存：Quest={serial}, D405={camera_serial}, robot={args.robot_ip}:{args.robot_port}')
    print('旧会话的方向参考已清除；下一步运行 check、install-quest（如需要）、calibrate。')


def check(_args):
    results = []
    def probe(name, function, required=True):
        try:
            value = function()
            results.append(dict(check=name, ok=True, required=required, value=value))
        except Exception as error:
            results.append(dict(check=name, ok=False, required=required, error=str(error)))
    def os_check():
        if platform.system() != 'Linux' or platform.machine() != 'x86_64' or sys.version_info[:2] != (3,10):
            raise RuntimeError('需要 Ubuntu 22.04 x86_64 / Python 3.10')
        return platform.platform()
    def libraries():
        for module in ('numpy', 'scipy', 'pyrealsense2', 'cv2', 'Robotic_Arm.rm_robot_interface'):
            importlib.import_module(module)
        from .realman_follow import RealManModel
        # Imports only: the live profile/FK check remains in preview/run.
        return 'Python dependencies and offline RM65 model class imported'
    def runtime():
        import ctypes
        from vr.xrobotoolkit_input import SDK
        ctypes.CDLL(str(SDK))
        return str(SDK)
    def robot():
        p = json.loads((config_file('realman_teleop.json')).read_text())
        with socket.create_connection((p['robot_ip'], p['robot_port']), timeout=2) as stream:
            stream.settimeout(2)
            stream.sendall(b'{"command":"get_current_arm_state"}\r\n')
            data = b''
            while b'\n' not in data and len(data) < 65536:
                chunk = stream.recv(8192)
                if not chunk:
                    raise RuntimeError('Robot closed connection')
                data += chunk
        reply = json.loads(data.splitlines()[0])
        if reply.get('state') != 'current_arm_state':
            raise RuntimeError(str(reply))
        if reply['arm_state']['err'] != [0]:
            raise RuntimeError('机械臂反馈错误：'+str(reply['arm_state']['err']))
        return reply
    def camera():
        import pyrealsense2 as rs
        expected = json.loads((config_file('realman_follow.json')).read_text())['camera_serial']
        found = [(d.get_info(rs.camera_info.name), d.get_info(rs.camera_info.serial_number))
                 for d in rs.context().query_devices()]
        if expected not in [s for _,s in found]:
            raise RuntimeError(f'未找到配置的 D405 {expected}，发现 {found}')
        return found
    probe('OS/Python', os_check)
    probe('Python libraries', libraries)
    probe('native SDK library', runtime)
    probe('authorized Quest USB', resolve_quest_serial)
    probe('robot read-only state', robot)
    probe('D405 enumeration', camera, required=False)
    print(json.dumps({'results': results, 'robot_motion_commands_sent': 0}, ensure_ascii=False, indent=2))
    return int(any(not x['ok'] and x['required'] for x in results))


def launch(args):
    cmd = args.command
    if cmd == 'install-quest':
        from vr.quest_client import install
        return install(args.serial)
    flags = []
    if cmd == 'monitor':
        flags = ['--monitor', '--seconds', str(args.seconds)]
    elif cmd == 'calibrate':
        flags = ['--calibrate-axes', '--third-direction', args.third_direction, '--seconds', str(args.seconds)]
    else:
        from .realman_follow import FollowConfig
        c = FollowConfig.load(config_file('realman_follow.json'))
        if c.direction_calibration is None:
            raise RuntimeError('迁移后的方向尚未校准：先运行 python examples/teleop/teleop.py calibrate')
        flags = ['--follow', '--preview' if cmd == 'preview' else '--direct']
        if args.seconds is not None:
            flags += ['--seconds', str(args.seconds)]
        if cmd == 'record':
            flags += ['--record', '--task', args.task]
    if args.serial:
        flags += ['--serial', args.serial]
    # Keep the launcher signal-forwarding/save logic rather than spawning a second owner.
    import os
    os.execve(sys.executable, [*module_command('teleop.native_launcher'), *flags], module_environment())


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init-config', help='从包内模板创建缺失配置，不覆盖现有校准')
    cfg = sub.add_parser('configure', help='保存新电脑设备设置，清除旧会话方向')
    cfg.add_argument('--robot-ip', default='192.168.0.18')
    cfg.add_argument('--robot-port', type=int, default=8080)
    cfg.add_argument('--serial')
    cfg.add_argument('--camera-serial')
    sub.add_parser('check', help='只读检查，不启动遥操或改变机器人设置')
    sub.add_parser('doctor', help='完全离线环境检查；不连接机械臂、头显或相机')
    for name in ('install-quest', 'monitor', 'calibrate', 'preview', 'run', 'record'):
        item = sub.add_parser(name)
        item.add_argument('--serial')
        if name != 'install-quest':
            item.add_argument('--seconds', type=float, default=180 if name == 'calibrate' else 15 if name == 'monitor' else 30 if name == 'preview' else None)
        if name == 'calibrate':
            item.add_argument('--third-direction', choices=('forward','backward'), default='forward')
        if name == 'record':
            item.add_argument('--task', required=True)
    args = p.parse_args(argv)
    if args.command == 'init-config':
        print(json.dumps(dict(project_root=str(ROOT),created=initialize_configs()),ensure_ascii=False,indent=2))
        return 0
    if args.command == 'configure':
        configure(args)
        return 0
    if args.command == 'check':
        return check(args)
    if args.command == 'doctor':
        from common.runtime_paths import offline_checks
        result = offline_checks()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(not result['ok'])
    return launch(args)


def entry_point(argv=None):
    try:
        return main(argv)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(entry_point())
