"""
Aurora first-run setup. One command from a fresh download to a running assistant:

    python setup_wizard.py            create venv, install packages, save + verify Groq key, pick microphone
    python setup_wizard.py --check    only report what is missing (no changes)
    python setup_wizard.py --run      do the setup, then start Aurora

Safe to re-run: finished steps are skipped. Uses only the standard library.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, "venv")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")
KEY_FILE = os.path.join(ROOT, "api_key.txt")
MIC_FILE = os.path.join(ROOT, "mic_index.txt")
WINDOWS_ONLY = ("pycaw", "comtypes")
OPTIONAL_TOOLS = (("adb", "phone control"), ("ffmpeg", "screen recording"), ("git", "project timeline"),
                  ("tesseract", "offline math reading"))

CHECK_ONLY = "--check" in sys.argv


def say(tag, msg):
    print(f"[{tag:^4}] {msg}", flush=True)


def venv_python():
    exe = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")
    return exe if os.path.exists(exe) else None


def run(cmd, quiet=True):
    r = subprocess.run(cmd, capture_output=quiet, text=True)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


# ---- steps ------------------------------------------------------------------
def step_python():
    ok = sys.version_info >= (3, 10)
    say("OK" if ok else "FAIL", f"Python {sys.version.split()[0]}" + ("" if ok else " (3.10 or newer needed)"))
    return ok


def step_venv():
    if venv_python():
        say("OK", "virtual environment exists")
        return True
    if CHECK_ONLY:
        say("TODO", "virtual environment missing")
        return False
    say("....", "creating virtual environment")
    ok, out = run([sys.executable, "-m", "venv", VENV])
    say("OK" if ok else "FAIL", "virtual environment created" if ok else out[-200:])
    return ok


def missing_packages(py):
    names = {"opencv-contrib-python": "cv2", "mediapipe": "mediapipe", "PyOpenGL": "OpenGL", "pygame": "pygame",
             "numpy": "numpy", "groq": "groq", "SpeechRecognition": "speech_recognition", "pyaudio": "pyaudio",
             "edge-tts": "edge_tts", "psutil": "psutil", "Pillow": "PIL", "pyserial": "serial", "pypdf": "pypdf",
             "python-pptx": "pptx", "python-docx": "docx", "openpyxl": "openpyxl", "faster-whisper": "faster_whisper"}
    code = ("import importlib.util as u,json;print(json.dumps([k for k,m in %r.items() if u.find_spec(m) is None]))" % names)
    ok, out = run([py, "-c", code])
    try:
        return json.loads(out.strip().splitlines()[-1]) if ok else list(names)
    except (ValueError, IndexError):
        return list(names)


def step_packages():
    py = venv_python()
    if not py:
        return False
    missing = missing_packages(py)
    if not missing:
        say("OK", "all packages installed")
        return True
    if CHECK_ONLY:
        say("TODO", "missing packages: " + ", ".join(missing))
        return False
    say("....", f"installing {len(missing)} missing package(s), this can take several minutes")
    lines = [l.strip() for l in open(REQUIREMENTS, encoding="utf-8") if l.strip() and not l.startswith("#")]
    keep = [l for l in lines if not l.lower().startswith("pyaudio")
            and (os.name == "nt" or not l.lower().startswith(WINDOWS_ONLY))]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(keep))
    ok, out = run([py, "-m", "pip", "install", "-r", f.name])
    os.unlink(f.name)
    if not ok:
        say("FAIL", "pip failed:\n" + out[-400:])
        return False
    if "pyaudio" in missing:
        ok, out = run([py, "-m", "pip", "install", "pyaudio"])
        if not ok and os.name == "nt":
            say("....", "pyaudio wheel failed, trying pipwin")
            run([py, "-m", "pip", "install", "pipwin"])
            ok, out = run([py, "-m", "pipwin", "install", "pyaudio"])
        say("OK" if ok else "WARN", "pyaudio installed" if ok else "pyaudio failed: voice input will not work (see README)")
    say("OK", "packages installed")
    return True


def groq_key_status(key):
    req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "aurora-setup/1.0"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
        return "valid"
    except urllib.error.HTTPError as e:
        return "invalid" if e.code in (401, 403) else f"error {e.code}"
    except (urllib.error.URLError, OSError):
        return "offline"


def step_api_key():
    key = ""
    try:
        key = open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        key = os.environ.get("GROQ_API_KEY", "")
    if key:
        status = groq_key_status(key)
        say("OK" if status in ("valid", "offline") else "WARN", f"Groq key found ({status})")
        if status != "invalid" or CHECK_ONLY:
            return True
    elif CHECK_ONLY:
        say("TODO", "no Groq key (api_key.txt). Local commands still work without it")
        return False
    print("Get a free key at https://console.groq.com/keys (press Enter to skip; local commands still work).")
    while True:
        key = input("Paste Groq API key: ").strip()
        if not key:
            say("SKIP", "no key saved")
            return False
        status = groq_key_status(key)
        if status in ("valid", "offline"):
            with open(KEY_FILE, "w", encoding="utf-8") as f:
                f.write(key)
            say("OK", "key saved" + (" (could not verify offline)" if status == "offline" else " and verified"))
            return True
        say("FAIL", f"key rejected ({status}), try again or press Enter to skip")


def step_microphone():
    py = venv_python()
    if not py or os.path.exists(MIC_FILE):
        if os.path.exists(MIC_FILE):
            say("OK", "microphone choice saved (mic_index.txt)")
        return True
    ok, out = run([py, "-c", "import speech_recognition as s,json;print(json.dumps(s.Microphone.list_microphone_names()))"])
    try:
        names = json.loads(out.strip().splitlines()[-1]) if ok else []
    except (ValueError, IndexError):
        names = []
    if not names:
        say("WARN", "no microphone found (voice input needs pyaudio and an input device)")
        return False
    if len(names) == 1 or CHECK_ONLY:
        say("OK", f"microphone: {names[0]}" + ("" if len(names) == 1 else " (system default; run setup without --check to choose)"))
        return True
    for i, n in enumerate(names):
        print(f"  [{i}] {n}")
    pick = input("Microphone number (Enter = system default): ").strip()
    if pick.isdigit() and int(pick) < len(names):
        with open(MIC_FILE, "w") as f:
            f.write(pick)
        say("OK", f"using microphone [{pick}]")
    else:
        say("OK", "using the system default microphone")
    return True


def step_optional_tools():
    for tool, purpose in OPTIONAL_TOOLS:
        found = shutil.which(tool)
        say("OK" if found else "skip", f"{tool}: {'found' if found else 'not installed, only needed for ' + purpose}")


def main():
    print("\nAURORA SETUP" + (" (check only)" if CHECK_ONLY else "") + "\n" + "=" * 40)
    results = [step_python()]
    if results[0]:
        results.append(step_venv())
        if results[-1]:
            results += [step_packages(), step_api_key(), step_microphone()]
    step_optional_tools()
    ok = all(results)
    print("=" * 40)
    print("Ready. Start with: python main.py" if ok else "Some steps need attention (see above). Re-run this script after fixing them.")
    if ok and "--run" in sys.argv and not CHECK_ONLY:
        sys.exit(subprocess.call([venv_python(), os.path.join(ROOT, "main.py")], cwd=ROOT))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
