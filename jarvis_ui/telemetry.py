"""
Arduino/IoT telemetry bridge for Aurora, plus Game Companion Mode and the
Experiment Recorder.

Two transports for Arduino/IoT, chosen by iot_config.json (next to
api_key.txt):

  Serial (Arduino/ESP32 over USB):
    {"mode": "serial", "port": "COM5", "baud": 9600}
    The device should Serial.println() one JSON object per line, e.g.:
        Serial.println("{\"temp\":21.5,\"humidity\":40}");

  WiFi (ESP32 running its own tiny web server):
    {"mode": "wifi", "url": "http://192.168.1.50/telemetry", "poll_seconds": 2}
    A GET to `url` should return a JSON object body with the current
    sensor readings.

Either way, Aurora doesn't need to know field names ahead of time — it
just displays whatever numeric/text fields the device sends, under
whatever label you ask for ("Aurora, show my Mars station telemetry").

Requires pyserial for the serial transport only:
    pip install pyserial
The wifi transport uses only the standard library.

GAME COMPANION MODE (GameSessionReader, below): reuses this same
background-reader + Hologram telemetry-panel pattern, but reports
system-level stats instead of Arduino data — FPS/CPU/RAM load, CPU
temp, session timer, foreground app, and recording status. Deliberately
does NOT read anything out of the game process itself (no hooks/memory
reads), so it stays "non-cheating" — informational only.

EXPERIMENT RECORDER (ExperimentRecorder, below): timestamped log of
observations, sensor readings, and screenshots for a school-science
style experiment, plus a compiled report.txt.
  "Aurora, start experiment" / "start experiment called <name>"
  "Aurora, log observation: <text>"
  "Aurora, take an experiment screenshot"
  "Aurora, generate my experiment report"
  "Aurora, stop experiment"
Files land in experiments/<name>/ (log.jsonl, screenshots/, report.txt).
"""

import json
import os
import re
import threading
import time
import urllib.request

import cv2

from jarvis_ui import system_control

try:
    import serial
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None
    PSUTIL_AVAILABLE = False

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "iot_config.json")
EXPERIMENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "experiments")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


class TelemetryReader:
    """Background reader that keeps `self.latest` updated with the most
    recent telemetry dict. Safe to read from another thread (the render
    loop) — `latest` is only ever replaced wholesale, never mutated
    in-place, so a torn read isn't possible."""

    def __init__(self, on_log=None):
        self._on_log = on_log or (lambda msg: None)
        self.latest = {}
        self.last_update = 0.0
        self.connected = False
        self.error = None
        self._running = False
        self._thread = None
        self.mode = None
        self.source_label = ""

    def start(self):
        """Loads iot_config.json and starts the background reader if not
        already running. Returns (ok, message)."""
        if self._running:
            return True, "Telemetry link already running."

        config = load_config()
        if not config:
            return False, ("No iot_config.json found next to api_key.txt. Create one like "
                           '{"mode": "serial", "port": "COM5", "baud": 9600} '
                           'or {"mode": "wifi", "url": "http://<esp32-ip>/telemetry"}.')

        mode = config.get("mode")
        if mode == "serial":
            if not PYSERIAL_AVAILABLE:
                return False, "pyserial isn't installed — run: pip install pyserial"
            port = config.get("port")
            baud = config.get("baud", 9600)
            if not port:
                return False, "iot_config.json is missing 'port' for serial mode."
            self.mode, self.source_label = "serial", port
            self._running = True
            self._thread = threading.Thread(target=self._serial_loop, args=(port, baud), daemon=True)
            self._thread.start()
            return True, f"Connecting to the device on {port}."

        if mode == "wifi":
            url = config.get("url")
            if not url:
                return False, "iot_config.json is missing 'url' for wifi mode."
            poll_seconds = config.get("poll_seconds", 2)
            self.mode, self.source_label = "wifi", url
            self._running = True
            self._thread = threading.Thread(target=self._wifi_loop, args=(url, poll_seconds), daemon=True)
            self._thread.start()
            return True, f"Connecting to {url} over wifi."

        return False, "iot_config.json 'mode' must be 'serial' or 'wifi'."

    def stop(self):
        self._running = False

    def _serial_loop(self, port, baud):
        ser = None
        while self._running:
            if ser is None:
                try:
                    ser = serial.Serial(port, baud, timeout=2)
                    self.connected = True
                    self.error = None
                    self._on_log(f"IOT: connected to {port} at {baud} baud")
                except Exception as e:
                    self.connected = False
                    self.error = str(e)
                    time.sleep(3)
                    continue
            try:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                data = json.loads(line)
                if isinstance(data, dict):
                    self.latest = data
                    self.last_update = time.time()
                    self.connected = True
            except json.JSONDecodeError:
                continue  # partial/garbled line from the device — wait for the next one
            except Exception as e:
                self.connected = False
                self.error = str(e)
                self._on_log(f"IOT: serial error ({e}), reconnecting...")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                time.sleep(3)

    def _wifi_loop(self, url, poll_seconds):
        while self._running:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "aurora-iot/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                if isinstance(data, dict):
                    self.latest = data
                    self.last_update = time.time()
                    self.connected = True
                    self.error = None
            except Exception as e:
                self.connected = False
                self.error = str(e)
                self._on_log(f"IOT: wifi poll failed ({e})")
            time.sleep(max(0.5, poll_seconds))

    def is_stale(self, max_age=10):
        return self.last_update == 0 or (time.time() - self.last_update) > max_age


class GameSessionReader:
    """Game Companion Mode — non-cheating info overlay. Reports:
      fps        (approximated from this process's own render loop via
                  hologram, fed in externally — see main.py; falls back
                  to 'N/A' if never set)
      cpu / ram  system load percentages (psutil)
      temp_c     CPU temperature if the sensor is exposed, else 'N/A'
      session    elapsed time since 'start()' was called
      foreground name of the currently focused window's process
      recording  'yes'/'no', from system_control's screen recorder

    Nothing here reads from the game process itself — it's the same
    background-reader / `latest` dict pattern as TelemetryReader, just
    displayed through Hologram's existing telemetry panel."""

    def __init__(self, on_log=None):
        self._on_log = on_log or (lambda msg: None)
        self.latest = {}
        self.connected = False
        self.last_update = 0.0
        self._running = False
        self._thread = None
        self.session_start = None
        self.external_fps = None  # set by main.py each frame, if available

    def start(self):
        if self._running:
            return True, "Game companion already running."
        self.session_start = time.time()
        self._running = True
        self.connected = True
        if PSUTIL_AVAILABLE:
            psutil.cpu_percent(interval=None)  # prime it, first call is always 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._on_log("GAME: companion mode started")
        return True, "Game companion started."

    def stop(self):
        self._running = False
        self.connected = False
        self.session_start = None
        self._on_log("GAME: companion mode stopped")

    def set_fps(self, fps):
        """Optional: main.py's render loop can feed Aurora's own FPS in
        here each frame for display alongside system stats."""
        self.external_fps = fps

    def _loop(self):
        while self._running:
            mins, secs = divmod(int(time.time() - self.session_start), 60)
            self.latest = {
                "session": f"{mins}m {secs}s",
                "fps": round(self.external_fps) if self.external_fps else "N/A",
                "cpu": round(psutil.cpu_percent(interval=None), 1) if PSUTIL_AVAILABLE else "N/A",
                "ram": round(psutil.virtual_memory().percent, 1) if PSUTIL_AVAILABLE else "N/A",
                "temp_c": system_control.get_cpu_temp() or "N/A",
                "foreground": system_control.get_foreground_process_name() or "unknown",
                "recording": "yes" if system_control.is_recording() else "no",
            }
            self.last_update = time.time()
            time.sleep(1.0)

    def is_stale(self, max_age=5):
        return self.last_update == 0 or (time.time() - self.last_update) > max_age


class ExperimentRecorder:
    """Timestamped log of observations, sensor data, and screenshots for
    a school-science-project style experiment, plus a compiled report.
    'Aurora, start experiment' / 'log observation: ...' /
    'take an experiment screenshot' / 'generate my experiment report'."""

    def __init__(self, on_log=None):
        self._on_log = on_log or (lambda m: None)
        self.active = False
        self.name = None
        self.dir = None
        self.log_path = None
        self._shot_count = 0
        self._last_sensor_log = 0.0

    def start(self, name=None):
        if self.active:
            return False, f"Experiment '{self.name}' is already running."
        self.name = (re.sub(r"[^a-zA-Z0-9_\- ]", "", name).strip().replace(" ", "_")
                     if name else time.strftime("experiment_%Y%m%d_%H%M%S"))
        self.dir = os.path.join(EXPERIMENTS_DIR, self.name)
        os.makedirs(os.path.join(self.dir, "screenshots"), exist_ok=True)
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self._shot_count = 0
        self._last_sensor_log = 0.0
        self.active = True
        self._write({"type": "start", "time": self._ts()})
        self._on_log(f"EXPERIMENT: started '{self.name}'")
        return True, self.name

    def stop(self):
        if not self.active:
            return False, "No experiment is running."
        self._write({"type": "stop", "time": self._ts()})
        self.active = False
        self._on_log(f"EXPERIMENT: stopped '{self.name}'")
        return True, self.name

    def log_observation(self, text):
        if not self.active or not text:
            return False
        self._write({"type": "observation", "time": self._ts(), "text": text})
        self._on_log("EXPERIMENT: observation logged")
        return True

    def log_sensor(self, data):
        if not self.active or not data:
            return False
        self._write({"type": "sensor", "time": self._ts(), "data": data})
        return True

    def maybe_log_sensor(self, data, interval=5):
        """Throttled auto-log — call every frame; only writes every
        `interval` seconds while an experiment is active."""
        if not self.active or not data:
            return
        now = time.time()
        if now - self._last_sensor_log < interval:
            return
        self._last_sensor_log = now
        self.log_sensor(data)

    def capture_screenshot(self, frame):
        if not self.active:
            return False, "No experiment is running."
        if frame is None:
            return False, "No camera frame available."
        self._shot_count += 1
        fname = f"shot_{self._shot_count:03d}.png"
        cv2.imwrite(os.path.join(self.dir, "screenshots", fname), frame)
        self._write({"type": "screenshot", "time": self._ts(), "file": fname})
        self._on_log(f"EXPERIMENT: screenshot {fname}")
        return True, fname

    def generate_report(self):
        if not self.name or not self.log_path or not os.path.exists(self.log_path):
            return None, "No experiment data to report on yet."
        entries = self._read_all()
        obs = [e for e in entries if e["type"] == "observation"]
        sensors = [e for e in entries if e["type"] == "sensor"]
        shots = [e for e in entries if e["type"] == "screenshot"]
        starts = [e for e in entries if e["type"] == "start"]
        stops = [e for e in entries if e["type"] == "stop"]

        lines = [f"EXPERIMENT REPORT: {self.name}", "=" * 40, ""]
        if starts:
            lines.append(f"Started: {starts[0]['time']}")
        if stops:
            lines.append(f"Stopped: {stops[-1]['time']}")
        lines.append(f"Observations: {len(obs)}  Sensor readings: {len(sensors)}  Screenshots: {len(shots)}")
        lines.append("")
        lines.append("-- OBSERVATIONS --")
        lines += [f"[{e['time']}] {e['text']}" for e in obs] or ["(none)"]
        lines.append("")
        lines.append("-- SENSOR DATA --")
        lines += [f"[{e['time']}] {e['data']}" for e in sensors] or ["(none)"]
        lines.append("")
        lines.append("-- SCREENSHOTS --")
        lines += [f"[{e['time']}] screenshots/{e['file']}" for e in shots] or ["(none)"]

        report_path = os.path.join(self.dir, "report.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self._on_log(f"EXPERIMENT: report generated -> {report_path}")
        return report_path, None

    def _ts(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _write(self, entry):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _read_all(self):
        entries = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        return entries
