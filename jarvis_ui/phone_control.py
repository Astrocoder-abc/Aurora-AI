"""
Phone control via ADB (Android Debug Bridge) — Google's own official tool
for talking to an Android device from a PC. This is legitimate device
automation for a phone YOU own and control, the same mechanism apps like
Tasker use — it is NOT a way to bypass another person's lock screen or
access a device you don't own.

REQUIREMENTS (all on your end, one-time setup):
  1. Install ADB: https://developer.android.com/tools/releases/platform-tools
     (unzip it somewhere and add that folder to your PATH, or just note
     the path to adb.exe)
  2. On your phone: Settings -> About phone -> tap "Build number" 7 times
     to unlock Developer Options, then Settings -> Developer Options ->
     enable "USB debugging"
  3. Connect your phone via USB, and tap "Allow" on the USB debugging
     prompt that appears on the phone screen
  4. Run `adb devices` in a terminal to confirm your phone shows up

WHAT THIS CAN ACTUALLY DO:
  - Wake the screen (always works)
  - "Unlock" only in the sense of sending your PIN as keystrokes after
    waking the screen — this requires you to store your own PIN locally
    in pin.txt (see below), and only works if you already know and
    consent to storing it. This is NOT a security bypass; ADB simply
    types on your behalf what you could type yourself.
  - Place a call to a number (requires you to confirm/dial on-device
    for some Android versions — ADB can start the call intent, but the
    phone may still require the CALL_PHONE permission grant screen once)

SECURITY NOTE: pin.txt stores your PIN in plaintext on this PC. Only use
this if you're comfortable with that tradeoff, and keep this machine
physically secure. Anyone with access to this PC and your USB cable
would be able to unlock your phone if it's plugged in.
"""

import os

from .paths import PROJECT_ROOT
import subprocess

PIN_FILE = os.path.join(str(PROJECT_ROOT), "phone_pin.txt")
CONTACTS_FILE = os.path.join(str(PROJECT_ROOT), "contacts.json")


def _adb(args):
    try:
        result = subprocess.run(["adb"] + args, capture_output=True, text=True, timeout=15)
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, "adb not found — install platform-tools and add it to PATH"
    except Exception as e:
        return False, str(e)


def is_device_connected():
    ok, output = _adb(["devices"])
    if not ok:
        return False, output
    lines = [l for l in output.strip().split("\n")[1:] if l.strip()]
    connected = len(lines) == 1 and len(lines[0].split()) >= 2 and lines[0].split()[1] == "device"
    return connected, output


def wake_screen():
    ok, _ = _adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    return ok


def unlock_with_pin():
    """Wakes the screen, swipes up (for phones that need it), then types
    the PIN from phone_pin.txt. Only works if you've created that file
    yourself with your own PIN — see the module docstring."""
    if not os.path.exists(PIN_FILE):
        return False, "No phone_pin.txt found — see phone_control.py for setup"
    with open(PIN_FILE, "r") as f:
        pin = f.read().strip()
    if not pin.isascii() or not pin.isdigit():
        return False, "phone_pin.txt must contain only digits"

    wake_screen()
    _adb(["shell", "input", "swipe", "500", "1500", "500", "500"])  # swipe up
    ok, output = _adb(["shell", "input", "text", pin])
    if ok:
        _adb(["shell", "input", "keyevent", "KEYCODE_ENTER"])
    return ok, output


def load_contacts():
    import json
    if not os.path.exists(CONTACTS_FILE):
        return {}
    try:
        with open(CONTACTS_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return {str(name).strip().lower(): str(number) for name, number in data.items()
                    if isinstance(number, (str, int))}
    except Exception:
        return {}


def call_number(number):
    ok, output = _adb(["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}"])
    return ok, output
