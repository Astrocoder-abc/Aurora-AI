"""
Aurora system layer (Windows). MERGED from: system_control, phone_control,
code_control (file/editor half), vision (QR/barcode half), knowledge_vault, notes.

  PC:      volume, media keys, launch apps, status/screenshot/lock, game-companion helpers
  PHONE:   Android over ADB (calls, WhatsApp calls, apps, wifi, search) - your own phone only
  CODE:    project/sketch scaffolding, backups, open in VS Code / Arduino IDE
  VISION:  QR / barcode decoding
  VAULT:   local keyword search over folders you add
The AI half of code generation lives in brain.py.
"""
import ctypes
import difflib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

try:
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    PYCAW_AVAILABLE = True
except Exception:
    PYCAW_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _p(name):
    return os.path.join(BASE, name)


def _load_json(name, default):
    try:
        with open(_p(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ============================================================================
# PC: volume, media, apps, status
# ============================================================================
VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB3

APP_COMMANDS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "file explorer": "explorer.exe", "explorer": "explorer.exe", "paint": "mspaint.exe",
    "task manager": "taskmgr.exe", "control panel": "control.exe", "spotify": "spotify.exe",
    "chrome": "chrome.exe", "word": "winword.exe", "excel": "excel.exe",
    "powerpoint": "powerpnt.exe", "vs code": "code.exe", "visual studio code": "code.exe",
    "vscode": "code.exe", "arduino": "arduino.exe", "arduino ide": "arduino.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe", "powershell": "powershell.exe",
}
APP_RE = re.compile(r"\b(" + "|".join(re.escape(a) for a in sorted(APP_COMMANDS, key=len, reverse=True)) + r")\b")


def _volume_interface():
    dev = AudioUtilities.GetSpeakers()
    if hasattr(dev, "EndpointVolume"):          # newer pycaw returns a wrapper
        return dev.EndpointVolume
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def get_volume_percent():
    if not PYCAW_AVAILABLE:
        return None
    try:
        return round(_volume_interface().GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return None


def set_volume_percent(percent):
    if not PYCAW_AVAILABLE:
        return False
    try:
        _volume_interface().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
        return True
    except Exception:
        return False


def adjust_volume_percent(delta):
    cur = get_volume_percent()
    if cur is None:
        return None
    new = max(0, min(100, cur + delta))
    set_volume_percent(new)
    return new


def _media_key(vk):
    try:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        return True
    except Exception:
        return False


def media_play_pause(): return _media_key(VK_MEDIA_PLAY_PAUSE)
def media_next(): return _media_key(VK_MEDIA_NEXT_TRACK)
def media_previous(): return _media_key(VK_MEDIA_PREV_TRACK)


def launch_app(name):
    key = name.strip().lower()
    command = APP_COMMANDS.get(key, key)
    try:
        os.startfile(command)          # ShellExecute honours the "App Paths" registry
        return True
    except Exception:
        pass
    resolved = shutil.which(command)
    if resolved:
        try:
            subprocess.Popen([resolved])
            return True
        except Exception:
            return False
    return False


CLOSE_EXES = {"calculator": "CalculatorApp.exe", "calc": "CalculatorApp.exe"}
NEVER_CLOSE = {"explorer", "file explorer", "cmd", "command prompt", "powershell", "task manager", "control panel"}


def close_app(name):
    """Gracefully closes a PC app (no /F, so unsaved-work prompts still appear)."""
    key = name.strip().lower()
    if key in NEVER_CLOSE:
        return False
    exe = CLOSE_EXES.get(key) or APP_COMMANDS.get(key, key if key.endswith(".exe") else key + ".exe")
    try:
        return subprocess.run(["taskkill", "/IM", exe], capture_output=True, timeout=10,
                              creationflags=0x08000000).returncode == 0
    except Exception:
        return False


def get_system_status():
    if not PSUTIL_AVAILABLE:
        return None
    parts = [f"CPU at {round(psutil.cpu_percent(interval=0.3))} percent",
             f"memory at {round(psutil.virtual_memory().percent)} percent"]
    b = psutil.sensors_battery()
    if b is not None:
        parts.append(f"battery at {round(b.percent)} percent, {'charging' if b.power_plugged else 'on battery'}")
    return ", ".join(parts) + "."


def take_screenshot():
    try:
        from PIL import ImageGrab
        d = os.path.join(os.path.expanduser("~"), "Pictures")
        os.makedirs(d, exist_ok=True)
        ImageGrab.grab().save(os.path.join(d, f"aurora_screenshot_{int(time.time())}.png"))
        return True
    except Exception:
        return False


def capture_screen(max_width=1600, hide_aurora=True):
    """Screenshot as a BGR array (None on failure). Aurora's own fullscreen window is minimised first so the
    user's real screen is captured, then restored."""
    hwnd = None
    if hide_aurora and os.name == "nt":
        try:
            import pygame
            hwnd = pygame.display.get_wm_info().get("window")
        except Exception:
            hwnd = None
    img = None
    try:
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)        # SW_MINIMIZE
            time.sleep(0.7)
        from PIL import ImageGrab
        img = ImageGrab.grab()
    except Exception:
        img = None
    finally:
        if hwnd:
            try:
                ctypes.windll.user32.ShowWindow(hwnd, 9)    # SW_RESTORE
            except Exception:
                pass
    if img is None:
        return None
    import cv2
    import numpy as np
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def lock_workstation():
    try:
        ctypes.windll.user32.LockWorkStation()
        return True
    except Exception:
        return False


# ---- Game Companion helpers (system-level only, nothing read from games) ----
RECORDING_DIR = _p("recordings")
_rec = None


def get_cpu_temp():
    if not PSUTIL_AVAILABLE or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        for entries in (psutil.sensors_temperatures() or {}).values():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        pass
    return None


def get_foreground_process_name():
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
    global _rec
    if _rec is not None:
        return False, "Already recording."
    os.makedirs(RECORDING_DIR, exist_ok=True)
    path = os.path.join(RECORDING_DIR, filename or f"session_{int(time.time())}.mp4")
    try:
        _rec = subprocess.Popen(["ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30", "-i", "desktop", path],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, path
    except FileNotFoundError:
        _rec = None
        return False, "ffmpeg not found - install it and add it to PATH."
    except Exception as e:
        _rec = None
        return False, str(e)


def stop_recording():
    global _rec
    if _rec is None:
        return False, "Not recording."
    try:
        _rec.communicate(input=b"q", timeout=5)
    except Exception:
        _rec.terminate()
    _rec = None
    return True, "Recording stopped."


def is_recording():
    return _rec is not None


# ============================================================================
# PHONE (ADB) - phone_pin.txt, my_number.txt, contacts.json, wifi_networks.json
# ============================================================================
def _adb(args, timeout=15):
    try:
        r = subprocess.run(["adb"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return False, "adb not found - install platform-tools and add it to PATH"
    except Exception as e:
        return False, str(e)


def _shell(cmd, timeout=15):
    return _adb(["shell", cmd], timeout)     # one string -> parsed by the phone's shell


def is_device_connected():
    ok, out = _adb(["devices"])
    if not ok:
        return False, out
    rows = [l.split() for l in out.splitlines()[1:] if l.strip()]
    return any(len(r) >= 2 and r[1] == "device" for r in rows), out


def wake_screen():
    return _shell("input keyevent KEYCODE_WAKEUP")[0]


def unlock_with_pin():
    try:
        pin = open(_p("phone_pin.txt")).read().strip()
    except Exception:
        return False, "No phone_pin.txt found"
    if not pin.isdigit():
        return False, "phone_pin.txt must contain digits only"
    wake_screen()
    _shell("input swipe 500 1500 500 500")
    ok, out = _shell(f"input text {pin}")
    if ok:
        _shell("input keyevent KEYCODE_ENTER")
    return ok, out


def load_contacts():
    return {k.lower(): v for k, v in _load_json("contacts.json", {}).items()}


def load_my_number():
    try:
        n = open(_p("my_number.txt")).read().strip()
        if n:
            return n
    except Exception:
        pass
    return load_contacts().get("me")


def call_number(number):
    return _shell(f"am start -a android.intent.action.CALL -d tel:{re.sub(r'[^0-9+]', '', number)}")


def connect_wireless(port=5555):
    ok, out = _adb(["tcpip", str(port)])
    if not ok:
        return False, out
    time.sleep(1.5)
    _, out = _shell("ip -f inet addr show wlan0")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        return False, "I couldn't read the phone's wifi address. Is it on wifi?"
    ok, out = _adb(["connect", f"{m.group(1)}:{port}"])
    return ("connected" in out.lower()), (m.group(1) if "connected" in out.lower() else out)


def wifi_set(on):
    return _shell(f"svc wifi {'enable' if on else 'disable'}")[0]


def wifi_connect(alias):
    """wifi_networks.json: {"home": {"ssid": "MyNet", "password": "secret"}} (password "" = open)."""
    net = _load_json("wifi_networks.json", {}).get(alias.lower())
    wifi_set(True)
    if not net:
        return False, f"I turned wifi on, but I have no saved network called {alias}. Add it to wifi_networks.json."
    ssid, pw = net.get("ssid", alias), net.get("password", "")
    sec = f"wpa2 '{pw}'" if pw else "open"
    ok, out = _shell(f"cmd wifi connect-network '{ssid}' {sec}")
    return ok, (f"Connecting your phone to {ssid}." if ok else f"The phone refused: {out[:80]}")


APP_PKGS = {
    "instagram": "com.instagram.android", "whatsapp": "com.whatsapp", "youtube": "com.google.android.youtube",
    "chrome": "com.android.chrome", "spotify": "com.spotify.music", "maps": "com.google.android.apps.maps",
    "gmail": "com.google.android.gm", "telegram": "org.telegram.messenger", "snapchat": "com.snapchat.android",
    "facebook": "com.facebook.katana", "twitter": "com.twitter.android", "photos": "com.google.android.apps.photos",
    "settings": "com.android.settings",
}


def _packages():
    _, out = _shell("pm list packages")
    return [l[8:].strip() for l in out.splitlines() if l.startswith("package:")]


def _resolve_pkg(name):
    key = name.lower().strip()
    pkg = APP_PKGS.get(key)
    if not pkg:
        cands = [p for p in _packages() if key.replace(" ", "") in p.lower()]
        pkg = min(cands, key=len) if cands else None
    return pkg


def close_phone_app(name):
    pkg = _resolve_pkg(name)
    return bool(pkg) and _shell(f"am force-stop {pkg}")[0]


def open_app(name):
    pkg = _resolve_pkg(name)
    if not pkg:
        return False
    ok, out = _shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    return ok and "No activities found" not in out


def web_search(query):
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    return _shell(f"am start -a android.intent.action.VIEW -d '{url}'")[0]


def _phone_contacts():
    _, out = _shell("content query --uri content://com.android.contacts/data/phones --projection display_name:data1")
    return [(n.strip(), num.strip()) for n, num in re.findall(r"display_name=(.*?), data1=(.*)$", out, re.M)]


def whatsapp_call(target, video=False):
    """Returns (ok, display_name_or_error_message)."""
    name = target.strip()
    mime = "vnd.android.cursor.item/vnd.com.whatsapp." + ("video.call" if video else "voip.call")
    _, out = _shell(f"content query --uri content://com.android.contacts/data --projection _id:display_name "
                    f"--where \"mimetype='{mime}'\"")
    for rid, dn in re.findall(r"_id=(\d+), display_name=(.*)$", out, re.M):
        if name.lower() in dn.lower():
            ok, _ = _shell(f"am start -a android.intent.action.VIEW -d content://com.android.contacts/data/{rid} "
                           f"-t {mime} -p com.whatsapp")
            return ok, dn.strip()
    number = load_contacts().get(name.lower()) or (re.sub(r"[^0-9]", "", name) if re.fullmatch(r"[\d+\s\-()]{6,}", name) else None)
    if number:
        ok, _ = _shell(f"am start -a android.intent.action.VIEW -d 'https://wa.me/{re.sub(r'[^0-9]', '', number)}'")
        return ok, f"{name}. I opened the chat, so tap the call button"
    return False, f"I couldn't find {name} in your WhatsApp contacts."


def search_phone(query, kind=None):
    res = {"contacts": [], "apps": [], "files": []}
    q = query.lower().strip()
    if kind in (None, "contacts"):
        res["contacts"] = [(n, num) for n, num in _phone_contacts() if q in n.lower() or q.replace(" ", "") in num.replace(" ", "")][:8]
    if kind in (None, "apps"):
        res["apps"] = [p for p in _packages() if q.replace(" ", "") in p.lower()][:8]
    if kind in (None, "files"):
        safe = re.sub(r"[^\w .-]", "", q)
        if safe:
            _, out = _shell(f"find /sdcard -maxdepth 4 -iname '*{safe}*' 2>/dev/null | head -8")
            res["files"] = [l.strip() for l in out.splitlines() if l.startswith("/")]
    return res


# ============================================================================
# CODE FILES + EDITORS (AI generation is in brain.py)
# ============================================================================
PROJECTS_ROOT, ARDUINO_ROOT = _p("projects"), _p("arduino_sketches")
LANG_EXT = {"python": ".py", "javascript": ".js", "js": ".js", "cpp": ".cpp", "c++": ".cpp", "c": ".c", "html": ".html"}
ARDUINO_TEMPLATE = "void setup() {\n  Serial.begin(9600);\n\n}\n\nvoid loop() {\n\n}\n"


def safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9_\-]", "", re.sub(r"[^a-zA-Z0-9_\- ]", "", name).strip().replace(" ", "_")) or "untitled"


def _template(language, name):
    return {
        "python": f'"""{name}"""\n\n\ndef main():\n    pass\n\n\nif __name__ == "__main__":\n    main()\n',
        "javascript": f"// {name}\n\nfunction main() {{\n\n}}\n\nmain();\n",
        "cpp": f"// {name}\n#include <iostream>\n\nint main() {{\n    return 0;\n}}\n",
        "c": f"// {name}\n#include <stdio.h>\n\nint main(void) {{\n    return 0;\n}}\n",
        "html": f"<!DOCTYPE html>\n<html>\n<head><title>{name}</title></head>\n<body>\n\n</body>\n</html>\n",
    }.get({"js": "javascript", "c++": "cpp"}.get(language, language), f"# {name}\n")


def resolve_project_path(name):
    projects = {k.lower(): v for k, v in _load_json("code_projects.json", {}).items()}
    if name.strip().lower() in projects:
        return projects[name.strip().lower()]
    safe = safe_filename(name)
    if os.path.isdir(PROJECTS_ROOT):
        for f in os.listdir(PROJECTS_ROOT):
            if os.path.splitext(f)[0] == safe:
                return os.path.join(PROJECTS_ROOT, f)
    return None


def find_arduino_sketch(name):
    projects = {k.lower(): v for k, v in _load_json("code_projects.json", {}).items()}
    if name.strip().lower() in projects:
        return projects[name.strip().lower()]
    safe = safe_filename(name)
    cand = os.path.join(ARDUINO_ROOT, safe, f"{safe}.ino")
    return cand if os.path.exists(cand) else None


def create_code_file(name, language="python"):
    ext = LANG_EXT.get(language.lower(), ".txt")
    os.makedirs(PROJECTS_ROOT, exist_ok=True)
    path = os.path.join(PROJECTS_ROOT, safe_filename(name) + ext)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_template(language.lower(), name))
    return path


def create_arduino_sketch(name):
    safe = safe_filename(name)
    d = os.path.join(ARDUINO_ROOT, safe)
    os.makedirs(d, exist_ok=True)
    ino = os.path.join(d, f"{safe}.ino")
    if not os.path.exists(ino):
        with open(ino, "w", encoding="utf-8") as f:
            f.write(ARDUINO_TEMPLATE)
    return ino


def get_or_make_path(name, language="python"):
    existing = resolve_project_path(name) or find_arduino_sketch(name)
    if existing:
        return existing
    return create_arduino_sketch(name) if language.lower() in ("arduino", "ino") else create_code_file(name, language)


def write_file_content(path, content):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path):                       # one-step backup of whatever was there
        try:
            shutil.copyfile(path, path + ".bak")
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def summarize_diff(old, new):
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))
    a = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    r = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    if not a and not r:
        return "no changes"
    return " and ".join(x for x in (f"{a} line{'s' if a != 1 else ''} added" if a else "",
                                    f"{r} line{'s' if r != 1 else ''} removed" if r else "") if x)


def open_in_vscode(path):
    if not os.path.exists(path):
        return False, f"Path not found: {path}"
    try:
        r = subprocess.run(f'code "{path}"', shell=True, capture_output=True, text=True, timeout=10)
        return (True, "") if r.returncode == 0 else (False, (r.stdout + r.stderr).strip() or "'code' isn't on PATH")
    except Exception as e:
        return False, str(e)


def open_in_arduino(path):
    if not os.path.exists(path):
        return False, f"Path not found: {path}"
    try:
        os.startfile(path)
        return True, ""
    except Exception as e:
        return False, str(e)


def open_in_editor(path):
    return (open_in_arduino if path.endswith(".ino") else open_in_vscode)(path)


# ============================================================================
# VISION: QR / barcodes
# ============================================================================
def read_codes(frame):
    import cv2
    codes = []
    try:
        ok, decoded, _, _ = cv2.QRCodeDetector().detectAndDecodeMulti(frame)
        if ok:
            codes += [d for d in decoded if d]
    except Exception:
        pass
    try:
        res = cv2.barcode.BarcodeDetector().detectAndDecodeWithType(frame)
        if res[0]:
            info, types = res[1], res[2]
            info, types = ([info], [types]) if isinstance(info, str) else (info, types)
            codes += [f"{d} ({t})" for d, t in zip(info, types) if d]
    except Exception:
        pass
    return codes


# ============================================================================
# NOTES
# ============================================================================
NOTES_FILE = _p("notes.txt")


def add_note(text):
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M')}] {text}\n")


def read_notes(n=5):
    try:
        lines = [l.strip() for l in open(NOTES_FILE, encoding="utf-8") if l.strip()]
    except Exception:
        return []
    return [re.sub(r"^\[.*?\]\s*", "", l) for l in lines[-n:]]


def clear_notes():
    open(NOTES_FILE, "w").close()


# ============================================================================
# KNOWLEDGE VAULT: keyword search over folders in vault_folders.json
# ============================================================================
VAULT_TEXT_EXTS = {".txt", ".md", ".py", ".json", ".csv", ".log"}
_STOP = set("the a an and or of to in on for is are was were with that this it i my me have has did do does what everything about write wrote find you your".split())


def _vwords(text):
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOP]


def vault_add_folder(path):
    path = os.path.normpath(path.strip().strip('"'))
    if not os.path.isdir(path):
        return False, f"'{path}' isn't a folder I can find."
    folders = _load_json("vault_folders.json", [])
    if path not in folders:
        folders.append(path)
        json.dump(folders, open(_p("vault_folders.json"), "w"), indent=2)
    return True, path


def _read_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
        except Exception:
            return ""
    if ext in VAULT_TEXT_EXTS:
        try:
            return open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            return ""
    return ""


def vault_build(on_log=None):
    entries, files = [], 0
    for folder in _load_json("vault_folders.json", []):
        for root, _, names in os.walk(folder):
            for fn in names:
                path = os.path.join(root, fn)
                text = _read_any(path)
                if not text.strip():
                    continue
                files += 1
                w = text.split()
                for i in range(0, len(w), 120):
                    chunk = " ".join(w[i:i + 120])
                    entries.append({"path": path, "text": chunk, "words": _vwords(chunk)})
    json.dump(entries, open(_p("vault_index.json"), "w", encoding="utf-8"))
    if on_log:
        on_log(f"VAULT: indexed {files} file(s), {len(entries)} chunk(s)")
    return files, len(entries)


def vault_search(query, top_k=3):
    """None if no index yet, else best-first [{path, text, score}]."""
    entries = _load_json("vault_index.json", None)
    if entries is None:
        return None
    q = set(_vwords(query))
    ql = query.lower().strip()
    scored = []
    for e in entries:
        ov = q & set(e["words"])
        if ov:
            scored.append((len(ov) + (2 if ql in e["text"].lower() else 0), e))
    scored.sort(key=lambda p: p[0], reverse=True)
    return [{"path": e["path"], "text": e["text"], "score": s} for s, e in scored[:top_k]]
