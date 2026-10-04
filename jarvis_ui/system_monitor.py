"""
Emergency System Monitor: background watcher that detects sudden disk-space loss, low disk,
overheating, network disconnects, application crashes and unusual CPU usage, then explains
what most likely happened (speech + red flash + event log + info card).

  "Aurora, emergency monitor on / off / status"
  "Aurora, what happened"  /  "explain the last alert"  /  "system alerts"

Alerts stay silent (log + flash only) while Presentation Mode is active (hologram.quiet_alerts).
Crash detection reads the Windows Application event log (wevtutil). Temperature uses the psutil
sensor, falling back to Windows ACPI thermal zones (may need admin; silently disabled otherwise).
"""
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque

from jarvis_ui import system_control as sc

try:
    import psutil
except ImportError:
    psutil = None

GB = 1024 ** 3
CPU_HIGH, CPU_WINDOW = 85.0, 6              # 6 samples x 5 s = 30 s sustained
TEMP_LIMIT_C = 85.0
DISK_DROP_BYTES, DISK_LOW_BYTES = 2 * GB, 5 * GB
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

CRASH_CODES = {
    "0xc0000005": "an access violation (the program touched invalid memory)",
    "0xc0000409": "a stack buffer overrun",
    "0xc00000fd": "a stack overflow",
    "0xc000001d": "an illegal CPU instruction",
    "0xe0434352": "an unhandled .NET exception",
    "0x40000015": "a fatal runtime error (abort)",
}
PROCESS_HINTS = {
    "msmpeng.exe": "Windows Defender is scanning files",
    "tiworker.exe": "Windows Update is installing",
    "searchindexer.exe": "Windows is indexing files",
    "compattelrunner.exe": "Windows is running a compatibility check",
    "svchost.exe": "a Windows service, often Update",
    "chrome.exe": "browser tabs or video",
    "python.exe": "a Python program, possibly Aurora itself",
}


def _fmt_bytes(n):
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / 1024 ** 2:.0f} MB"


def _drive_root():
    return (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"


class SystemMonitor:
    def __init__(self, hologram, voice=None):
        self.h, self.voice = hologram, voice
        self.running = False
        self.alerts = []                    # [{t, kind, summary, explanation}]
        self.temp_c = None
        self._last, self._due, self._errors = {}, {}, set()
        self._free = deque(maxlen=24)       # (time, free bytes), ~2 min
        self._io = deque(maxlen=14)         # (time, {pid: (name, write_bytes)})
        self._cpu = deque(maxlen=120)
        self._top = ("unknown", 0.0)
        self._net_fails, self._net_down_since = 0, None
        self._crash_seen, self._crash_primed = None, False
        self._temp_dead = False
        getattr(type(hologram), "_LOG_CATEGORY_COLORS", {}).setdefault("MONITOR", (1.0, 0.3, 0.3))

    # ---- lifecycle -----------------------------------------------------------
    def start(self):
        if psutil is None:
            return False, "The system monitor needs psutil. Run: pip install psutil"
        if self.running:
            return True, "Emergency monitor is already on."
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        self.h.log_event("MONITOR: emergency monitor on")
        return True, "Emergency monitor on. Watching disk, CPU, temperature, network and app crashes."

    def stop(self):
        self.running = False

    def _loop(self):
        psutil.cpu_percent(interval=None)
        checks = ((5, self._check_disk), (5, self._check_cpu), (30, self._check_temp),
                  (10, self._check_network), (20, self._check_crashes))
        while self.running:
            now = time.time()
            for every, fn in checks:
                if now >= self._due.get(fn.__name__, 0):
                    self._due[fn.__name__] = now + every
                    try:
                        fn(now)
                    except Exception as e:
                        if fn.__name__ not in self._errors:
                            self._errors.add(fn.__name__)
                            self.h.log_event(f"MONITOR: {fn.__name__} failed ({type(e).__name__})")
            time.sleep(1.0)

    # ---- alerting ------------------------------------------------------------
    def _quiet(self):
        return bool(getattr(self.h, "quiet_alerts", False))

    def _say(self, text):
        if self.voice is not None and not self._quiet():
            self.voice.speak_now(text)

    def _alert(self, kind, summary, explanation, cooldown):
        now = time.time()
        if now - self._last.get(kind, 0) < cooldown:
            return
        self._last[kind] = now
        self.alerts = (self.alerts + [{"t": now, "kind": kind, "summary": summary, "explanation": explanation}])[-20:]
        self.h.log_event(f"MONITOR: {summary}")
        self.h.trigger_flash("thumbs_down", 0.8)
        if not self._quiet():
            if getattr(self.h, "mode", "") in ("empty", "network"):
                self.h.show_info_card("System alert: " + summary, explanation)
            self._say(f"Heads up. {summary}. {explanation}")

    def _notice(self, text):
        self.h.log_event(f"MONITOR: {text}")
        self._say(text)

    # ---- disk ----------------------------------------------------------------
    def _snapshot_io(self, now):
        snap = {}
        for p in psutil.process_iter(["name"]):
            try:
                snap[p.pid] = (p.info.get("name") or "?", p.io_counters().write_bytes)
            except (psutil.Error, AttributeError, OSError):
                continue
        self._io.append((now, snap))

    def _writer_hint(self, now):
        generic = "Likely a large download, install, update or temp file."
        old = next((s for t, s in self._io if now - t <= 90), None)
        if not old or not self._io:
            return generic
        best = max(((w - old.get(pid, (n, 0))[1], n) for pid, (n, w) in self._io[-1][1].items()), default=(0, "?"))
        if best[0] < 100 * 1024 ** 2:
            return generic
        return f"{best[1]} wrote about {_fmt_bytes(best[0])} in that time, so it is the likely cause."

    def _check_disk(self, now):
        root = _drive_root()
        drive = root.rstrip("\\/") or "/"
        u = psutil.disk_usage(root)
        self._free.append((now, u.free))
        self._snapshot_io(now)
        recent = [f for t, f in self._free if now - t <= 90]
        drop = (max(recent) - u.free) if recent else 0
        if drop >= max(DISK_DROP_BYTES, 0.03 * u.total):
            self._alert("disk_drop", f"Disk space on {drive} dropped by {_fmt_bytes(drop)} in about a minute",
                        f"Only {_fmt_bytes(u.free)} is free now. {self._writer_hint(now)}", 300)
        elif u.free < DISK_LOW_BYTES or u.percent > 95:
            self._alert("disk_low", f"Disk {drive} is almost full with {_fmt_bytes(u.free)} free",
                        "Windows slows down and updates fail when the system drive is this full. "
                        "Clear downloads, temp files or the recycle bin.", 1800)

    # ---- cpu / temperature -----------------------------------------------------
    def _top_cpu(self):
        cores, best = psutil.cpu_count() or 1, ("unknown", 0.0)
        for p in psutil.process_iter(["name", "cpu_percent"]):
            name = p.info.get("name") or "?"
            if p.pid == 0 or name.lower() == "system idle process":
                continue
            share = (p.info.get("cpu_percent") or 0.0) / cores
            if share > best[1]:
                best = (name, share)
        return best

    def _check_cpu(self, now):
        self._cpu.append(psutil.cpu_percent(interval=None))
        self._top = self._top_cpu()                     # called every cycle so per-process readings stay primed
        if len(self._cpu) < CPU_WINDOW:
            return
        samples = list(self._cpu)
        avg = sum(samples[-CPU_WINDOW:]) / CPU_WINDOW
        older = samples[:-CPU_WINDOW]
        base = sum(older) / len(older) if len(older) >= 24 else None
        if avg >= CPU_HIGH or (base is not None and avg >= 60 and avg >= base + 40):
            name, share = self._top
            hint = PROCESS_HINTS.get(name.lower())
            self._alert("cpu", f"CPU is running at {avg:.0f} percent",
                        f"It has averaged {avg:.0f}% for 30 seconds" + (f", normally about {base:.0f}%" if base is not None else "")
                        + f". The top process is {name} at about {share:.0f}% of total" + (f", probably because {hint}." if hint else "."),
                        180)

    def _read_temp(self):
        t = sc.get_cpu_temp()
        if t is not None:
            return t
        if os.name != "nt" or self._temp_dead:
            return None
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature"
                                " | Select-Object -First 1).CurrentTemperature"],
                               capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
            temp = float(r.stdout.strip()) / 10 - 273.15
            if 0 < temp < 130:
                return temp
        except Exception:
            pass
        self._temp_dead = True
        return None

    def _check_temp(self, now):
        self.temp_c = self._read_temp()
        if self.temp_c is not None and self.temp_c >= TEMP_LIMIT_C:
            load = self._cpu[-1] if self._cpu else 0
            self._alert("overheat", f"CPU temperature is {self.temp_c:.0f} degrees Celsius",
                        f"CPU load is {load:.0f}% and the top process is {self._top[0]}. Throttling is likely: "
                        "check the vents and fans and close heavy programs.", 300)

    # ---- network ---------------------------------------------------------------
    @staticmethod
    def _online():
        for host in ("1.1.1.1", "8.8.8.8"):
            try:
                socket.create_connection((host, 53), 1.5).close()
                return True
            except OSError:
                continue
        return False

    @staticmethod
    def _net_explain():
        ups = [n for n, s in psutil.net_if_stats().items() if s.isup and not n.lower().startswith(("lo", "loopback"))]
        if not ups:
            return "No network adapter is connected: wifi is off, the cable is unplugged or airplane mode is on."
        return f"Adapter {ups[0]} is up but the internet is unreachable, so the router, your ISP or DNS is the likely problem."

    def _check_network(self, now):
        if self._online():
            if self._net_down_since is not None:
                secs = int(now - self._net_down_since)
                self._net_down_since = None
                self._notice(f"Network is back after {secs} seconds")
            self._net_fails = 0
            return
        self._net_fails += 1
        if self._net_fails == 2:
            self._net_down_since = now - 10
            self._alert("network", "Network disconnected", self._net_explain(), 60)

    # ---- crashes (Windows Application event log) -----------------------------------
    def _check_crashes(self, now):
        if os.name != "nt":
            return
        r = subprocess.run(["wevtutil", "qe", "Application", "/q:*[System[(EventID=1000)]]", "/c:1", "/rd:true", "/f:text"],
                           capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW)
        first, self._crash_primed = not self._crash_primed, True
        app = re.search(r"Faulting application name:\s*([^,\r\n]+)", r.stdout)
        stamp = re.search(r"Date:\s*(\S+)", r.stdout)
        if not app or not stamp:
            return
        if first:
            self._crash_seen = stamp.group(1)            # baseline: ignore crashes from before Aurora started
            return
        if stamp.group(1) == self._crash_seen:
            return
        self._crash_seen = stamp.group(1)
        code = re.search(r"Exception code:\s*(\S+)", r.stdout)
        module = re.search(r"Faulting module name:\s*([^,\r\n]+)", r.stdout)
        reason = CRASH_CODES.get(code.group(1).lower(), "an unhandled error") if code else "an unhandled error"
        self._alert("crash", f"{app.group(1).strip()} crashed",
                    f"It failed with {reason}" + (f" inside {module.group(1).strip()}" if module else "")
                    + ". Restarting usually works; if it repeats, update or reinstall it, or check the related driver.", 20)

    # ---- status ------------------------------------------------------------------
    def status(self):
        u = psutil.disk_usage(_drive_root())
        temp = f", CPU temperature {self.temp_c:.0f} degrees" if self.temp_c is not None else ""
        return (f"Monitor is {'on' if self.running else 'off'}. CPU at {psutil.cpu_percent(interval=None):.0f} percent, "
                f"{_fmt_bytes(u.free)} free on disk{temp}, network {'online' if self._online() else 'offline'}. "
                f"{len(self.alerts)} alert{'s' if len(self.alerts) != 1 else ''} so far.")


_monitor = None


def start(hologram, voice=None):
    """Create (once) and start the monitor. Returns (ok, message)."""
    global _monitor
    if _monitor is None:
        _monitor = SystemMonitor(hologram, voice)
    return _monitor.start()


def handle_command(voice, text):
    """Voice entry point for voice_assistant.py. Returns True if handled."""
    t = text.lower().strip(" .?!")
    mon, say = _monitor, voice._speak
    if re.search(r"\b(?:emergency|system) monitor\b", t):
        if re.search(r"\b(off|stop|disable|pause)\b", t):
            if mon:
                mon.stop()
            say("Emergency monitor off.")
        elif re.search(r"\b(on|start|enable|resume)\b", t):
            say(start(voice.hologram, voice)[1])
        else:
            say(mon.status() if mon and psutil else "The emergency monitor isn't running. Say emergency monitor on.")
        return True
    explicit = re.search(r"\b(?:explain|what was) (?:the |that )?(?:last )?(?:alert|alarm|warning|problem)\b"
                         r"|\b(?:system|any) (?:alerts|alarms|warnings)\b|\balert history\b", t)
    if explicit or (t in ("what happened", "what just happened") and mon and mon.alerts):
        if not mon or not mon.alerts:
            say("No alerts so far. I'm watching disk space, CPU, temperature, network and app crashes.")
            return True
        voice.hologram.show_info_card("System alerts", " | ".join(
            f"{time.strftime('%H:%M', time.localtime(a['t']))} {a['summary']}" for a in mon.alerts[-4:]))
        say(f"{mon.alerts[-1]['summary']}. {mon.alerts[-1]['explanation']}")
        return True
    return False
