"""Explicit installation of an already-built, user-provided Quest client APK."""
import os
from pathlib import Path
import subprocess

from common.device_config import resolve_quest_serial
from common.paths import ROOT


def install(requested_serial=None):
    apk=Path(os.environ.get('REALMAN_QUEST_APK',str(ROOT/'Android_APK/XRoboToolkit-Quest-RealMan-1.0.1-r2.apk'))).expanduser()
    if not apk.is_file():
        raise FileNotFoundError(f'Missing APK: {apk}; set REALMAN_QUEST_APK. See docs/native_runtime.md')
    serial=resolve_quest_serial(requested_serial)
    commands=(['get-state'], ['install','-r',str(apk.resolve())],
              ['shell','am','force-stop','com.xrobotoolkit.client.quest'],
              ['reverse','tcp:63901','tcp:63901'],
              ['shell','am','start','-n','com.xrobotoolkit.client.quest.realman/com.unity3d.player.UnityPlayerActivity'])
    for args in commands:
        subprocess.run(['adb','-s',serial,*args],check=True)
    return 0
