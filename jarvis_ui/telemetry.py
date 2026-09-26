"""
Arduino/IoT telemetry bridge for Aurora.

Two transports, chosen by iot_config.json (next to api_key.txt):

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
"""

import json
import os
import threading
import time
import urllib.request

try:
    import serial
    PYSERIAL_AVAILABLE = True
except ImportError:
    PYSERIAL_AVAILABLE = False

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
