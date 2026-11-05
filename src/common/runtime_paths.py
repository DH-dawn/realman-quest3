"""Runtime paths and offline checks; no device initialization or robot sockets."""
from .paths import config_file
import json
import os
from pathlib import Path

from .paths import ROOT


def _path(value):
    p = Path(value).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


def service_dir():
    if os.environ.get('REALMAN_SERVICE_DIR'):
        return _path(os.environ['REALMAN_SERVICE_DIR'])
    local = ROOT / '.native/pc-service/opt/apps/roboticsservice'
    return local if (local/'RoboticsServiceProcess').is_file() else Path('/opt/apps/roboticsservice')


def sdk_path():
    value = os.environ.get('XROBOT_SDK_LIBRARY')
    return _path(value) if value else service_dir()/'SDK/x64/libPXREARobotSDK.so'


def offline_checks():
    import ctypes
    import importlib
    import importlib.metadata
    import platform
    import sys
    checks = []

    def probe(name, fn):
        try:
            checks.append({'check': name, 'ok': True, 'value': fn()})
        except Exception as exc:
            checks.append({'check': name, 'ok': False, 'error': str(exc)})

    def python():
        if sys.version_info[:2] != (3,10) or platform.machine() != 'x86_64' or platform.system() != 'Linux':
            raise RuntimeError('Requires Linux x86_64 and Python 3.10')
        return {'executable': sys.executable, 'version': platform.python_version(),
                'platform': f'{platform.system()} {platform.release()} ({platform.machine()})'}
    probe('Python/platform', python)
    for module in ('numpy','scipy','cv2','pyrealsense2','Robotic_Arm.rm_robot_interface'):
        def dependency(name=module):
            m = importlib.import_module(name)
            dist = {'cv2':'opencv-python-headless','Robotic_Arm.rm_robot_interface':'Robotic_Arm'}.get(name,name)
            try:
                version = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                version = getattr(m,'__version__','imported')
            return {'version': version, 'module': str(getattr(m,'__file__',''))}
        probe(module, dependency)

    def sdk():
        p = sdk_path()
        lib = ctypes.CDLL(str(p))
        for name in ('PXREAInit','PXREADeinit'):
            getattr(lib,name)
        return str(p)
    probe('XR native SDK (load only)', sdk)
    def service():
        p = service_dir()/'RoboticsServiceProcess'
        if not p.is_file() or not os.access(p,os.X_OK):
            raise RuntimeError(f'PC Service missing or not executable: {p}')
        return str(p)
    probe('PC Service executable (not started)', service)
    def config():
        from teleop.realman_follow import FollowConfig
        from teleop.realman_teleop import Config
        c = FollowConfig.load(config_file('realman_follow.json'))
        r = Config.load(config_file('realman_teleop.json'))
        return {'control_hz':c.control_hz,'expected_robot':r.expected_product,
                'direction_calibrated':c.direction_calibration is not None}
    probe('Control profiles (local files)', config)
    return {'ok':all(c['ok'] for c in checks),'checks':checks,
            'robot_connected':False,'robot_motion_commands_sent':0}


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--service-dir',action='store_true')
    p.add_argument('--run-service',action='store_true',help='显式启动PC Service；普通doctor不启动服务')
    args = p.parse_args()
    if args.run_service:
        folder=service_dir()
        executable=folder/'RoboticsServiceProcess'
        if not executable.is_file():
            raise SystemExit('PC Service missing; see docs/native_runtime.md')
        env=os.environ.copy()
        env.pop('PYTHONPATH',None)
        env.update(LD_LIBRARY_PATH=f'{folder}:{folder}/lib:{folder}/SDK/x64',
                   QT_PLUGIN_PATH=str(folder/'plugins'),QT_QPA_PLATFORM='offscreen')
        os.chdir(folder)
        os.execve(str(executable),[str(executable)],env)
    elif args.service_dir:
        print(service_dir())
    else:
        result = offline_checks()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(0 if result['ok'] else 1)
