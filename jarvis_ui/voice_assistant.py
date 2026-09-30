"""
Aurora voice pipeline: wake word -> speech-to-text -> instant local commands, otherwise the
Groq gpt-oss-120b brain (brain.py, with tools) -> pipelined neural speech.

SpeechEngine runs two threads (synthesise ahead / play); a turn waits for ALL speech to finish, and the
stop listener only reacts to a bare "stop" phrase. Optional modules (sandbox_labs, addons, aurora_plus)
plug in through self.plus / _handle_local_command.
"""
import asyncio
import ast
import difflib
import importlib
import json
import operator
import os
import queue
import random
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

import speech_recognition as sr
import pygame

from jarvis_ui.hologram import (ELEMENTS, SHAPES, SHAPE_ALIASES, US_STATE_POSITIONS, STAR_SYSTEMS,
                                MOLECULES, MOLECULE_ALIASES, CONSTELLATIONS, CONSTELLATION_ALIASES)
from jarvis_ui import system_control as sc
from jarvis_ui import brain, telemetry

try:
    from groq import Groq
except ImportError:
    Groq = None
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False


def _optional(name):
    try:
        return importlib.import_module(f"jarvis_ui.{name}")
    except Exception as e:
        print(f"optional module {name} unavailable: {e}", flush=True)
        return None


sandbox_labs, addons = (_optional(n) for n in ("sandbox_labs", "addons"))

WAKE_WORD_CORE, WAKE_FUZZY = "aurora", 0.72
VISION_RE = re.compile(r"\bwhat (?:can |do )?you see\b|\bwhat am i (?:holding|looking at|wearing)\b|\blook at (?:this|me)\b"
                       r"|\bwhat(?:'s| is) in front of me\b|\bdescribe (?:the |my )?(?:camera|scene|room|view)\b"
                       r"|\b(?:use|check|open) (?:the |my )?(?:webcam|camera view)\b")
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api_key.txt")
EDGE_VOICE = "en-GB-RyanNeural"
STOP_WORDS = ("stop", "stop it", "be quiet", "silence", "shut up", "enough")
STOP_RE = re.compile(r"(?:\w+[, ]+)?(?:please )?(?:stop|be quiet|silence|shut up|enough|cancel)(?: it| talking| that| please)?")

NUMBER_WORDS = {"one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3, "four": 4, "4": 4, "five": 5, "5": 5,
                "six": 6, "6": 6, "seven": 7, "7": 7, "eight": 8, "8": 8, "nine": 9, "9": 9, "ten": 10, "10": 10}
LOCAL_SITES = {"google": "https://google.com", "youtube": "https://youtube.com",
               "github": "https://github.com", "gmail": "https://mail.google.com"}


def find_wake_word(text):
    """(True, text_after_wake_word) using fuzzy matching ('arora' etc.)."""
    words = text.split()
    for i, w in enumerate(words):
        c = w.strip(",.!?").lower()
        if c == WAKE_WORD_CORE or difflib.SequenceMatcher(None, c, WAKE_WORD_CORE).ratio() >= WAKE_FUZZY:
            after, before = " ".join(words[i + 1:]), " ".join(words[:i])
            return True, (after or before)
    return False, None


def clean_for_speech(text):
    for pat, rep in ((r"```.*?```", ""), (r"`([^`]+)`", r"\1"), (r"\*\*\*(.+?)\*\*\*", r"\1"), (r"\*\*(.+?)\*\*", r"\1"),
                     (r"\*(.+?)\*", r"\1"), (r"__(.+?)__", r"\1"), (r"\[([^\]]+)\]\([^)]+\)", r"\1")):
        text = re.sub(pat, rep, text, flags=re.S)
    text = re.sub(r"^#{1,6}\s*|^[\*\-\+]\s+|^\d+\.\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", re.sub(r"[*_~`#]", "", text)).strip()


def load_api_key():
    try:
        with open(API_KEY_FILE) as f:
            key = f.read().strip()
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("GROQ_API_KEY")


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg}


def _eval_node(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval_node(n.left), _eval_node(n.right))
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval_node(n.operand))
    raise ValueError("unsupported")


def try_calculate(text):
    t = text.lower()
    m = re.search(r"([\d.]+)\s*percent of\s*([\d.]+)", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return a / 100 * b, f"{a}% of {b}"
    e = t
    for pat, rep in ((r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\btimes\b|\bmultiplied by\b", "*"), (r"\bdivided by\b|\bover\b", "/")):
        e = re.sub(pat, rep, e)
    e = re.sub(r"[^0-9+\-*/(). ]", "", e).strip()
    if not e or not re.search(r"\d", e) or not any(o in e for o in "+-*/"):
        return None
    try:
        return _eval_node(ast.parse(e, mode="eval").body), e
    except Exception:
        return None


class Chunker:
    """Turns streamed text into speakable chunks: first one early, tiny sentences merged."""
    END = re.compile(r"(?<=[.!?])\s+|\n+")
    SOFT = re.compile(r"(?<=[,;:])\s+")

    def __init__(self, min_len=24, first_soft=70):
        self.buf, self.first, self.min_len, self.first_soft = "", True, min_len, first_soft

    def feed(self, delta):
        self.buf += delta
        out = []
        while True:
            cut = next((m for m in self.END.finditer(self.buf) if len(self.buf[:m.start()].strip()) >= self.min_len), None)
            if cut is None and self.first and len(self.buf) > self.first_soft:
                cut = next((m for m in self.SOFT.finditer(self.buf) if len(self.buf[:m.start()].strip()) >= 30), None)
            if cut is None:
                return out
            chunk, self.buf, self.first = self.buf[:cut.start()].strip(), self.buf[cut.end():], False
            if chunk:
                out.append(chunk)

    def flush(self):
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


def _rm(path):
    if path:
        try:
            os.remove(path)
        except Exception:
            pass


async def _edge_save(text, path):
    await edge_tts.Communicate(text, voice=EDGE_VOICE).save(path)


class SpeechEngine:
    """Pipelined TTS: synth thread (prefetch) -> play thread. Never drops queued speech."""

    def __init__(self, on_log):
        self.log = on_log
        self.use_edge = lambda: True          # aurora_plus swaps in an online check
        self._text_q, self._audio_q = queue.Queue(), queue.Queue(maxsize=4)
        self._gen, self._pending, self._cv = 0, 0, threading.Condition()
        self._offline = None
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    @property
    def busy(self):
        return self._pending > 0

    def say(self, text):
        text = clean_for_speech(text)
        if not text:
            return
        with self._cv:
            self._pending += 1
        self._text_q.put((self._gen, text))

    def _done(self):
        with self._cv:
            self._pending = max(0, self._pending - 1)
            self._cv.notify_all()

    def wait(self, timeout=300):
        with self._cv:
            self._cv.wait_for(lambda: self._pending == 0, timeout)

    def stop(self):
        with self._cv:
            self._gen += 1
        for q in (self._text_q, self._audio_q):
            try:
                while True:
                    item = q.get_nowait()
                    if q is self._audio_q:
                        _rm(item[2])
                    self._done()
            except queue.Empty:
                pass
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

    def _synth_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            gen, text = self._text_q.get()
            try:
                if gen != self._gen:
                    self._done()
                    continue
                path = None
                if EDGE_TTS_AVAILABLE and self.use_edge():
                    for _ in range(2):
                        fd, p = tempfile.mkstemp(suffix=".mp3")
                        os.close(fd)
                        try:
                            loop.run_until_complete(asyncio.wait_for(_edge_save(text, p), 25))
                            path = p
                            break
                        except Exception as e:
                            _rm(p)
                            self.log(f"VOICE: edge-tts failed ({str(e)[:80]})")
                if gen != self._gen:
                    _rm(path)
                    self._done()
                    continue
                self._audio_q.put((gen, text, path))
            except Exception as e:
                self.log(f"VOICE: synth error ({e})")
                self._done()

    def _play_loop(self):
        while True:
            gen, text, path = self._audio_q.get()
            try:
                if gen == self._gen:
                    if path:
                        self._play_file(gen, path)
                    else:
                        self._speak_offline(text)
            except Exception as e:
                self.log(f"VOICE: playback error ({e})")
            finally:
                _rm(path)
                self._done()

    def _play_file(self, gen, path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy() and gen == self._gen:
            time.sleep(0.03)
        if gen != self._gen:
            pygame.mixer.music.stop()
        try:
            pygame.mixer.music.unload()          # releases the file lock on Windows
        except Exception:
            pass

    def _speak_offline(self, text):
        if self._offline is None:
            try:
                import pyttsx3
                eng = pyttsx3.init()
                eng.setProperty("rate", 175)
                for v in eng.getProperty("voices"):
                    if any(k in v.name.lower() for k in ("david", "male", "mark", "guy")):
                        eng.setProperty("voice", v.id)
                        break
                self._offline = eng
            except Exception as e:
                self.log(f"VOICE: no offline TTS ({e}) - reply is text-only")
                self._offline = False
        if self._offline:
            self._offline.say(text)
            self._offline.runAndWait()


class NullVoice:
    """Stand-in when the voice assistant can't be constructed, so main.py keeps running."""
    state, enabled, client, microphone, plus = "idle", False, None, None, None
    pending_enrollment_name, latest_frame, drawer = None, None, None

    def __init__(self):
        self.telemetry = telemetry.TelemetryReader()
        self.game_session = telemetry.GameSessionReader()
        self.experiment = telemetry.ExperimentRecorder()
        self.active_reader = self.telemetry

    def speak_now(self, text): print("AURORA (voice off):", text)
    def start(self): pass
    def stop(self): pass


class VoiceAssistant:
    WMO = {}
    for _codes, _cond, _desc in [((0,), "sunny", "clear sky"), ((1,), "sunny", "mainly clear"), ((2,), "partly_cloudy", "partly cloudy"),
                                 ((3,), "cloudy", "overcast"), ((45, 48), "fog", "fog"), ((51, 53, 55, 56, 57), "rain", "drizzle"),
                                 ((61, 63, 65, 66, 67, 80, 81, 82), "rain", "rain"), ((71, 73, 75, 77, 85, 86), "snow", "snow"),
                                 ((95, 96, 99), "storm", "thunderstorm")]:
        for _c in _codes:
            WMO[_c] = (_cond, _desc)

    def __init__(self, hologram, face_id, on_log=None):
        self.hologram, self.face_id = hologram, face_id
        self._state, self.enabled = "idle", False
        self.last_heard = self.last_reply = ""
        ext = on_log or (lambda t: None)
        logfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "voice_debug.log")

        def log(text):
            print(text, flush=True)
            try:
                with open(logfile, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception:
                pass
            ext(text)

        self._log = self._on_log = log
        log(f"VOICE: === startup {time.strftime('%H:%M:%S')} ===")
        self._running = False
        self.active_timers, self.last_weather_error = [], None
        self.pending_enrollment_name, self.latest_frame, self.drawer = None, None, None
        self.plus = None                          # set by aurora_plus.install()
        self.telemetry = telemetry.TelemetryReader(on_log=log)
        self.game_session = telemetry.GameSessionReader(on_log=log)
        self.experiment = telemetry.ExperimentRecorder(on_log=log)
        self.active_reader = self.telemetry
        self._cancel, self._mic_lock = threading.Event(), threading.Lock()
        self._capture, self._capture_tid, self._turn_display = None, None, False

        self.speech = SpeechEngine(log)
        self.api_key = load_api_key()
        self.client = None
        if Groq is None:
            log("VOICE: 'groq' package not installed - voice AI disabled")
        elif not self.api_key:
            log("VOICE: no API key found (api_key.txt or GROQ_API_KEY) - voice AI disabled")
        else:
            self.client = Groq(api_key=self.api_key)
        self.brain = brain.Brain(self.client, log, self._exec_tool) if self.client else None

        try:
            self.recognizer = sr.Recognizer()
            names = sr.Microphone.list_microphone_names()
            log(f"VOICE: found {len(names)} audio input device(s):")
            for i, n in enumerate(names):
                log(f"  [{i}] {n}")
            idx = self._read_number("mic_index.txt", int)
            self.microphone = sr.Microphone(device_index=idx) if idx is not None else sr.Microphone()
            log(f"VOICE: using mic_index.txt override -> [{idx}]" if idx is not None else
                "VOICE: using system default input (create mic_index.txt if nothing is heard)")
        except Exception as e:
            log(f"VOICE: microphone init failed ({e}) - voice AI disabled")
            self.recognizer = self.microphone = None
        log(f"VOICE: Edge neural voice '{EDGE_VOICE}'" if EDGE_TTS_AVAILABLE else "VOICE: edge-tts missing, offline voice only")

    @property
    def state(self):
        return "speaking" if self.speech.busy else self._state

    @staticmethod
    def _read_number(name, cast):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", name)) as f:
                v = f.read().strip()
            return cast(v) if v else None
        except Exception:
            return None

    def start(self):
        if not self.microphone:                   # an API key is NOT required: local commands work offline
            return
        self.enabled = self._running = True
        threading.Thread(target=self._run_loop, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        self._log("VOICE: listening for wake word 'Aurora'" + ("" if self.client else " (no AI key - local commands only)"))

    def stop(self):
        self._running = False
        self.speech.stop()

    # ---- speech ---------------------------------------------------------------
    def speak_now(self, text):
        """Non-blocking, safe from any thread (e.g. the camera loop greeting someone)."""
        self.last_reply = clean_for_speech(text)
        self._log(f"AURORA: {self.last_reply}")
        self.speech.say(text)

    def _speak(self, text):
        """Blocking speak for command handlers. While a tool call is capturing, records instead of speaking."""
        text = clean_for_speech(text)
        if self._capture is not None and threading.get_ident() == self._capture_tid:
            self._capture.append(text)
            return
        self.last_reply = text
        self._log(f"AURORA: {text}")
        self.speech.say(text)
        self.speech.wait(120)

    def _interrupt(self):
        self._cancel.set()
        self.speech.stop()

    def _watch_loop(self):
        """While Aurora speaks, listen for a bare 'stop'. Serialised with the main loop via _mic_lock."""
        while self._running:
            if not self.speech.busy or not self._mic_lock.acquire(timeout=0.2):
                time.sleep(0.1)
                continue
            text = ""
            try:
                with self.microphone as src:
                    audio = self.recognizer.listen(src, timeout=1.2, phrase_time_limit=2.5)
                text = self.recognizer.recognize_google(audio).lower().strip(" .,!?")
            except Exception:
                pass
            finally:
                self._mic_lock.release()
            if text and STOP_RE.fullmatch(text):
                self._log("VOICE: stop heard, interrupting speech")
                self._interrupt()

    # ---- timers -------------------------------------------------------------------
    def _start_timer(self, seconds, label):
        ends = time.time() + seconds
        self.active_timers.append({"label": label, "ends_at": ends})

        def run():
            time.sleep(seconds)
            self.active_timers[:] = [t for t in self.active_timers if t["ends_at"] != ends]
            self._log(f"TIMER: {label} finished")
            self._speak(f"{label} is up!" if label else "Timer's up!")

        threading.Thread(target=run, daemon=True).start()
        self._log(f"VOICE: timer started - {label} ({seconds}s)")

    # ---- LLM turn ---------------------------------------------------------------------
    def _ask(self, text):
        if self.brain is None:
            self.last_reply = "My AI brain isn't connected. Check your Groq API key."
            self._speak(self.last_reply)
            return self.last_reply
        self._cancel.clear()
        self._turn_display = False
        self._state = "speaking"
        chunker, acc = Chunker(), []

        def on_delta(d):
            if self._cancel.is_set():
                return
            acc.append(d)
            if not self._turn_display:
                self.hologram.show_info_card(text, "".join(acc))
            for s in chunker.feed(d):
                self.speech.say(s)

        try:
            reply = self.brain.ask(text, on_delta, self._cancel)
        except Exception as e:
            self._log(f"VOICE: brain error {type(e).__name__}: {e}")
            reply = f"Sorry, I hit an error talking to the API: {str(e)[:120]}"
            self.speech.say(reply)
        if not self._cancel.is_set():
            for s in chunker.flush():
                self.speech.say(s)
            if not reply:
                reply = "Done." if self._turn_display else "I didn't get a response back."
                self.speech.say(reply)
        self.last_reply = reply
        self._log(f"AURORA: {reply}")
        self.speech.wait(300)            # never return until everything queued has been spoken
        time.sleep(0.3)                  # let the speaker tail die before the mic re-opens
        self._state = "idle"
        return reply

    def run_text(self, text):
        """Same pipeline as a spoken command, for typed input (network hub). Returns the reply text."""
        self.last_reply = ""
        if self._handle_local_command(text):
            return self.last_reply or "Done."
        self._ask(text)
        return self.last_reply

    # ---- tools (LLM -> Aurora). Most reuse the local commands, capturing their reply text ----
    def _run_local(self, phrase):
        self._capture, self._capture_tid = [], threading.get_ident()
        try:
            handled = self._handle_local_command(phrase)
            out = " ".join(self._capture)
        finally:
            self._capture = None
        return out if handled else "That action isn't available."

    def _exec_tool(self, name, a):
        g = lambda k, d="": str(a.get(k, d) or d).strip()
        if name == "get_time":
            return time.strftime("It's %I:%M %p on %A %d %B %Y")
        if name == "read_notes":
            return "; ".join(sc.read_notes(5)) or "no notes yet"
        if name == "search_files":
            hits = sc.vault_search(g("query"))
            if hits is None:
                return "The vault isn't built yet. Say: add folder <path> to my vault, then rebuild my vault."
            return " | ".join(f"{os.path.basename(h['path'])}: {h['text'][:200]}" for h in hits) or "nothing found"
        if name == "describe_camera":
            self._turn_display = True
            return self._run_local("what do you see")
        kind, n, val, tgt = g("kind"), g("name"), g("value"), g("target")
        phrase = None
        if name == "show_display":
            self._turn_display = True
            phrase = {"atom": f"show me a {n} atom", "shape": f"show me a {n}", "molecule": f"show a {n} molecule",
                      "constellation": f"show {n}", "star_system": f"show the {n} system", "solar_system": "show me the solar system",
                      "network": "show my systems", "graph": f"plot {g('expression') or n}", "satellite": f"show {n or 'ISS'} satellite",
                      "physics": f"show a {n or 'pendulum'} simulation", "heart": "show the human heart"}.get(kind)
        elif name == "get_weather":
            self._turn_display = True
            phrase = f"weather in {g('location')}" if g("location") else "what's the weather right now"
        elif name == "set_timer":
            phrase = f"set a timer for {int(a.get('seconds', 60))} seconds" + (f" called {g('label')}" if g("label") else "")
        elif name == "edit_atom":
            self._turn_display = True
            d = int(a.get("delta", 1))
            phrase = f"{'add' if d > 0 else 'remove'} {abs(d)} {g('particle', 'proton')}"
        elif name == "take_note":
            phrase = f"take a note: {g('text')}"
        elif name == "write_code":
            lang = g("language", "python")
            phrase = f"write {lang if lang in ('python', 'javascript', 'cpp', 'c', 'html', 'arduino') else 'python'} code called {g('name')} that {g('description')}"
        elif name == "pc_control":
            act = g("action")
            phrase = {"volume_up": "volume up", "volume_down": "volume down", "mute": "mute volume", "set_volume": f"volume to {val}",
                      "play_pause": "play music", "next_track": "next song", "previous_track": "previous song",
                      "screenshot": "take a screenshot", "lock": "lock my computer", "status": "system status",
                      "open_app": f"open {val}", "open_website": f"open {val}"}.get(act)
        elif name == "phone_control":
            act = g("action")
            phrase = {"call": "call me" if tgt.lower() == "me" else f"call {tgt}", "whatsapp_call": f"call {tgt} on whatsapp",
                      "whatsapp_video_call": f"video call {tgt} on whatsapp", "open_app": f"open {tgt} on my phone",
                      "web_search": f"google {tgt} on my phone", "wifi_on": "turn on wifi on my phone",
                      "wifi_off": "turn off wifi on my phone", "unlock": "unlock my phone"}.get(act)
        return self._run_local(phrase) if phrase else "Unsupported request."

    # ---- phone commands ---------------------------------------------------------------------
    def _phone_ready(self):
        ok, _ = sc.is_device_connected()
        if not ok:
            self._speak("I can't see your phone. Plug it in with USB debugging on, or say 'connect to my phone over wifi'.")
        return ok

    def _handle_phone_command(self, t):
        t = re.sub(r"what'?s\s?app", "whatsapp", re.sub(r"wi[\s-]fi", "wifi", t))
        log, speak = self._log, self._speak
        m = re.search(r"(video )?call (.+?) (?:on|via|using|through|in) whatsapp", t) or re.search(r"whatsapp (video )?call (?:to )?(.+)$", t)
        if m:
            video, target = bool(m.group(1)), re.sub(r"^my ", "", m.group(2).strip())
            if self._phone_ready():
                ok, info = sc.whatsapp_call(target, video)
                log(f"PHONE: whatsapp {'video ' if video else ''}call {target} {'ok' if ok else 'failed'}")
                speak(f"Starting a WhatsApp {'video ' if video else ''}call with {info}." if ok else info)
            return True
        if re.search(r"connect (?:to )?(?:my )?phone (?:over|via|through|on) wifi|connect (?:to )?(?:my )?phone wirelessly|go wireless", t):
            speak("Connecting to your phone over wifi.")
            ok, info = sc.connect_wireless()
            log(f"PHONE: wireless connect {'ok' if ok else 'failed'} - {info}")
            speak("Connected wirelessly. You can unplug the cable now." if ok else info)
            return True
        m = re.search(r"connect (?:my )?phone to (?:the )?wifi (?:network )?(?:called |named )?(.+)$", t) or re.search(r"connect (?:my )?phone to (?:the )?(.+?) wifi", t)
        if m:
            if self._phone_ready():
                ok, msg = sc.wifi_connect(m.group(1).strip())
                log(f"PHONE: wifi connect {m.group(1).strip()} {'ok' if ok else 'failed'}")
                speak(msg)
            return True
        m = re.search(r"(?:turn|switch) (on|off) (?:the )?wifi (?:on|in) (?:my )?phone|(?:turn|switch) (on|off) (?:my )?phone(?:'s)? wifi", t)
        if m:
            if self._phone_ready():
                on = next(x for x in m.groups() if x) == "on"
                ok = sc.wifi_set(on)
                speak(f"Phone wifi turned {'on' if on else 'off'}." if ok else "I couldn't change the phone's wifi.")
            return True
        m = re.search(r"(?:open|launch|start) (.+?) (?:on|in) (?:my |the )?(?:phone|mobile)", t)
        if m:
            name = m.group(1).strip()
            if self._phone_ready():
                ok = sc.open_app(name)
                log(f"PHONE: open {name} {'ok' if ok else 'failed'}")
                speak(f"Opening {name} on your phone." if ok else f"I couldn't find an app called {name} on your phone.")
            return True
        m = re.search(r"(?:google|search google for|search the web for) (.+?) (?:on|in) (?:my |the )?(?:phone|mobile)", t)
        if m:
            if self._phone_ready():
                ok = sc.web_search(m.group(1).strip())
                speak(f"Searching for {m.group(1).strip()} on your phone." if ok else "I couldn't start that search on your phone.")
            return True
        m = re.search(r"(?:search|find|look)\s+(?:on\s+)?(?:my\s+|the\s+)?(?:phone|mobile)\s+for\s+(.+)$", t) or \
            re.search(r"(?:search for|find|look for)\s+(.+?)\s+(?:on|in)\s+(?:my\s+|the\s+)?(?:phone|mobile)", t)
        if m:
            query, kind = m.group(1).strip(), None
            m2 = re.match(r"(contact|number|app|application|file)s?\s+(?:called\s+|named\s+|for\s+)?(.+)$", query)
            if m2:
                kind = {"contact": "contacts", "number": "contacts", "app": "apps", "application": "apps", "file": "files"}[m2.group(1)]
                query = m2.group(2).strip()
            if not self._phone_ready():
                return True
            speak(f"Searching your phone for {query}.")
            res = sc.search_phone(query, kind)
            contacts, apps, files = res["contacts"], res["apps"], res["files"]
            if not (contacts or apps or files):
                speak(f"I didn't find anything for {query} on your phone.")
                return True
            card = []
            if contacts:
                card.append("Contacts: " + ", ".join(f"{n} {num}" for n, num in contacts[:4]))
            if apps:
                card.append("Apps: " + ", ".join(apps[:4]))
            if files:
                card.append("Files: " + ", ".join(os.path.basename(f) for f in files[:5]))
            self.hologram.show_info_card(f"Search phone: {query}", " | ".join(card))
            bits = [f"{len(l)} {w}{'' if len(l) == 1 else 's'}" for l, w in ((contacts, "contact"), (apps, "app"), (files, "file")) if l]
            spoken = "I found " + ", ".join(bits) + "."
            if contacts:
                spoken += f" Top contact: {contacts[0][0]}."
            elif files:
                spoken += f" Top file: {os.path.basename(files[0])}."
            speak(spoken)
            return True
        return False

    # ---- code helpers --------------------------------------------------------------------------
    def _open_code(self, path, done_msg):
        ok, err = sc.open_in_editor(path)
        self._speak(done_msg if ok else f"{done_msg.split(' and ')[0]}, but I couldn't open the editor. {err[:80] if err else ''}")

    # ---- local (no-API) commands --------------------------------------------------------------------
    def _handle_local_command(self, text):
        t = text.lower()
        H, speak, log = self.hologram, self._speak, self._log
        has = lambda *ks: any(k in t for k in ks)
        showing = bool(re.search(r"\b(show|display|draw|load|model|pull up|bring up)\b", t))

        if t.strip() in STOP_WORDS:
            self._interrupt()
            log("VOICE: stop command")
            return True
        if t.strip() in ("repeat that", "say that again", "what did you say", "can you repeat that", "repeat"):
            speak(self.last_reply or "I haven't said anything yet.")
            return True

        if self.plus is not None and self.plus.route(text):        # cowork, hub, offline mode, file search
            return True
        if sandbox_labs and sandbox_labs.handle_command(H, t, speak):
            return True
        if addons and addons.handle_command(self, t):              # draw mode, panels, sandbox, heart, plots
            return True

        # notes
        m = re.search(r"take a note[:\s]+(.+)$", t)
        if m:
            note = text[m.start(1):m.end(1)].strip()
            try:
                sc.add_note(note)
                log(f"NOTES: added '{note[:60]}'")
                speak("Noted.")
            except Exception as e:
                log(f"NOTES: failed ({e})")
                speak("Sorry, I couldn't save that note.")
            return True
        if has("read my notes", "read notes", "what are my notes", "list my notes"):
            notes = sc.read_notes(5)
            speak(f"Here are your last {len(notes)} notes: " + "; ".join(notes) if notes else "You don't have any notes yet.")
            return True
        if "clear my notes" in t or ("delete" in t and "notes" in t):
            sc.clear_notes()
            speak("Cleared your notes.")
            return True

        # knowledge vault
        m = re.search(r"add (?:the )?folder (.+?) to my vault", t)
        if m:
            ok, info = sc.vault_add_folder(text[m.start(1):m.end(1)])
            speak("Added it. Say 'rebuild my vault' to index it." if ok else info)
            return True
        if has("rebuild my vault", "index my vault", "update my vault"):
            speak("Indexing your vault.")
            f, c = sc.vault_build(log)
            speak(f"Indexed {f} files.")
            return True
        m = re.search(r"(?:find everything (?:i have )?about|what did i write about|search my vault for)\s+(.+)$", t)
        if m:
            hits = sc.vault_search(m.group(1))
            if hits is None:
                speak("Your vault isn't built yet. Add a folder, then rebuild it.")
            elif not hits:
                speak("I found nothing about that in your vault.")
            else:
                H.show_info_card(f"Vault: {m.group(1)}", " | ".join(os.path.basename(h["path"]) for h in hits))
                speak(f"Best match is {os.path.basename(hits[0]['path'])}: {hits[0]['text'][:160]}")
            return True

        # experiment recorder
        m = re.search(r"start experiment(?:\s+(?:called|named)\s+(.+))?$", t)
        if m:
            ok, info = self.experiment.start(m.group(1))
            speak(f"Experiment '{info}' started. Say 'log observation' to record notes." if ok else info)
            return True
        if "stop experiment" in t or ("end" in t and "experiment" in t):
            ok, info = self.experiment.stop()
            speak(f"Experiment '{info}' stopped." if ok else info)
            return True
        m = re.search(r"log observation[:\s]+(.+)$", t)
        if m:
            speak("Observation logged." if self.experiment.log_observation(text[m.start(1):m.end(1)].strip())
                  else "No experiment is running. Say 'start experiment' first.")
            return True
        if "experiment" in t and "screenshot" in t:
            ok, info = self.experiment.capture_screenshot(self.latest_frame)
            speak(f"Screenshot saved: {info}" if ok else info)
            return True
        if "experiment report" in t or ("generate" in t and "report" in t and "experiment" in t):
            path, err = self.experiment.generate_report()
            speak(f"Report generated in the {os.path.basename(os.path.dirname(path))} folder." if path else err)
            return True

        # vision
        if VISION_RE.search(t) or has("what am i looking at", "what is this", "what's this", "scan this", "read this qr", "read this code",
               "read this barcode", "what do you see", "what's in front of me", "describe what you see", "describe the camera"):
            frame = self.latest_frame
            if frame is None:
                speak("I can't get a camera frame. Close any other app using the webcam and try again.")
                return True
            codes = []
            try:
                codes = sc.read_codes(frame)
            except Exception as e:
                log(f"VISION: code scan failed ({e})")
            if codes:
                H.show_info_card("What am I looking at?", "Code found: " + "; ".join(codes))
                speak("I found a code: " + "; ".join(codes))
                return True
            if not self.client:
                speak("I need the Groq API connected to describe what I'm looking at.")
                return True
            speak("Let me take a look.")
            try:
                desc = brain.describe_scene(self.client, frame)
                H.show_info_card("What am I looking at?", desc)
                speak(desc)
            except Exception as e:
                log(f"VISION: describe_scene failed ({e})")
                speak("Sorry, I hit an error looking at the camera.")
            return True

        # faces
        m = re.search(r"(?:remember|enroll) (?:my face )?as (\w+)|enroll me as (\w+)", t)
        if m:
            name = (m.group(1) or m.group(2)).strip().title()
            self.pending_enrollment_name = name
            log(f"FACE: enrollment requested for '{name}'")
            speak(f"Okay {name}. Look at the camera, and slowly turn your head a little while I scan.")
            return True
        if "forget" in t and "face" in t:
            m2 = re.search(r"forget (\w+)(?:'s)? face", t)
            name = m2.group(1).strip().title() if m2 else None
            ok = bool(name) and self.face_id.forget(name)
            log(f"FACE: forgot '{name}'" if ok else "FACE: forget failed")
            speak(f"I've forgotten {name}'s face." if ok else "I don't have that person enrolled.")
            return True
        if has("who do you know", "who have you enrolled", "list enrolled", "list faces", "who's enrolled"):
            names = list(self.face_id.people.keys())
            speak("I know " + ", ".join(names) if names else "I don't have anyone enrolled yet.")
            return True
        if has("check face recognition", "is face recognition working", "face recognition status", "diagnose face"):
            f = self.face_id
            if not f.detection_available:
                speak("Face detection isn't available. That's almost always an OpenCV install conflict: uninstall opencv-python, "
                      "opencv-python-headless and opencv-contrib-python, then install only opencv-contrib-python.")
            elif not f.available:
                speak("I can detect faces but not recognise them, because opencv-contrib's face module isn't loading. Reinstall it cleanly.")
            elif f.people:
                dist = f" Last match distance was {f.last_distance:.0f}, threshold is {f.threshold:.0f}." if f.last_distance else ""
                speak(f"Face recognition is working. Enrolled: {', '.join(f.people)}.{dist}")
            else:
                speak("Face recognition is working, but nobody's enrolled yet. Say 'remember my face as' and your name.")
            return True

        if "what time" in t or "current time" in t:
            speak(time.strftime("It's %I:%M %p"))
            return True

        # computer utilities
        if has("system status", "system stats", "battery", "how's my computer", "how is my computer"):
            speak(sc.get_system_status() or "System stats aren't available. Install psutil.")
            return True
        if "screenshot" in t or "screen shot" in t:
            speak("Screenshot saved to your Pictures folder." if sc.take_screenshot() else "I couldn't take a screenshot.")
            return True
        if re.search(r"\block\b", t) and has("computer", "pc", "laptop", "screen"):
            speak("Locking your computer.")
            sc.lock_workstation()
            return True

        # game companion / recording
        if "game companion" in t and has("stop", "close", "hide", "dismiss"):
            self.game_session.stop()
            H.hide_telemetry()
            self.active_reader = self.telemetry
            speak("Closing game companion.")
            return True
        if has("game companion", "game mode", "show game stats", "game stats", "gaming mode"):
            ok, msg = self.game_session.start()
            H.show_telemetry("Game Companion")
            self.active_reader = self.game_session
            speak(msg + " Say 'start recording' to capture the session.")
            return True
        if has("start recording", "start screen recording", "record my screen", "begin recording"):
            ok, info = sc.start_recording()
            speak("Recording started." if ok else info)
            return True
        if has("stop recording", "stop screen recording", "end recording"):
            speak(sc.stop_recording()[1])
            return True

        # timers
        m = re.search(r"(?:set a |set )?timer for (\d+)\s*(second|minute|hour)s?(?:\s+(?:for|called|named)\s+(.+))?", t)
        if m:
            amount, unit, label = int(m.group(1)), m.group(2), m.group(3)
            plural = "s" if amount != 1 else ""
            self._start_timer(amount * {"second": 1, "minute": 60, "hour": 3600}[unit],
                              label.strip() if label else f"{amount} {unit}{plural} timer")
            speak(f"Timer set for {amount} {unit}{plural}")
            return True
        if "cancel" in t and "timer" in t:
            n = len(self.active_timers)
            self.active_timers.clear()
            speak(f"Cancelled {n} timer{'s' if n != 1 else ''}" if n else "No timers running")
            return True
        if "how much time" in t or ("timer" in t and has("left", "remaining")):
            if not self.active_timers:
                speak("No timers running")
            else:
                parts = []
                for tm in self.active_timers:
                    mins, secs = divmod(max(0, round(tm["ends_at"] - time.time())), 60)
                    parts.append(f"{tm['label']}: {mins} minutes {secs} seconds" if mins else f"{tm['label']}: {secs} seconds")
                speak("; ".join(parts))
            return True

        # quick math
        if has("plus", "minus", "times", "multiplied", "divided", "percent of") or re.search(r"\d\s*[+\-*/]\s*\d", t):
            calc = try_calculate(t)
            if calc:
                log(f"CALC: {calc[1]} = {calc[0]:g}")
                speak(f"That's {calc[0]:g}")
                return True

        # code (AI generation via brain.py)
        m = re.search(r"write (?:(python|javascript|js|cpp|c\+\+|c|html|arduino) )?code called (.+?) that (.+)$", t)
        if m:
            lang, name, desc = (m.group(1) or "python"), m.group(2).strip(), m.group(3).strip()
            if not self.client:
                speak("I need the Groq API connected to generate code.")
                return True
            speak(f"Writing {name} now, one moment.")
            try:
                path = sc.get_or_make_path(name, lang)
                sc.write_file_content(path, brain.generate_code(self.client, desc, lang))
                log(f"CODE: generated '{name}' ({lang}) -> {path}")
                self._open_code(path, f"Done. I wrote {name} and opened it")
            except Exception as e:
                log(f"CODE: generation failed ({e})")
                speak("Sorry, I hit an error generating that code.")
            return True
        m = re.search(r"\bedit (?:code |the file |the sketch |the project )?(.+?) to (.+)$", t)
        if m and (sc.resolve_project_path(m.group(1)) or sc.find_arduino_sketch(m.group(1)) or has("code", "file", "sketch", "project")):
            name, instr = m.group(1).strip(), m.group(2).strip()
            if not self.client:
                speak("I need the Groq API connected to edit code.")
                return True
            path = sc.resolve_project_path(name) or sc.find_arduino_sketch(name)
            if not path:
                speak(f"I couldn't find a file called {name}. Say 'write code called {name} that ...' to create it first.")
                return True
            speak(f"Updating {name} now, one moment.")
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    current = f.read()
                updated = brain.edit_code(self.client, current, instr)
                diff = sc.summarize_diff(current, updated)
                sc.write_file_content(path, updated)
                log(f"CODE: edited '{name}' ({diff}) -> {path}")
                self._open_code(path, f"Updated {name}: {diff}. Opened it")
            except Exception as e:
                log(f"CODE: edit failed ({e})")
                speak("Sorry, I hit an error editing that file.")
            return True
        m = re.search(r"(?:new|create) arduino sketch (?:called |named )?(.+)", t)
        if m:
            path = sc.create_arduino_sketch(m.group(1).strip())
            self._open_code(path, f"Created a new Arduino sketch called {m.group(1).strip()} and opened it")
            return True
        m = re.search(r"(?:new|create) (python|javascript|js|cpp|c\+\+|c|html)?\s*(?:script|file)\s+(?:called |named )?(.+)", t)
        if m:
            path = sc.create_code_file(m.group(2).strip(), (m.group(1) or "python").strip())
            self._open_code(path, f"Created {m.group(2).strip()} and opened it")
            return True
        m = re.search(r"open (.+?) in (?:vs code|vscode|visual studio code)", t)
        if m:
            path = sc.resolve_project_path(m.group(1).strip())
            if not path:
                speak(f"I don't have a project called {m.group(1).strip()} saved. Add it to code_projects.json, or say 'new script called {m.group(1).strip()}'.")
            else:
                ok, err = sc.open_in_vscode(path)
                speak(f"Opening {m.group(1).strip()} in VS Code" if ok else f"I couldn't open that in VS Code. {err[:80]}")
            return True
        m = re.search(r"open (.+?) in arduino", t)
        if m:
            path = sc.find_arduino_sketch(m.group(1).strip())
            if not path:
                speak(f"I don't have a sketch called {m.group(1).strip()}. Say 'new arduino sketch called {m.group(1).strip()}'.")
            else:
                ok, err = sc.open_in_arduino(path)
                speak(f"Opening {m.group(1).strip()} in the Arduino IDE" if ok else f"I couldn't open the Arduino IDE. {err[:80]}")
            return True

        # phone (before PC apps, or "open chrome on my phone" would open it on the PC)
        if self._handle_phone_command(t):
            return True

        # PC apps
        app = sc.APP_RE.search(t)
        if app and re.search(r"\b(open|launch|start)\b", t):
            ok = sc.launch_app(app.group(1))
            log(f"APP: {'launched' if ok else 'failed to launch'} {app.group(1)}")
            speak(f"Opening {app.group(1)}" if ok else f"I couldn't open {app.group(1)}")
            return True

        # volume / media
        if "volume" in t:
            m = re.search(r"volume to (\d+)", t)
            if m:
                pct = int(m.group(1))
                speak(f"Volume set to {pct} percent" if sc.set_volume_percent(pct) else "Volume control isn't available. Install pycaw.")
                return True
            if "mute" in t:
                speak("Muted" if sc.set_volume_percent(0) else "Volume control isn't available")
                return True
            for word, delta in (("up", 10), ("down", -10)):
                if re.search(rf"\b{word}\b", t):
                    v = sc.adjust_volume_percent(delta)
                    speak(f"Volume at {v} percent" if v is not None else "Volume control isn't available")
                    return True
        if has("pause music", "play music", "pause the music", "play the music") or t.strip() in ("play", "pause"):
            sc.media_play_pause()
            speak("Done")
            return True
        if has("next song", "next track", "skip song", "skip track"):
            sc.media_next()
            speak("Skipping")
            return True
        if has("previous song", "previous track", "last song", "go back a song"):
            sc.media_previous()
            speak("Going back")
            return True

        # phone calls / unlock
        if re.search(r"\b(call me|give me a call|call my phone|ring me)\b", t):
            number = sc.load_my_number()
            if not number:
                speak("I don't have your number saved. Create my_number.txt next to api_key.txt with your phone number.")
            elif self._phone_ready():
                ok, out = sc.call_number(number)
                log(f"PHONE: call me {'ok' if ok else 'failed'} - {out[:80]}")
                speak("Calling you now" if ok else "I couldn't reach your phone to call you")
            return True
        if "unlock" in t and "phone" in t:
            if self._phone_ready():
                ok, out = sc.unlock_with_pin()
                log(f"PHONE: unlock {'ok' if ok else 'failed'} - {out[:80]}")
                speak("Phone unlocked" if ok else "I couldn't unlock the phone. Check phone_pin.txt is set up.")
            return True
        m = re.search(r"^\s*call (\w+(?:\s\w+)?)\s*$", t)
        if m:
            name = m.group(1).strip()
            number = sc.load_contacts().get(name.lower())
            if not number:
                speak(f"I don't have a number saved for {name}. Add them to contacts.json first.")
            elif self._phone_ready():
                ok, out = sc.call_number(number)
                log(f"PHONE: call {name} {'ok' if ok else 'failed'} - {out[:80]}")
                speak(f"Calling {name}" if ok else f"I couldn't reach your phone to call {name}")
            return True
        m = re.search(r"\bopen (google|youtube|github|gmail)\b", t)
        if m:
            webbrowser.open(LOCAL_SITES[m.group(1)])
            speak(f"Opening {m.group(1)}")
            return True

        # orbit / particle editing
        m = re.search(r"select orbit (\w+)", t)
        if m:
            num = NUMBER_WORDS.get(m.group(1))
            if num is None:
                speak("Which orbit number would you like?")
            elif H.select_orbit(num - 1):
                speak(random.choice([f"Orbit {num}, got it.", f"Selected orbit {num}.", f"Orbit {num} is now active.", f"You're editing orbit {num} now."]))
            else:
                speak(f"There's no orbit {num} on the current display.")
            return True
        if "deselect" in t and "orbit" in t:
            H.deselect_orbit()
            speak("Orbit deselected")
            return True
        m = re.search(r"\b(add|remove)\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)?\s*(proton|neutron|electron)s?\b", t)
        if m:
            d, cw, particle = m.groups()
            count = NUMBER_WORDS.get(cw, 1) if cw else 1
            {"proton": H.add_protons, "neutron": H.add_neutrons, "electron": H.add_electrons}[particle](count if d == "add" else -count)
            log(f"EDIT: {d} {count} {particle} -> {H.mode_label}")
            speak(f"{'Added' if d == 'add' else 'Removed'} {count} {particle}{'s' if count != 1 else ''}. "
                  f"Now {H.protons} protons, {H.neutrons} neutrons, {H.electron_count} electrons.")
            return True
        if "new element" in t or ("start" in t and "element" in t):
            H.protons, H.neutrons, H.electron_count = 1, 0, 1
            H._ensure_atom_mode()
            H._update_custom_label()
            speak("Starting a new element with one proton and one electron. Tell me what to add.")
            return True

        # IoT
        if has("connect to my arduino", "connect my arduino", "connect arduino", "connect to my esp32", "connect my esp32",
               "connect esp32", "connect iot device", "connect to my device"):
            ok, msg = self.telemetry.start()
            self.active_reader = self.telemetry
            speak(msg if not ok else "Connected. Say 'show my telemetry' to see it on the display.")
            return True
        if "telemetry" in t and has("hide", "close", "stop", "dismiss"):
            self.telemetry.stop()
            H.hide_telemetry()
            self.active_reader = self.telemetry
            speak("Closing telemetry")
            return True
        if "telemetry" in t and has("show", "display"):
            nm = re.search(r"(?:show|display)\s+(?:my\s+|the\s+)?(.+?)\s+telemetry", t)
            label = nm.group(1).strip() if nm and nm.group(1).strip() else "device"
            ok, msg = self.telemetry.start()
            self.active_reader = self.telemetry
            H.show_telemetry(label)
            speak(f"Connecting to your {label} telemetry now." if ok else f"Showing the {label} display, but couldn't connect: {msg}")
            return True

        # astronomy / lab
        if has("astronomy mode", "what can astronomy mode show"):
            H.show_info_card("Astronomy Mode", "Star maps for: " + ", ".join(sorted(CONSTELLATIONS)).title() + ".")
            speak("Astronomy mode can show star maps for constellations like Orion, the Big Dipper, Cassiopeia, and more. Just say 'show Orion'.")
            return True
        const = next((c for c in CONSTELLATIONS if re.search(rf"\b{re.escape(c)}\b", t)), None) or \
            CONSTELLATION_ALIASES.get(next((a for a in CONSTELLATION_ALIASES if re.search(rf"\b{re.escape(a)}\b", t)), ""))
        if const and has("show", "display", "find", "locate"):
            H.load_constellation(const)
            speak(f"Displaying {const.title()}")
            return True
        if has("lab mode", "what can lab mode show", "lab mode options"):
            summary = "Molecules like water or methane, atoms, planets, satellites, DNA, math graphs, physics simulations like a pendulum or projectile, and 3D shapes."
            H.show_info_card("Lab Mode", summary)
            speak("Lab mode can show " + summary + " Just ask, like 'show a water molecule' or 'graph sine of x'.")
            return True
        mol = next((k for k in MOLECULES if re.search(rf"\b{re.escape(k)}\b", t)), None) or \
            MOLECULE_ALIASES.get(next((a for a in MOLECULE_ALIASES if re.search(rf"\b{re.escape(a)}\b", t)), ""))
        if mol and has("show", "display", "model", "molecule", "draw"):
            H.load_molecule(mol)
            speak(f"Displaying a {mol} molecule")
            return True
        if "pendulum" in t and has("show", "simulate", "display"):
            H.load_physics_sim("pendulum")
            speak("Simulating a pendulum")
            return True
        if has("projectile", "trajectory") and has("show", "simulate", "display"):
            H.load_physics_sim("projectile")
            speak("Simulating a projectile")
            return True
        if "satellite" in t and has("show", "display"):
            nm = re.search(r"(?:show|display)\s+(?:a\s+|the\s+)?(.+?)\s+satellite", t)
            sat = nm.group(1).strip().upper() if nm and nm.group(1).strip() else "ISS"
            H.load_satellite(sat)
            speak(f"Displaying {sat} orbiting Earth")
            return True

        # solar system / network / stepping
        if "solar system" in t and (showing or has("open", "take me")):
            H.load_solar_system()
            speak("Displaying the solar system")
            return True
        if (has("show my systems", "system map", "node graph") or (has("network", "constellation") and showing)):
            H.load_network()
            speak("Here's a map of my systems")
            return True
        if has("next", "previous", "last") and has("element", "atom"):
            sym, name, z = H.next_element() if "next" in t else H.previous_element()
            speak(f"{name.title()}, symbol {sym}. Element {z} of 118.")
            return True
        if has("next", "previous", "last") and "system" in t:
            label, count, _ = H.next_star_system() if "next" in t else H.previous_star_system()
            speak(f"The {label} system, {count} planets.")
            return True
        star = next((s for s in STAR_SYSTEMS if s in t or s.replace("-", " ") in t), None)
        if star and has("show", "display", "system"):
            label, count, _ = H.load_star_system(star)
            speak(f"Displaying the {label} system, {count} planets.")
            return True
        if "star system" in t or re.search(r"\bsystem (?:called|named|of)\b", t):
            m = re.search(r"(?:star system|system)\s+(?:called\s+|named\s+|of\s+)?([a-zA-Z0-9\- ]+?)$", t)
            label, count, gen = H.load_star_system(m.group(1).strip() if m and m.group(1).strip() else "unknown")
            speak(f"Displaying the {label} system, {count} planets." + (" I made this one up since I don't have real data for it." if gen else ""))
            return True

        # weather
        if "weather" in t:
            if has("hide", "close", "dismiss"):
                H.hide_weather()
                speak("Closing the weather display")
                return True
            loc = self._weather_place(t)
            state_key = next((s for s in US_STATE_POSITIONS if loc and s in loc), None)
            data = self._fetch_weather_data(loc)
            if data:
                if state_key:
                    data["state"] = state_key
                H.show_weather(data)
                temp = f"{round(data['temp_c'])} degrees" if data["temp_c"] is not None else "an unknown temperature"
                speak(f"It's {temp} and {data['description']} in {data['location']}.")
            else:
                speak({"not_found": f"I couldn't find a place called {loc}. Try adding a country or state.",
                       "no_location": "I couldn't work out your location. Try 'weather in Chicago'.",
                       "network": "I couldn't reach the weather service. Check your internet connection."}.get(
                    self.last_weather_error, "Sorry, I couldn't get the weather right now."))
            return True

        # shapes / elements (require display intent so ordinary questions reach the LLM)
        shape = next((s for s in sorted(SHAPES) if re.search(rf"\b{re.escape(s)}\b", t)), None) or \
            SHAPE_ALIASES.get(next((a for a in SHAPE_ALIASES if re.search(rf"\b{re.escape(a)}\b", t)), ""))
        if shape and showing:
            H.load_shape(shape)
            speak(f"Displaying a {shape} model")
            return True
        if has("sphere", "globe", "round thing") and showing:
            H.load_shape("sphere")
            speak("Displaying a sphere")
            return True
        el = next((n for n in sorted(ELEMENTS, key=len, reverse=True) if re.search(rf"\b{n}\b", t)), None)
        if el and (showing or re.search(r"\batom\b", t)):
            res = H.load_atom(el)
            speak(f"Displaying a {res[1]} atom, symbol {res[0]}" if res else f"I don't have a model for {el}.")
            return True
        if "reset" in t and has("display", "hologram", "diagram"):
            H.load_demo()
            speak("Resetting the display")
            return True
        return False

    # ---- weather (Open-Meteo, free, keyless) ---------------------------------------------------------
    _PLACE_NOISE = re.compile(r"\b(right now|currently|today|tonight|tomorrow|now|like|please|outside|at the moment|this week)\b.*$")
    _PLACE_FILLER = {"what", "whats", "what's", "how", "hows", "how's", "the", "is", "show", "me", "tell", "get", "current",
                     "check", "give", "display", "open", "a", "my", "it", "in"}

    @classmethod
    def _weather_place(cls, t):
        """'weather in new york right now' / 'paris weather' / 'what's the weather like in Delhi' -> place, else None (auto-locate)."""
        m = re.search(r"\b(?:weather|forecast)\b.*?\b(?:in|at|for|of)\s+(.+)$", t) or \
            re.search(r"\b(?:in|at|for)\s+(.+?)\s+(?:weather|forecast)\b", t)
        if m:
            place = m.group(1)
        else:
            m = re.search(r"^\s*(.+?)\s+(?:weather|forecast)\s*$", t)
            place = " ".join(w for w in m.group(1).split() if w not in cls._PLACE_FILLER) if m else ""
        place = cls._PLACE_NOISE.sub("", place).strip(" ,.?!")
        return None if place in ("", "here", "me", "home", "my location", "my area", "my city") else place

    def _get_json(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "aurora-hologram/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())

    def _geocode_location(self, query):
        name, _, hint = query.partition(",")
        name, hint = name.strip(), hint.strip().lower()
        try:
            data = self._get_json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
                {"name": name, "count": 10, "language": "en", "format": "json"}))
        except Exception as e:
            self._log(f"VOICE: geocoding failed ({e})")
            return None
        results = data.get("results") or []
        if hint:
            results = [r for r in results if hint in " ".join(str(r.get(k, "")) for k in ("country", "admin1", "country_code")).lower()] or results
        if not results:
            return None
        r = results[0]
        parts = [r.get("name")] + ([r["admin1"]] if r.get("admin1") and r["admin1"] != r.get("name") else []) + [r.get("country")]
        return r["latitude"], r["longitude"], ", ".join(p for p in parts if p)

    def _ip_geolocate(self):
        try:
            d = self._get_json("https://ipapi.co/json/")
        except Exception as e:
            self._log(f"VOICE: IP geolocation failed ({e})")
            return None
        if d.get("latitude") is None or d.get("longitude") is None:
            return None
        return d["latitude"], d["longitude"], ", ".join(p for p in (d.get("city"), d.get("region"), d.get("country_name")) if p) or "your location"

    def _fetch_weather_data(self, query):
        self.last_weather_error = None
        geo = self._geocode_location(query) if query else self._ip_geolocate()
        if geo is None:
            self.last_weather_error = "not_found" if query else "no_location"
            return None
        lat, lon, name = geo
        try:
            data = self._get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
                {"latitude": lat, "longitude": lon, "timezone": "auto",
                 "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"}))
        except Exception as e:
            self._log(f"VOICE: open-meteo failed ({e})")
            self.last_weather_error = "network"
            return None
        cur = data.get("current", {})
        cond, desc = self.WMO.get(cur.get("weather_code"), ("cloudy", "unknown conditions"))
        return {"location": name, "temp_c": cur.get("temperature_2m"), "condition": cond, "description": desc,
                "humidity": cur.get("relative_humidity_2m"), "wind_kph": cur.get("wind_speed_10m"),
                "updated_at": time.strftime("%H:%M")}

    # ---- main listen loop ---------------------------------------------------------------------------
    def _run_loop(self):
        r = self.recognizer
        r.dynamic_energy_threshold = False       # dynamic mode learns Aurora's own voice as "noise"
        r.pause_threshold, r.non_speaking_duration, r.phrase_threshold = 0.9, 0.4, 0.3
        override = self._read_number("energy_threshold.txt", float)
        if override:
            r.energy_threshold = override
            self._log(f"VOICE: energy_threshold.txt override -> {override:.0f}")
        else:
            try:
                with self._mic_lock, self.microphone as src:
                    self._log("VOICE: calibrating for ambient noise (2 sec)...")
                    r.adjust_for_ambient_noise(src, duration=2)
                r.energy_threshold = max(50, min(600, r.energy_threshold))
                self._log(f"VOICE: calibrated (energy_threshold={r.energy_threshold:.0f})")
            except Exception as e:
                self._log(f"VOICE: could not calibrate microphone ({e})")
                return

        loops, last_cal = 0, time.time()
        while self._running:
            loops += 1
            if not override and time.time() - last_cal > 120 and not self.speech.busy:
                try:
                    with self._mic_lock, self.microphone as src:
                        r.adjust_for_ambient_noise(src, duration=1)
                    r.energy_threshold = max(50, min(600, r.energy_threshold))
                except Exception:
                    pass
                last_cal = time.time()
            try:
                with self._mic_lock, self.microphone as src:
                    audio = r.listen(src, timeout=5, phrase_time_limit=8)
                text = r.recognize_google(audio)
                self._log(f"VOICE: picked up audio -> '{text}'")
            except sr.WaitTimeoutError:
                continue
            except sr.UnknownValueError:
                if loops % 5 == 0:
                    self._log("VOICE: heard audio but couldn't transcribe it")
                continue
            except Exception as e:
                self._log(f"VOICE ERROR: {e}")
                time.sleep(1)
                continue

            found, command = find_wake_word(text)
            if not found:
                continue
            command = command.strip(" ,.")
            if not command:
                self._speak("Yes?")
                continue
            self._state, self.last_heard = "listening", command
            self._log(f"HEARD: {command}")
            try:
                if not self._handle_local_command(command):
                    self._ask(command)
            except Exception as e:
                import traceback
                self._log(f"VOICE: command crashed ({type(e).__name__}: {e})")
                traceback.print_exc()
            finally:
                self._state = "idle"
