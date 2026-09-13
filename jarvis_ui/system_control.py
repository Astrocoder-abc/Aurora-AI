"""
System control: volume, media playback keys, and launching apps.

Volume uses pycaw (Windows Core Audio). If it's not installed, volume
commands log a clear message instead of crashing — everything else in
the app keeps working either way.

Media keys and app launching use only the standard library (ctypes for
virtual key sends, subprocess/os for launching), so they work with no
extra install.
"""

import ctypes
import os
import shutil
import subprocess

try:
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except (ImportError, OSError):
    PYCAW_AVAILABLE = False

# Virtual key codes for Windows media keys
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

# Common apps: name -> command. Anything not listed here is tried directly
# as a command, which covers most things already on PATH (notepad, calc,
# explorer, etc.)
APP_COMMANDS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "task manager": "taskmgr.exe",
    "control panel": "control.exe",
    "spotify": "spotify.exe",
    "chrome": "chrome.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "vs code": "code.exe",
    "visual studio code": "code.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
}


def _get_volume_interface():
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def get_volume_percent():
    if not PYCAW_AVAILABLE:
        return None
    try:
        vol = _get_volume_interface()
        return round(vol.GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return None


def set_volume_percent(percent):
    if not PYCAW_AVAILABLE:
        return False
    try:
        percent = max(0, min(100, percent))
        vol = _get_volume_interface()
        vol.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return True
    except Exception:
        return False


def adjust_volume_percent(delta):
    current = get_volume_percent()
    if current is None:
        return None
    new_val = max(0, min(100, current + delta))
    return new_val if set_volume_percent(new_val) else None


def _send_media_key(vk_code):
    """Simulates a media key press via SendInput — works system-wide,
    controls whatever app currently owns media focus (Spotify, YouTube,
    etc.), same as a physical keyboard media key would."""
    try:
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)  # KEYEVENTF_KEYUP
        return True
    except Exception:
        return False


def media_play_pause():
    return _send_media_key(VK_MEDIA_PLAY_PAUSE)


def media_next():
    return _send_media_key(VK_MEDIA_NEXT_TRACK)


def media_previous():
    return _send_media_key(VK_MEDIA_PREV_TRACK)


def launch_app(name):
    key = name.strip().lower()
    command = APP_COMMANDS.get(key, key)  # fall back to trying the name directly

    # os.startfile uses ShellExecute, which respects the Windows "App
    # Paths" registry — the same mechanism the Start Menu and Run dialog
    # use to find GUI apps by name (Chrome, Spotify, etc. register
    # themselves there even though they're not on the system PATH).
    # This is far more reliable than a shell command lookup for apps
    # installed the normal way.
    try:
        os.startfile(command)
        return True
    except Exception:
        pass

    # Fall back to a PATH-based lookup, but verify the executable is
    # actually resolvable FIRST. subprocess.Popen(cmd, shell=True) does
    # NOT raise an exception just because the command isn't found — it
    # spawns cmd.exe successfully, and cmd.exe fails internally instead,
    # which made every launch attempt silently report false success.
    resolved = shutil.which(command)
    if resolved:
        try:
            subprocess.Popen([resolved])
            return True
        except Exception:
            return False
    return False
