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

"CALL ME": my_number.txt (create it yourself, one line, e.g.
+15551234567) lets "Aurora, call me" dial your own number from the
connected phone — handy as a makeshift "find my phone" or reminder call.
It's just a shortcut for calling a specific number; contacts.json works
the same way for named contacts.

SECURITY NOTE: pin.txt and my_number.txt store plaintext on this PC.
Only use this if you're comfortable with that tradeoff, and keep this
machine physically secure. Anyone with access to this PC and your USB
cable would be able to unlock your phone if it's plugged in.
"""
import json
import os
import re
import subprocess
import urllib.parse

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PIN_FILE = os.path.join(BASE, "phone_pin.txt")
CONTACTS_FILE = os.path.join(BASE, "contacts.json")
MY_NUMBER_FILE = os.path.join(BASE, "my_number.txt")
WIFI_FILE = os.path.join(BASE, "wifi_networks.json")

KNOWN_APPS = {
    "instagram": "com.instagram.android", "whatsapp": "com.whatsapp",
    "youtube": "com.google.android.youtube", "chrome": "com.android.chrome",
    "spotify": "com.spotify.music", "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps", "telegram": "org.telegram.messenger",
    "snapchat": "com.snapchat.android", "camera": "com.android.camera",
}


def _adb(args, timeout=15):
    try:
        r = subprocess.run(["adb"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout + r.stderr
    except FileNotFoundError:
        return False, "adb not found — install platform-tools and add it to PATH"
    except Exception as e:
        return False, str(e)


def _sh(cmd, timeout=15):
    return _adb(["shell", cmd], timeout)


def _clean(s):
    return re.sub(r"[^\w\s.\-+]", "", s or "").strip()


def is_device_connected():
    ok, out = _adb(["devices"])
    if not ok:
        return False, out
    lines = [l for l in out.strip().split("\n")[1:] if l.strip()]
    return any(l.split()[-1] == "device" for l in lines), out


def wake_screen():
    return _adb(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])[0]


def unlock_with_pin():
    if not os.path.exists(PIN_FILE):
        return False, "No phone_pin.txt found"
    pin = open(PIN_FILE).read().strip()
    if not pin:
        return False, "phone_pin.txt is empty"
    wake_screen()
    _adb(["shell", "input", "swipe", "500", "1500", "500", "500"])
    ok, out = _adb(["shell", "input", "text", pin])
    if ok:
        _adb(["shell", "input", "keyevent", "KEYCODE_ENTER"])
    return ok, out


def load_contacts():
    try:
        with open(CONTACTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_my_number():
    try:
        n = open(MY_NUMBER_FILE).read().strip()
        if n:
            return n
    except Exception:
        pass
    return load_contacts().get("me")


def call_number(number):
    return _sh(f"am start -a android.intent.action.CALL -d 'tel:{_clean(number)}'")


# ---- WhatsApp calls -----------------------------------------------------------

def _rows(output):
    return [dict(re.findall(r"(\w+)=([^,]*?)(?:,\s|$)", l)) for l in output.splitlines() if l.startswith("Row:")]


def whatsapp_call(target, video=False):
    """Finds the WhatsApp call entry for a contact (by contacts.json name/number
    or saved contact name) and starts a voice/video call. Returns (ok, info)."""
    target = _clean(target).lower()
    number = re.sub(r"\D", "", load_contacts().get(target, ""))
    mime = "vnd.android.cursor.item/vnd.com.whatsapp." + ("video.call" if video else "voip.call")
    ok, out = _sh("content query --uri content://com.android.contacts/data "
                  "--projection _id:display_name:data1 "
                  f"--where \"mimetype='{mime}'\"", timeout=20)
    if not ok:
        return False, "I couldn't read WhatsApp contacts from the phone."
    for row in _rows(out):
        name, jid = row.get("display_name", ""), row.get("data1", "")
        if (number and number[-10:] in jid) or (target and target in name.lower()):
            ok, out = _sh(f"am start -a android.intent.action.VIEW -d content://com.android.contacts/data/{row['_id']} "
                          f"-t '{mime}' -p com.whatsapp")
            return (True, name) if ok else (False, "WhatsApp wouldn't start the call.")
    return False, f"I couldn't find {target} in your WhatsApp contacts."


# ---- wifi ---------------------------------------------------------------------

def connect_wireless(port=5555):
    ok, out = _adb(["tcpip", str(port)])
    if not ok:
        return False, "Plug the phone in by USB first."
    ok, out = _sh("ip route")
    m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        return False, "I couldn't find the phone's wifi address — is it on wifi?"
    ok, out = _adb(["connect", f"{m.group(1)}:{port}"])
    return ("connected" in out.lower()), (out.strip() or "Connection failed.")


def wifi_set(on):
    return _sh(f"svc wifi {'enable' if on else 'disable'}")[0]


def wifi_connect(name):
    name = name.strip().lower()
    wifi_set(True)
    try:
        net = json.load(open(WIFI_FILE)).get(name)
    except Exception:
        net = None
    if net:
        ok, out = _sh(f"cmd wifi connect-network '{_clean(net['ssid'])}' wpa2 '{_clean(net.get('password', ''))}'")
        return ok, (f"Connecting your phone to {name}." if ok else "The phone rejected that wifi connection.")
    return True, f"Wifi is on. Add {name} to wifi_networks.json for me to join it; saved networks reconnect on their own."


# ---- apps, web, search ----------------------------------------------------------

def _packages():
    ok, out = _sh("pm list packages")
    return [l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")] if ok else []


def open_app(name):
    name = _clean(name).lower()
    pkg = KNOWN_APPS.get(name)
    if not pkg:
        key = name.replace(" ", "")
        pkg = next((p for p in _packages() if key in p.lower()), None)
    if not pkg:
        return False
    return _sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")[0]


def web_search(query):
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    return _sh(f"am start -a android.intent.action.VIEW -d '{url}'")[0]


def search_phone(query, kind=None):
    """Returns {"contacts": [(name, number)], "apps": [pkg], "files": [path]}."""
    q = _clean(query)
    res = {"contacts": [], "apps": [], "files": []}
    if not q:
        return res
    if kind in (None, "contacts"):
        ok, out = _sh("content query --uri content://com.android.contacts/data/phones "
                      f"--projection display_name:data1 --where \"display_name like '%{q}%'\"")
        seen = set()
        for r in _rows(out) if ok else []:
            item = (r.get("display_name", "?"), r.get("data1", ""))
            if item not in seen:
                seen.add(item)
                res["contacts"].append(item)
    if kind in (None, "apps"):
        key = q.lower().replace(" ", "")
        res["apps"] = [p for p in _packages() if key in p.lower()][:10]
    if kind in (None, "files"):
        ok, out = _sh(f"find /sdcard -maxdepth 5 -iname '*{q.replace(' ', '*')}*' 2>/dev/null | head -10", timeout=30)
        res["files"] = [l.strip() for l in out.splitlines() if l.startswith("/")] if ok else []
    return res