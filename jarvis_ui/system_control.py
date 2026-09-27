"""
System control: volume, media playback keys, launching apps, system
status/screenshot/lock, and Game Companion helpers (CPU temp, foreground
app, screen recording).

Volume uses pycaw (Windows Core Audio). If it's not installed, volume
commands log a clear message instead of crashing — everything else in
the app keeps working either way.

Media keys and app launching use only the standard library (ctypes for
virtual key sends, subprocess/os for launching), so they work with no
extra install.

GAME COMPANION: reports honest system-level signals only (CPU/RAM load,
CPU temp if exposed, foreground app, whether a recording is running) —
deliberately NOT reading anything out of a game process, so this can't
be used as a cheat overlay. Screen recording uses ffmpeg (must be on
PATH) capturing the whole desktop, not any specific game window.
"""

import ctypes
import os
import shutil
import subprocess
import time

try:
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except ImportError:
    PYCAW_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False

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
    "vscode": "code.exe",
    "arduino": "arduino.exe",
    "arduino ide": "arduino.exe",
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
    set_volume_percent(new_val)
    return new_val


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


# ---- system status / screenshot / lock -------------------------------------

def get_system_status():
    """Short spoken-friendly CPU/RAM/battery readout, or None if psutil
    isn't installed."""
    if not PSUTIL_AVAILABLE:
        return None
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory().percent
    parts = [f"CPU at {round(cpu)} percent", f"memory at {round(mem)} percent"]
    battery = psutil.sensors_battery()
    if battery is not None:
        state = "charging" if battery.power_plugged else "on battery"
        parts.append(f"battery at {round(battery.percent)} percent, {state}")
    return ", ".join(parts) + "."


def take_screenshot():
    """Saves a screenshot to the user's Pictures folder using the
    standard library only (ImageGrab from Pillow if available, else a
    ctypes-based fallback isn't implemented — Pillow is a common enough
    dependency that this just uses it, and fails cleanly if missing)."""
    try:
        from PIL import ImageGrab
    except ImportError:
        return False
    try:
        pictures_dir = os.path.join(os.path.expanduser("~"), "Pictures")
        os.makedirs(pictures_dir, exist_ok=True)
        path = os.path.join(pictures_dir, f"aurora_screenshot_{int(time.time())}.png")
        ImageGrab.grab().save(path)
        return True
    except Exception:
        return False


def lock_workstation():
    try:
        ctypes.windll.user32.LockWorkStation()
        return True
    except Exception:
        return False


# ---- Game Companion Mode ----------------------------------------------------
# Deliberately non-cheating: only reports system-level signals (CPU/RAM
# load, CPU temp if the sensor is exposed, which app is focused, and
# whether a recording is running) — nothing is read out of any game
# process or memory.

RECORDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "recordings")
_recording_process = None


def get_cpu_temp():
    """Best-effort CPU temperature in Celsius, or None. Most consumer
    Windows machines don't expose this via psutil (no WMI temp sensor
    driver) — None just means 'not available', not an error."""
    if not PSUTIL_AVAILABLE or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        temps = psutil.sensors_temperatures()
        for entries in (temps or {}).values():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        pass
    return None


def get_foreground_process_name():
    """Name of whatever window currently has focus (e.g. 'valorant.exe') —
    just a label for the dashboard, read-only, no interaction with it."""
    if not PSUTIL_AVAILABLE:
        return None
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name()
    except Exception:
        return None


def start_recording(filename=None):
    """Screen capture via ffmpeg (must be installed and on PATH). Records
    the whole desktop, not any specific game window — safe/non-invasive."""
    global _recording_process
    if _recording_process is not None:
        return False, "Already recording."
    os.makedirs(RECORDING_DIR, exist_ok=True)
    path = os.path.join(RECORDING_DIR, filename or f"session_{int(time.time())}.mp4")
    try:
        _recording_process = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30", "-i", "desktop", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, path
    except FileNotFoundError:
        _recording_process = None
        return False, "ffmpeg not found — install it and add it to PATH."
    except Exception as e:
        _recording_process = None
        return False, str(e)


def stop_recording():
    global _recording_process
    if _recording_process is None:
        return False, "Not recording."
    try:
        _recording_process.communicate(input=b"q", timeout=5)
    except Exception:
        _recording_process.terminate()
    _recording_process = None
    return True, "Recording stopped."


def is_recording():
    return _recording_process is not None
