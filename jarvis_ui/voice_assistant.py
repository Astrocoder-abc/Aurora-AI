"""
Background voice pipeline: wake word -> speech-to-text -> Groq API brain
(groq/compound, built-in web search) -> natural neural speech reply.
Also handles "show me a diagram of X" and "select orbit N" style commands
by driving the hologram directly (no API call needed, so it's instant).

TTS: uses Microsoft Edge's free neural voices via the `edge-tts` package
(no API key, genuinely free) for a natural, fluent voice, with the
offline Windows voice as an automatic fallback if that ever fails
(no internet, package missing, etc).

SETUP:
  1. Get a free API key at https://console.groq.com/ (no credit card).
  2. Put it in api_key.txt in this same folder, or set GROQ_API_KEY.
  3. Say "Aurora" + your request, e.g.:
     "Aurora, what's the weather like right now?"
     "Aurora, show me a carbon atom"
     "Aurora, show me the solar system"
     "Aurora, select orbit one"
     "Aurora, what time is it?"
"""

import asyncio
import ast
import difflib
import operator
import math
import os

from .paths import PROJECT_ROOT
import random
import queue
import re
import tempfile
import threading
import time
import webbrowser

import speech_recognition as sr
import pygame

from .hologram import ELEMENTS, SHAPES, SHAPE_ALIASES, US_STATE_POSITIONS, STAR_SYSTEMS
from . import system_control
from . import phone_control
from .timers import TimerManager
from .weather import WeatherError, extract_location, fetch_current_weather

try:
    from groq import Groq
except ImportError:
    Groq = None

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

WAKE_WORDS = ["aurora", "hey aurora"]
WAKE_WORD_CORE = "aurora"
WAKE_WORD_FUZZY_THRESHOLD = 0.72  # tuned so "arora" (a common mishearing) matches


def find_wake_word(text):
    """Returns (True, remaining_text_after_it) if a wake word is found —
    using fuzzy matching per-word, not just an exact substring check,
    since speech recognition commonly mangles "Aurora" into similar-
    sounding words ("Arora" is a frequent one, since it's a real name
    Google's model is biased toward). Exact matches for "aurora" still
    work as before; this just also catches near-misses instead of
    requiring an ever-growing hardcoded alias list."""
    words = text.split()
    for i, w in enumerate(words):
        clean = w.strip(",.!?").lower()
        matched = clean == WAKE_WORD_CORE or \
            difflib.SequenceMatcher(None, clean, WAKE_WORD_CORE).ratio() >= WAKE_WORD_FUZZY_THRESHOLD
        if matched:
            after = " ".join(words[i + 1:])
            before = " ".join(words[:i])
            return True, (after if after else before)
    return False, None
API_KEY_FILE = os.path.join(str(PROJECT_ROOT), "api_key.txt")

# groq/compound does live web search internally, but that reasoning step
# adds real latency even for questions that don't need it — a plain "hi"
# was going through the same search-capable pipeline as "what's the
# weather", which is why replies felt slow. Fast model for normal chat,
# compound reserved for the one place that actually needs search.
MODEL_FAST = "llama-3.3-70b-versatile"
MODEL_SEARCH = "groq/compound"

# en-GB-RyanNeural is a natural British male voice. Swap for any other
# Edge neural voice name if you prefer a different one (run
# `edge-tts --list-voices` to see all options).
EDGE_VOICE = "en-GB-RyanNeural"

SYSTEM_PROMPT = (
    "You are Aurora, a witty, concise voice assistant speaking out loud to "
    "your user through text-to-speech. Keep replies short — 1 to 3 "
    "sentences — since long replies are tedious to listen to. Be direct "
    "and helpful. Always respond to greetings like 'hi' or 'hello' warmly, "
    "even briefly. IMPORTANT: never use markdown formatting — no asterisks, "
    "bullet points, numbered lists, headers, or backticks. Write in plain "
    "spoken sentences only, since this text is read aloud, not displayed."
)


def clean_for_speech(text):
    """Strip markdown formatting so TTS doesn't read out '*', '#', etc.
    literally. Belt-and-suspenders alongside the system prompt asking the
    model not to use markdown in the first place."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\*\-\+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_~`#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

LOCAL_SITES = {
    "google": "https://google.com",
    "youtube": "https://youtube.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
}

_SAFE_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def _safe_eval_node(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        result = node.value
    elif isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        left, right = _safe_eval_node(node.left), _safe_eval_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent too large")
        result = _SAFE_OPS[type(node.op)](left, right)
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        result = _SAFE_OPS[type(node.op)](_safe_eval_node(node.operand))
    else:
        raise ValueError("unsupported expression")
    if type(result) not in (int, float) or abs(result) > 1e15 or not math.isfinite(result):
        raise ValueError("result outside calculator limits")
    return result


def try_calculate(text):
    """Returns (result, display_expression) or None if the text doesn't
    look like a calculable expression."""
    if len(text) > 200:
        return None
    t = text.lower()

    m = re.search(r"([\d.]+)\s*percent of\s*([\d.]+)", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        result = a / 100 * b
        return (result, f"{a}% of {b}") if math.isfinite(result) and abs(result) <= 1e15 else None

    expr = t
    expr = re.sub(r"\bplus\b", "+", expr)
    expr = re.sub(r"\bminus\b", "-", expr)
    expr = re.sub(r"\btimes\b|\bmultiplied by\b", "*", expr)
    expr = re.sub(r"\bdivided by\b|\bover\b", "/", expr)
    expr = re.sub(r"[^0-9+\-*/(). ]", "", expr)
    expr = expr.strip()
    if not expr or not re.search(r"\d", expr) or not any(op in expr for op in "+-*/"):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 64:
            return None
        return _safe_eval_node(tree.body), expr
    except Exception:
        return None


NUMBER_WORDS = {
    "one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3,
    "four": 4, "4": 4, "five": 5, "5": 5, "six": 6, "6": 6,
    "seven": 7, "7": 7, "eight": 8, "8": 8, "nine": 9, "9": 9,
    "ten": 10, "10": 10,
}


def load_api_key():
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    return os.environ.get("GROQ_API_KEY")


async def _edge_tts_save(text, path):
    communicate = edge_tts.Communicate(text, voice=EDGE_VOICE)
    await communicate.save(path)


class VoiceAssistant:
    def __init__(self, hologram, face_id, on_log=None):
        self.hologram = hologram
        self.face_id = face_id
        self.state = "idle"       # idle | listening | thinking | speaking
        self.last_heard = ""
        self.last_reply = ""
        self.enabled = False

        external_log = on_log or (lambda text: None)
        log_file_path = os.path.join(str(PROJECT_ROOT), "voice_debug.log")

        def combined_log(text):
            print(text, flush=True)
            try:
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception:
                pass
            external_log(text)

        self._on_log = combined_log
        self._on_log(f"VOICE: === startup {time.strftime('%H:%M:%S')} ===")

        self._running = False
        self._thread = None
        self.history = []
        self.timers = TimerManager(self._timer_finished)
        self._speech_queue = queue.Queue(maxsize=20)
        self._mic_lock = threading.Lock()
        self.last_weather_error = None
        self.pending_enrollment_name = None  # set by voice, consumed by main.py's camera loop

        self.api_key = load_api_key()
        if Groq is None:
            self._on_log("VOICE: 'groq' package not installed — general chat unavailable; local voice commands remain available")
            self.client = None
        elif not self.api_key:
            self._on_log("VOICE: no API key found (api_key.txt or GROQ_API_KEY) — general chat unavailable; local voice commands remain available")
            self.client = None
        else:
            self.client = Groq(api_key=self.api_key, timeout=20, max_retries=1)

        try:
            self.recognizer = sr.Recognizer()
            self.recognizer.operation_timeout = 8
            mic_names = sr.Microphone.list_microphone_names()
            self._on_log(f"VOICE: found {len(mic_names)} audio input device(s):")
            for i, name in enumerate(mic_names):
                self._on_log(f"  [{i}] {name}")

            device_index = self._load_mic_index()
            if device_index is not None:
                self._on_log(f"VOICE: using mic_index.txt override -> device [{device_index}]")
                self.microphone = sr.Microphone(device_index=device_index)
            else:
                self.microphone = sr.Microphone()
                self._on_log("VOICE: using system default input device "
                             "(if voice never picks up audio, create mic_index.txt "
                             "with the number of the correct device from the list above)")
        except Exception as e:
            self._on_log(f"VOICE: microphone init failed ({e}) — voice AI disabled")
            self.recognizer = None
            self.microphone = None

        self._tts_engine = None
        if pyttsx3:
            try:
                self._tts_engine = pyttsx3.init()
                self._tts_engine.setProperty("rate", 175)
                self._select_fallback_voice()
            except Exception as e:
                self._on_log(f"VOICE: offline TTS init failed ({e})")

        if EDGE_TTS_AVAILABLE:
            fallback_note = "offline fallback ready" if self._tts_engine else "no offline fallback available"
            self._on_log(f"VOICE: using Edge neural voice '{EDGE_VOICE}' ({fallback_note})")
        elif self._tts_engine:
            self._on_log("VOICE: edge-tts not installed, using offline voice")
        else:
            self._on_log("VOICE: no TTS backend available — replies will be text-only")

    def _load_mic_index(self):
        path = os.path.join(str(PROJECT_ROOT), "mic_index.txt")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    content = f.read().strip()
                    if content:
                        return int(content)
            except Exception:
                pass
        return None

    def _select_fallback_voice(self):
        try:
            voices = self._tts_engine.getProperty("voices")
        except Exception:
            return
        for v in voices:
            if any(k in v.name.lower() for k in ("david", "male", "mark", "guy")):
                self._tts_engine.setProperty("voice", v.id)
                return

    def start(self):
        if self._running or not self.microphone:
            return
        self.enabled = True
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._on_log("VOICE: listening for wake word 'Aurora'")

    def stop(self):
        self.enabled = False
        self._running = False
        self.timers.cancel_all()
        try:
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except pygame.error:
            pass

    # ---- speech output -----------------------------------------------------

    def speak_now(self, text):
        """Queue greetings/timer notices; never block the graphics thread on TTS."""
        if not self.enabled:
            self._on_log(text)
            return
        try:
            self._speech_queue.put_nowait(text)
        except queue.Full:
            self._on_log("VOICE: notification queue full; skipped speech")

    def _speak(self, text):
        text = clean_for_speech(text)
        self.state = "speaking"
        self.last_reply = text
        self._on_log(f"AURORA: {text}")

        if EDGE_TTS_AVAILABLE:
            self._on_log("VOICE: attempting Edge neural speech...")
            if self._speak_edge(text):
                self._on_log("VOICE: Edge speech played OK")
                self.state = "idle"
                return

        if self._tts_engine:
            self._on_log("VOICE: attempting offline speech...")
            try:
                self._tts_engine.say(text)
                self._tts_engine.runAndWait()
                self._on_log("VOICE: offline speech played OK")
            except Exception as e:
                self._on_log(f"VOICE: offline TTS error ({e})")
        else:
            self._on_log("VOICE: no TTS backend available, reply is text-only")
        self.state = "idle"

    def _speak_edge(self, text):
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            asyncio.run(asyncio.wait_for(_edge_tts_save(text, path), timeout=15))
            if not self._running:
                return True
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()

            # The main listening loop isn't touching the mic right now
            # (it's blocked here, waiting on us), so it's safe to use it
            # for a short-lived "did they say stop?" listener during
            # playback — this is what lets "Aurora, stop" actually
            # interrupt mid-sentence instead of only working between turns.
            stop_watcher = threading.Thread(target=self._watch_for_stop, daemon=True)
            stop_watcher.start()

            while self._running and pygame.mixer.music.get_busy():
                time.sleep(0.05)
            return True
        except Exception as e:
            self._on_log(f"VOICE: edge-tts failed ({e}), falling back to offline voice")
            return False
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def _watch_for_stop(self):
        """Runs only while audio is playing. Listens for a short phrase and
        interrupts playback immediately if it hears 'stop'."""
        try:
            while self._running and pygame.mixer.music.get_busy():
                with self._mic_lock, self.microphone as source:
                    audio = self.recognizer.listen(source, timeout=1.5, phrase_time_limit=2)
                text = self.recognizer.recognize_google(audio).lower()
                if "stop" in text or "aurora" in text:
                    self._on_log("VOICE: stop heard, interrupting speech")
                    pygame.mixer.music.stop()
                    return
        except Exception:
            pass  # timeouts / no speech / mic hiccups are all fine here

    # ---- timers -----------------------------------------------------------

    def _timer_finished(self, label):
        self._on_log(f"TIMER: {label} finished")
        self.speak_now(f"{label} is up!" if label else "Timer's up!")

    def _start_timer(self, seconds, label):
        self.timers.start(seconds, label)
        self._on_log(f"VOICE: timer started — {label} ({seconds}s)")

    # ---- local (no-API) commands, including hologram diagrams -------------

    def _handle_local_command(self, text):
        t = text.lower().strip()
        if "weather" in t:
            return self._handle_weather(t)
        if t in ("show diagnostics", "hide diagnostics"):
            self.hologram.show_diagnostics = t.startswith("show")
            self._speak("Diagnostics " + ("shown" if self.hologram.show_diagnostics else "hidden"))
            return True
        if t in ("reduce motion", "less motion", "resume animation"):
            self.hologram.reduced_motion = t != "resume animation"
            self._speak("Motion reduced" if self.hologram.reduced_motion else "Animation resumed")
            return True
        if t in ("show core", "show the core", "close display", "close the display"):
            self.hologram.show_core()
            self._speak("Returning to the core")
            return True
        if t in ("help", "show help", "show commands", "what can you do"):
            self.hologram.show_help = True
            self._speak("Try asking me to show a carbon atom, change theme, or show weather in your city.")
            return True
        if t in ("close help", "hide help"):
            self.hologram.show_help = False
            self._speak("Closing the guide")
            return True
        if t in ("change theme", "next theme", "change color"):
            self.hologram.cycle_theme(1)
            self._speak("Theme changed")
            return True
        if t in ("scroll down", "read more", "next page"):
            self.hologram.scroll_info(1)
            return True
        if t in ("scroll up", "previous page"):
            self.hologram.scroll_info(-1)
            return True

        if t in ("close answer", "close the answer", "dismiss response", "close response"):
            self.hologram.show_core()
            self._speak("Closing the answer")
            return True
        if t.strip() in ("stop", "stop it", "be quiet", "silence"):
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
            self._on_log("VOICE: stop command")
            return True

        m = re.search(r"(?:remember|enroll) (?:my face )?as (\w+)|enroll me as (\w+)", t)
        if m:
            name = (m.group(1) or m.group(2)).strip().title()
            self.pending_enrollment_name = name
            self._on_log(f"FACE: enrollment requested for '{name}'")
            self._speak(f"Okay, look at the camera and hold still for a moment, {name}.")
            return True

        if "forget" in t and "face" in t:
            m2 = re.search(r"forget (\w+)(?:'s)? face", t)
            name = m2.group(1).strip().title() if m2 else None
            if name and self.face_id.forget(name):
                self._on_log(f"FACE: forgot '{name}'")
                self._speak(f"I've forgotten {name}'s face.")
            else:
                self._speak("I don't have that person enrolled.")
            return True

        if any(k in t for k in ("who do you know", "who have you enrolled", "list enrolled", "list faces", "who's enrolled")):
            names = list(self.face_id.people.keys())
            self._speak("I know " + ", ".join(names) if names else "I don't have anyone enrolled yet.")
            return True

        if "what time" in t or "current time" in t:
            self._speak(time.strftime("It's %I:%M %p"))
            return True

        # ---- timers ---------------------------------------------------------
        m = re.search(r"(?:set a |set )?timer for (\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(second|minute|hour)s?(?:\s+(?:for|called|named)\s+(.+))?", t)
        if m:
            amount = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
            unit, label = m.group(2), m.group(3)
            seconds = amount * {"second": 1, "minute": 60, "hour": 3600}[unit]
            display_label = label.strip() if label else f"{amount} {unit}{'s' if amount != 1 else ''} timer"
            if not 0 < seconds <= 86400:
                self._speak("Choose a timer between one second and twenty four hours.")
                return True
            self._start_timer(seconds, display_label)
            self._speak(f"Timer set for {amount} {unit}{'s' if amount != 1 else ''}")
            return True

        if "cancel" in t and "timer" in t:
            count = self.timers.cancel_all()
            self._on_log(f"VOICE: cancelled {count} timer(s)")
            self._speak(f"Cancelled {count} timer{'s' if count != 1 else ''}" if count else "No timers running")
            return True

        if "how much time" in t or ("timer" in t and any(k in t for k in ("left", "remaining"))):
            active_timers = self.timers.snapshot()
            if not active_timers:
                self._speak("No timers running")
            else:
                parts = []
                for timer in active_timers:
                    remaining = round(timer["remaining"])
                    mins, secs = divmod(remaining, 60)
                    parts.append(f"{timer['label']}: {mins} minutes {secs} seconds" if mins else f"{timer['label']}: {secs} seconds")
                self._speak("; ".join(parts))
            return True

        # ---- quick math -------------------------------------------------------
        if any(k in t for k in ("plus", "minus", "times", "multiplied", "divided", "percent of")) or \
                re.search(r"\d\s*[+\-*/]\s*\d", t):
            calc = try_calculate(t)
            if calc:
                result, expr = calc
                result_str = f"{result:g}"
                self._on_log(f"CALC: {expr} = {result_str}")
                self._speak(f"That's {result_str}")
                return True

        # ---- app launching ------------------------------------------------------
        app_hit = next((a for a in system_control.APP_COMMANDS if a in t), None)
        if app_hit and any(k in t for k in ("open", "launch", "start")):
            ok = system_control.launch_app(app_hit)
            self._on_log(f"APP: {'launched' if ok else 'failed to launch'} {app_hit}")
            self._speak(f"Opening {app_hit}" if ok else f"I couldn't open {app_hit}")
            return True

        # ---- volume ---------------------------------------------------------
        if "volume" in t or t in ("mute", "mute audio"):
            m = re.search(r"volume to (\d+)", t)
            if m:
                pct = max(0, min(100, int(m.group(1))))
                ok = system_control.set_volume_percent(pct)
                self._speak(f"Volume set to {pct} percent" if ok else "Volume control isn't available — install pycaw")
                return True
            if "mute" in t:
                ok = system_control.set_volume_percent(0)
                self._speak("Muted" if ok else "Volume control isn't available")
                return True
            if "up" in t:
                new_val = system_control.adjust_volume_percent(10)
                self._speak(f"Volume at {new_val} percent" if new_val is not None else "Volume control isn't available")
                return True
            if "down" in t:
                new_val = system_control.adjust_volume_percent(-10)
                self._speak(f"Volume at {new_val} percent" if new_val is not None else "Volume control isn't available")
                return True

        # ---- media playback -----------------------------------------------------
        if any(k in t for k in ("pause music", "play music", "pause the music", "play the music")) or t.strip() in ("play", "pause"):
            ok = system_control.media_play_pause()
            self._on_log(f"MEDIA: play/pause {ok}")
            self._speak("Playback toggled" if ok else "Media control is unavailable on this device")
            return True
        if any(k in t for k in ("next song", "next track", "skip song", "skip track")):
            ok = system_control.media_next()
            self._speak("Skipping" if ok else "Media control is unavailable on this device")
            return True
        if any(k in t for k in ("previous song", "previous track", "last song", "go back a song")):
            ok = system_control.media_previous()
            self._speak("Going back" if ok else "Media control is unavailable on this device")
            return True

        # ---- phone (requires ADB setup — see phone_control.py) ------------------
        if "unlock" in t and "phone" in t:
            connected, _ = phone_control.is_device_connected()
            if not connected:
                self._speak("I can't see your phone — check it's connected via USB with debugging enabled")
                return True
            ok, output = phone_control.unlock_with_pin()
            self._on_log(f"PHONE: unlock {'ok' if ok else 'failed'} — {output[:80]}")
            self._speak("Unlock input sent. Check your phone." if ok else "I couldn't unlock the phone — check phone_pin.txt is set up")
            return True

        m = re.search(r"^call (\w+(?:\s\w+)?)$", t.strip())
        if m:
            name = m.group(1).strip()
            contacts = phone_control.load_contacts()
            number = contacts.get(name.lower())
            if not number:
                self._speak(f"I don't have a number saved for {name}. Add them to contacts.json first.")
                return True
            connected, _ = phone_control.is_device_connected()
            if not connected:
                self._speak("I can't see your phone — check it's connected via USB with debugging enabled")
                return True
            ok, output = phone_control.call_number(number)
            self._on_log(f"PHONE: call {name} {'ok' if ok else 'failed'} — {output[:80]}")
            self._speak(f"Calling {name}" if ok else f"I couldn't reach your phone to call {name}")
            return True

        m = re.search(r"open (google|youtube|github|gmail)", t)
        if m:
            site = m.group(1)
            webbrowser.open(LOCAL_SITES[site])
            self._speak(f"Opening {site}")
            return True

        m = re.search(r"\bselect orbit (\w+)", t)
        if m:
            word = m.group(1)
            num = NUMBER_WORDS.get(word)
            if num is None:
                self._speak("Which orbit number would you like?")
            elif self.hologram.select_orbit(num - 1):
                self._on_log(f"DISPLAY: orbit {num} selected")
                phrases = [
                    f"Orbit {num}, got it.",
                    f"Selected orbit {num}.",
                    f"Orbit {num} is now active.",
                    f"You're editing orbit {num} now.",
                ]
                self._speak(random.choice(phrases))
            else:
                self._speak(f"There's no orbit {num} on the current display.")
            return True

        if "deselect" in t and "orbit" in t:
            self.hologram.deselect_orbit()
            self._on_log("DISPLAY: orbit deselected")
            self._speak("Orbit deselected")
            return True

        m = re.search(r"(add|remove)\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)?\s*(proton|neutron|electron)s?", t)
        if m:
            direction, count_word, particle = m.group(1), m.group(2), m.group(3)
            count = int(count_word) if count_word and count_word.isdigit() else NUMBER_WORDS.get(count_word, 1)
            delta = count if direction == "add" else -count
            method = {
                "proton": self.hologram.add_protons,
                "neutron": self.hologram.add_neutrons,
                "electron": self.hologram.add_electrons,
            }[particle]
            actual_delta = method(delta)
            count = abs(actual_delta)
            self._on_log(f"EDIT: {direction} {count} {particle}{'s' if count != 1 else ''} -> {self.hologram.mode_label}")
            verb = "Added" if direction == "add" else "Removed"
            self._speak(f"{verb} {count} {particle}{'s' if count != 1 else ''}. Now {self.hologram.protons} protons, "
                        f"{self.hologram.neutrons} neutrons, {self.hologram.electron_count} electrons.")
            return True

        if "new element" in t or ("start" in t and "element" in t):
            self.hologram.new_element()
            self._on_log("EDIT: started new custom element from scratch")
            self._speak("Starting a new element with one proton and one electron. Tell me what to add.")
            return True

        if "solar system" in t:
            self.hologram.load_solar_system()
            self._on_log("DISPLAY: solar system")
            self._speak("Displaying the solar system")
            return True

        if ("next" in t or "previous" in t or "last" in t) and ("element" in t or "atom" in t):
            if "next" in t:
                symbol, name, z = self.hologram.next_element()
            else:
                symbol, name, z = self.hologram.previous_element()
            self._on_log(f"DISPLAY: {name} atom (element {z}/118)")
            self._speak(f"{name.title()}, symbol {symbol}. Element {z} of 118.")
            return True

        if ("next" in t or "previous" in t or "last" in t) and "system" in t:
            if "next" in t:
                label, count, generated = self.hologram.next_star_system()
            else:
                label, count, generated = self.hologram.previous_star_system()
            self._on_log(f"DISPLAY: {label} system ({count} planets)")
            self._speak(f"The {label} system, {count} planets.")
            return True

        # Named star system, real or not — curated real data for
        # well-known ones, a consistent generated layout for anything
        # else. Checks known names first, then falls back to a generic
        # "[star system/system] called/named/of X" pattern.
        star_hit = next((s for s in STAR_SYSTEMS if s in t or s.replace("-", " ") in t), None)
        if star_hit and any(k in t for k in ("show", "display", "system")):
            label, count, generated = self.hologram.load_star_system(star_hit)
            self._on_log(f"DISPLAY: {label} system ({count} planets)")
            self._speak(f"Displaying the {label} system, {count} planets.")
            return True

        if "star system" in t or re.search(r"\bsystem (?:called|named|of)\b", t):
            m = re.search(r"(?:star system|system)\s+(?:called\s+|named\s+|of\s+)?([a-zA-Z0-9\- ]+?)$", t)
            name = m.group(1).strip() if m and m.group(1).strip() else "unknown"
            label, count, generated = self.hologram.load_star_system(name)
            note = " — I made this one up since I don't have real data for it" if generated else ""
            self._on_log(f"DISPLAY: {label} system ({count} planets){' [generated]' if generated else ''}")
            self._speak(f"Displaying the {label} system, {count} planets.{note}")
            return True

        # Explicit shape name (sphere, cube, torus, pyramid, cylinder)
        shape_hit = next((s for s in SHAPES if s in t), None)
        if not shape_hit:
            alias_hit = next((a for a in SHAPE_ALIASES if a in t), None)
            shape_hit = SHAPE_ALIASES.get(alias_hit)
        if shape_hit and any(k in t for k in ("show", "display", "model", "draw", "load")):
            self.hologram.load_shape(shape_hit)
            self._on_log(f"DISPLAY: {shape_hit} model")
            self._speak(f"Displaying a {shape_hit} model")
            return True

        # Generic "anything round" -> sphere, when no specific shape/atom
        # name was given. This is the catch-all for "show me a globe/ball/
        # planet-looking thing" style requests.
        if any(k in t for k in ("sphere", "globe", "ball", "orb", "round thing")) \
                and any(k in t for k in ("show", "display", "model", "draw", "load")):
            self.hologram.load_shape("sphere")
            self._on_log("DISPLAY: sphere model")
            self._speak("Displaying a sphere")
            return True

        # Match an element name anywhere in the phrase, as long as some
        # display-intent keyword is also present. This is deliberately
        # forgiving because speech recognition often clips trailing words
        # like "atom" — "show me a carbon" should still work, not just
        # the perfectly-transcribed "show me a carbon atom".
        element_hit = next((name for name in sorted(ELEMENTS, key=len, reverse=True)
                            if re.search(r"\b" + re.escape(name) + r"\b", t)), None)
        if element_hit and re.search(r"\b(show|display|draw|load|model)\b", t):
            result = self.hologram.load_atom(element_hit)
            if result:
                symbol, name = result
                self._on_log(f"DISPLAY: {name} atom")
                self._speak(f"Displaying a {name} atom, symbol {symbol}")
            else:
                self._speak(f"I don't have a model for {element_hit}, but I can show common elements up to gold.")
            return True

        if "reset" in t and ("display" in t or "hologram" in t or "diagram" in t):
            self.hologram.show_core()
            self._on_log("DISPLAY: reset to voice core")
            self._speak("Resetting the display")
            return True

        return False

    # ---- structured weather (independent of Groq) ----------------------------

    def _handle_weather(self, command):
        if re.search(r"\b(?:hide|close|dismiss)\b", command):
            self.hologram.hide_weather()
            self._speak("Closing the weather display")
            return True
        if re.search(r"\b(?:tomorrow|forecast|next week|yesterday|tonight)\b", command):
            message = "I can show current weather, not forecasts yet. Ask for weather in a city right now."
            self.hologram.set_weather_status("error", message)
            self._speak(message)
            return True
        self.state = "thinking"
        self.hologram.set_weather_status("loading", "Resolving location and current conditions")
        try:
            data = fetch_current_weather(extract_location(command))
        except WeatherError as exc:
            self.last_weather_error = exc.code
            self._on_log(f"WEATHER: {exc.code}: {exc}")
            self.hologram.set_weather_status("error", str(exc))
            self._speak(str(exc))
        else:
            self.last_weather_error = None
            self.hologram.show_weather(data)
            self._on_log(f"WEATHER: {data['location']} / {data['updated_at']}")
            self._speak(f"It's {round(data['temp_c'])} degrees Celsius and {data['description']} in {data['location']}.")
        return True

    # ---- Groq brain ---------------------------------------------------------

    NEEDS_SEARCH_KEYWORDS = (
        "latest", "current", "today", "right now", "this week", "recent",
        "news", "score", "who won", "stock price", "happening now",
    )

    def _needs_search(self, text):
        t = text.lower()
        return any(k in t for k in self.NEEDS_SEARCH_KEYWORDS)

    def _ask_groq(self, text):
        if not self.client:
            return "General conversation needs a Groq API key. Weather and local display commands are still available."
        self.history.append({"role": "user", "content": text})
        use_search = self._needs_search(text)
        reply = self._call_groq_safe(use_search)

        # Store a trimmed copy in history (not the full reply) so
        # search-augmented answers don't balloon future request sizes.
        trimmed = reply if len(reply) < 400 else reply[:400] + "..."
        self.history.append({"role": "assistant", "content": trimmed})
        if len(self.history) > 12:
            self.history = self.history[-12:]
        return reply

    def _call_groq_safe(self, use_search=False):
        try:
            return self._call_groq(self.history, use_search)
        except Exception as e:
            msg = str(e)
            if "413" in msg or "too large" in msg.lower() or "request_too_large" in msg.lower():
                self._on_log("VOICE: request too large, clearing conversation memory and retrying")
                last_user = self.history[-1]
                self.history = [last_user]
                try:
                    return self._call_groq(self.history, use_search)
                except Exception as e2:
                    return f"Sorry, still hit an error after clearing memory: {e2}"
            return f"Sorry, I hit an error talking to the API: {e}"

    def _call_groq(self, messages, use_search=False):
        model = MODEL_SEARCH if use_search else MODEL_FAST
        # compound needs headroom for its internal search step before it
        # can write the final answer; the fast model doesn't need as much
        max_tokens = 800 if use_search else 300
        response = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        )
        if not response.choices:
            return "I didn't get a response back from the API."
        content = response.choices[0].message.content
        if content is None:
            return "I searched for that but didn't get a final answer back — try asking again."
        reply = content.strip()
        return reply or "I didn't get a text response back."

    # ---- main listen loop -----------------------------------------------------

    MIN_ENERGY_THRESHOLD = 50
    MAX_ENERGY_THRESHOLD = 600  # calibration can never require shouting past this

    def _load_energy_override(self):
        path = os.path.join(str(PROJECT_ROOT), "energy_threshold.txt")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    val = float(f.read().strip())
                    if val > 0:
                        return val
            except Exception:
                pass
        return None

    def _clamp_energy_threshold(self):
        clamped = max(self.MIN_ENERGY_THRESHOLD, min(self.MAX_ENERGY_THRESHOLD, self.recognizer.energy_threshold))
        if clamped != self.recognizer.energy_threshold:
            self._on_log(f"VOICE: clamping energy_threshold {self.recognizer.energy_threshold:.0f} -> {clamped:.0f}")
        self.recognizer.energy_threshold = clamped

    def _run_loop(self):
        # dynamic_energy_threshold sounds good in theory (auto-adapt to
        # ambient noise) but on a laptop with the mic and speakers close
        # together, it also "learns" Aurora's OWN voice through the
        # speakers as loud ambient noise and keeps ratcheting the
        # threshold up in response — that's very likely why it took
        # shouting to register. Using a fixed, clamped threshold instead
        # (re-measured periodically, but never runaway) is far more
        # predictable.
        self.recognizer.dynamic_energy_threshold = False
        self.recognizer.pause_threshold = 0.9
        self.recognizer.non_speaking_duration = 0.4
        self.recognizer.phrase_threshold = 0.3

        override = self._load_energy_override()
        if override is not None:
            self.recognizer.energy_threshold = override
            self._on_log(f"VOICE: using energy_threshold.txt override -> {override:.0f}")
        else:
            try:
                with self._mic_lock, self.microphone as source:
                    self._on_log("VOICE: calibrating for ambient noise (2 sec)...")
                    self.recognizer.adjust_for_ambient_noise(source, duration=2)
                self._clamp_energy_threshold()
                self._on_log(f"VOICE: calibration done (energy_threshold={self.recognizer.energy_threshold:.0f}), now listening")
            except Exception as e:
                self._on_log(f"VOICE: could not calibrate microphone ({e})")
                self.enabled = False
                self._running = False
                return

        loop_count = 0
        last_recalibration = time.time()
        while self._running:
            try:
                notice = self._speech_queue.get_nowait()
            except queue.Empty:
                notice = None
            if notice:
                self._speak(notice)
                continue
            loop_count += 1

            # Ambient noise drifts over a long-running session (AC turning
            # on, other people talking, etc.) — a one-time calibration at
            # startup goes stale. Recalibrate periodically in the
            # background, but only if there's no manual override, and
            # always clamped so it can never drift up to shouting levels.
            if override is None and time.time() - last_recalibration > 120:
                try:
                    with self._mic_lock, self.microphone as source:
                        self.recognizer.adjust_for_ambient_noise(source, duration=1)
                    self._clamp_energy_threshold()
                    self._on_log(f"VOICE: recalibrated (energy_threshold={self.recognizer.energy_threshold:.0f})")
                except Exception:
                    pass
                last_recalibration = time.time()

            try:
                with self._mic_lock, self.microphone as source:
                    audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=8)
                text = self.recognizer.recognize_google(audio)
                self._on_log(f"VOICE: picked up audio -> '{text}'")
            except sr.WaitTimeoutError:
                continue
            except sr.UnknownValueError:
                if loop_count % 5 == 0:
                    self._on_log("VOICE: heard audio but couldn't transcribe it")
                continue
            except Exception as e:
                self._on_log(f"VOICE ERROR: {e}")
                time.sleep(1)
                continue

            lowered = text.lower()
            found, command = find_wake_word(lowered)
            if not found:
                continue
            command = command.strip(" ,.")

            if not command:
                # A wake word on its own opens a real follow-up listening window.
                self.state = "listening"
                self._on_log("VOICE: listening for your request")
                try:
                    with self._mic_lock, self.microphone as source:
                        audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                    command = self.recognizer.recognize_google(audio).lower().strip(" ,.")
                    repeated_wake, remainder = find_wake_word(command)
                    if repeated_wake:
                        command = remainder.strip(" ,.")
                except Exception as exc:
                    self._on_log(f"VOICE: follow-up ended ({type(exc).__name__})")
                    command = ""
                if not command:
                    self.state = "idle"
                    continue

            self.state = "listening"
            self.last_heard = command
            self._on_log(f"HEARD: {command}")

            try:
                if self._handle_local_command(command):
                    self.state = "idle"
                    continue

                self.state = "thinking"
                reply = self._ask_groq(command)
                self.hologram.show_info_card(command, reply)
                self._speak(reply)
            except Exception as e:
                import traceback
                self._on_log(f"VOICE: command handling crashed ({type(e).__name__}: {e})")
                traceback.print_exc()
                self.state = "idle"
                # keep the loop alive no matter what went wrong above —
                # losing one command is much better than the whole voice
                # thread silently dying
