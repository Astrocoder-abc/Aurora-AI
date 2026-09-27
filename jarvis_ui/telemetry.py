"""
Arduino/IoT telemetry bridge for Aurora, plus Game Companion Mode.

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
"""

import json
import os
import threading
import time
import urllib.request

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
