"""
Background voice pipeline: wake word -> speech-to-text -> Groq API brain
(groq/compound, built-in web search) -> natural neural speech reply.
Also handles "show me a diagram of X" and "select orbit N" style commands
by driving the hologram directly (no API call needed, so it's instant).

STREAMING: general Q&A replies stream token-by-token from Groq and are
spoken sentence-by-sentence as they arrive, instead of waiting for the
whole answer to finish generating before saying a word. True word-by-word
audio isn't used on purpose — TTS on single words sounds choppy — sentence
chunks give the same "starts talking almost immediately" win with natural
speech. "Aurora, stop" cancels the in-flight generation and clears any
sentences still queued to be spoken, not just the one playing.

PHONE (needs ADB setup — see phone_control.py):
  "Aurora, call mom on WhatsApp" / "video call Sam on WhatsApp"
  "Aurora, open Instagram on my phone"
  "Aurora, connect to my phone over wifi"
  "Aurora, connect my phone to home wifi" / "turn off wifi on my phone"
  "Aurora, search my phone for contact John" / "find resume on my phone"
  "Aurora, google best pizza on my phone"

COMPUTER: "system status", "take a screenshot", "lock my computer"

VISION: "Aurora, what am I looking at?" — decodes any QR code/barcode in
frame locally (instant, no API call); if none found, sends the current
camera frame to Groq's vision model for a short spoken description.

WEATHER: uses Open-Meteo (https://open-meteo.com) — a free weather API
that needs no API key. Two calls: their geocoding endpoint turns a place
name into latitude/longitude, then the forecast endpoint returns current
conditions for that point. If no location is given ("what's the weather
right now"), IP-based geolocation (ipapi.co, also free/keyless) is used
to guess where you are. This replaced asking the Groq model to search
the web and format weather into a strict text line, which was slower,
occasionally hit rate limits, and was one more thing that could return
malformed output.

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

Weather needs no separate setup — Open-Meteo and ipapi.co are both
free and keyless.
"""

import asyncio
import ast
import difflib
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
from jarvis_ui import system_control
from jarvis_ui import phone_control
from jarvis_ui import code_control
from jarvis_ui import vision
from jarvis_ui import telemetry

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
API_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "api_key.txt")
NOTES_FILE = os.path.join(os.path.dirname(__file__), "..", "notes.txt")

# groq/compound does live web search internally, but that reasoning step
# adds real latency even for questions that don't need it — a plain "hi"
# was going through the same search-capable pipeline as "what's the
# weather", which is why replies felt slow. Fast model for normal chat,
# compound reserved for the one place that actually needs search.
# (Weather no longer uses this at all — see WEATHER section below.)
MODEL_FAST = "groq/compound-mini"
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
    """Evaluates only basic arithmetic — no function calls, no names, no
    attribute access — so this is safe to run on raw speech text, unlike
    a bare eval()."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval_node(node.left), _safe_eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval_node(node.operand))
    raise ValueError("unsupported expression")


def try_calculate(text):
    """Returns (result, display_expression) or None if the text doesn't
    look like a calculable expression."""
    t = text.lower()

    m = re.search(r"([\d.]+)\s*percent of\s*([\d.]+)", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return (a / 100 * b), f"{a}% of {b}"

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
        self.state = "idle"       # idle | listening | speaking
        self.last_heard = ""
        self.last_reply = ""
        self.enabled = False

        external_log = on_log or (lambda text: None)
        log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "voice_debug.log")

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
        self.active_timers = []  # list of dicts: label, ends_at (time.time())
        self.last_weather_error = None
        self.pending_enrollment_name = None  # set by voice, consumed by main.py's camera loop
        self.latest_frame = None  # set every frame by main.py's camera loop, used by vision commands
        self.telemetry = telemetry.TelemetryReader(on_log=self._on_log)  # Arduino/IoT Mode

        # streaming reply state: the in-flight sentence queue (so a stop
        # command can drain it) and a cancel flag checked between tokens
        # and before each queued sentence is spoken.
        self._active_tts_queue = None
        self._stream_cancel = threading.Event()

        self.api_key = load_api_key()
        if Groq is None:
            self._on_log("VOICE: 'groq' package not installed — voice AI disabled")
            self.client = None
        elif not self.api_key:
            self._on_log("VOICE: no API key found (api_key.txt or GROQ_API_KEY) — voice AI disabled")
            self.client = None
        else:
            self.client = Groq(api_key=self.api_key)

        try:
            self.recognizer = sr.Recognizer()
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
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mic_index.txt")
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
        if not self.client or not self.microphone:
            return
        self.enabled = True
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._on_log("VOICE: listening for wake word 'Aurora'")

    def stop(self):
        self._running = False

    # ---- speech output -----------------------------------------------------

    def speak_now(self, text):
        """Public entry point for other threads (main.py's camera loop)
        to trigger speech — e.g. greeting someone the moment face
        recognition identifies them. Safe to call cross-thread the same
        way the mid-speech 'stop' watcher already does."""
        self._speak(text)

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
            asyncio.run(_edge_tts_save(text, path))
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()

            # The main listening loop isn't touching the mic right now
            # (it's blocked here, waiting on us), so it's safe to use it
            # for a short-lived "did they say stop?" listener during
            # playback — this is what lets "Aurora, stop" actually
            # interrupt mid-sentence instead of only working between turns.
            stop_watcher = threading.Thread(target=self._watch_for_stop, daemon=True)
            stop_watcher.start()

            while pygame.mixer.music.get_busy():
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

    def _interrupt_all_speech(self):
        """Stops whatever's playing right now AND cancels an in-flight
        streamed reply, draining any sentences still queued to be spoken.
        Without the drain, 'stop' would only cut the current sentence and
        the rest of the answer would keep talking."""
        self._stream_cancel.set()
        q = self._active_tts_queue
        if q is not None:
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        pygame.mixer.music.stop()

    def _watch_for_stop(self):
        """Runs only while audio is playing. Listens for a short phrase and
        interrupts playback immediately if it hears 'stop'."""
        try:
            while pygame.mixer.music.get_busy():
                with self.microphone as source:
                    audio = self.recognizer.listen(source, timeout=1.5, phrase_time_limit=2)
                text = self.recognizer.recognize_google(audio).lower()
                if "stop" in text or "aurora" in text:
                    self._on_log("VOICE: stop heard, interrupting speech")
                    self._interrupt_all_speech()
                    return
        except Exception:
            pass  # timeouts / no speech / mic hiccups are all fine here

    # ---- timers -----------------------------------------------------------

    def _start_timer(self, seconds, label):
        ends_at = time.time() + seconds
        self.active_timers.append({"label": label, "ends_at": ends_at})

        def run():
            time.sleep(seconds)
            self.active_timers[:] = [t for t in self.active_timers if t["ends_at"] != ends_at]
            self._on_log(f"TIMER: {label} finished")
            self._speak(f"{label} is up!" if label else "Timer's up!")

        threading.Thread(target=run, daemon=True).start()
        self._on_log(f"VOICE: timer started — {label} ({seconds}s)")

    # ---- streaming Groq replies: speak sentence-by-sentence as tokens arrive ---

    _SENTENCE_END = re.compile(r"[.!?]\s|\n")

    def _stream_and_speak(self, messages, use_search, question_for_card):
        """Streams a reply from Groq. As soon as a sentence boundary shows
        up in the accumulating text, that sentence is handed to a
        background worker thread that speaks it — so speech starts after
        the first sentence, not after the whole reply. Also keeps the
        on-screen info card updated live as text streams in. Returns the
        full reply text; raises on API errors (caller retries/reports)."""
        model = MODEL_SEARCH if use_search else MODEL_FAST
        max_tokens = 800 if use_search else 300

        self._stream_cancel.clear()
        tts_queue = queue.Queue()
        self._active_tts_queue = tts_queue

        def speak_worker():
            while True:
                chunk = tts_queue.get()
                if chunk is None:
                    return
                if not self._stream_cancel.is_set():
                    self._speak(chunk)

        worker = threading.Thread(target=speak_worker, daemon=True)
        worker.start()

        full_reply = []
        buffer = ""
        try:
            stream = self.client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                stream=True,
            )
            for event in stream:
                if self._stream_cancel.is_set():
                    break
                if not event.choices:
                    continue
                delta = event.choices[0].delta.content
                if not delta:
                    continue
                buffer += delta
                full_reply.append(delta)
                self.hologram.show_info_card(question_for_card, "".join(full_reply))

                while True:
                    m = self._SENTENCE_END.search(buffer)
                    if not m:
                        break
                    cut = m.end()
                    sentence, buffer = buffer[:cut].strip(), buffer[cut:]
                    if sentence:
                        tts_queue.put(sentence)
        finally:
            if buffer.strip() and not self._stream_cancel.is_set():
                tts_queue.put(buffer.strip())
            tts_queue.put(None)
            worker.join(timeout=15)
            self._active_tts_queue = None

        reply = "".join(full_reply).strip()
        return reply or "I didn't get a text response back."

    def _ask_groq_stream(self, text):
        """Streaming counterpart to _ask_groq: same history bookkeeping
        and the same request-too-large retry, but speaks as it goes
        instead of waiting for the full reply."""
        self.history.append({"role": "user", "content": text})
        use_search = self._needs_search(text)
        already_spoken = False
        try:
            reply = self._stream_and_speak(self.history, use_search, text)
            already_spoken = True
        except Exception as e:
            msg = str(e)
            if "413" in msg or "too large" in msg.lower() or "request_too_large" in msg.lower():
                self._on_log("VOICE: request too large, clearing conversation memory and retrying")
                last_user = self.history[-1]
                self.history = [last_user]
                try:
                    reply = self._stream_and_speak(self.history, use_search, text)
                    already_spoken = True
                except Exception as e2:
                    reply = f"Sorry, still hit an error after clearing memory: {e2}"
            else:
                reply = f"Sorry, I hit an error talking to the API: {e}"

        if not already_spoken:
            self._speak(reply)

        trimmed = reply if len(reply) < 400 else reply[:400] + "..."
        self.history.append({"role": "assistant", "content": trimmed})
        if len(self.history) > 12:
            self.history = self.history[-12:]
        return reply

    # ---- phone: WhatsApp calls, apps, wifi, search (see phone_control.py) ----

    def _phone_ready(self):
        connected, _ = phone_control.is_device_connected()
        if not connected:
            self._speak("I can't see your phone. Plug it in with USB debugging on, "
                        "or say 'connect to my phone over wifi'.")
        return connected

    def _handle_phone_command(self, t):
        t = re.sub(r"wi[\s-]fi", "wifi", t)
        t = re.sub(r"what'?s\s?app", "whatsapp", t)

        # WhatsApp call: "call mom on whatsapp", "video call sam on whatsapp", "whatsapp call mom"
        m = re.search(r"(video )?call (.+?) (?:on|via|using|through|in) whatsapp", t) or \
            re.search(r"whatsapp (video )?call (?:to )?(.+)$", t)
        if m:
            video, target = bool(m.group(1)), re.sub(r"^my ", "", m.group(2).strip())
            if not self._phone_ready():
                return True
            ok, info = phone_control.whatsapp_call(target, video)
            self._on_log(f"PHONE: whatsapp {'video ' if video else ''}call {target} {'ok' if ok else 'failed'}")
            self._speak(f"Starting a WhatsApp {'video ' if video else ''}call with {info}." if ok else info)
            return True

        # Wireless ADB: "connect to my phone over wifi"
        if re.search(r"connect (?:to )?(?:my )?phone (?:over|via|through|on) wifi|"
                     r"connect (?:to )?(?:my )?phone wirelessly|go wireless", t):
            self._speak("Connecting to your phone over wifi.")
            ok, info = phone_control.connect_wireless()
            self._on_log(f"PHONE: wireless connect {'ok' if ok else 'failed'} — {info}")
            self._speak("Connected wirelessly. You can unplug the cable now." if ok else info)
            return True

        # Phone joins a saved network: "connect my phone to home wifi"
        m = re.search(r"connect (?:my )?phone to (?:the )?wifi (?:network )?(?:called |named )?(.+)$", t) or \
            re.search(r"connect (?:my )?phone to (?:the )?(.+?) wifi", t)
        if m:
            if not self._phone_ready():
                return True
            ok, msg = phone_control.wifi_connect(m.group(1).strip())
            self._on_log(f"PHONE: wifi connect {m.group(1).strip()} {'ok' if ok else 'failed'}")
            self._speak(msg)
            return True

        # Phone wifi on/off
        m = re.search(r"(?:turn|switch) (on|off) (?:the )?wifi (?:on|in) (?:my )?phone|"
                      r"(?:turn|switch) (on|off) (?:my )?phone(?:'s)? wifi", t)
        if m:
            if not self._phone_ready():
                return True
            on = next(g for g in m.groups() if g) == "on"
            ok = phone_control.wifi_set(on)
            self._on_log(f"PHONE: wifi {'on' if on else 'off'} {'ok' if ok else 'failed'}")
            self._speak(f"Phone wifi turned {'on' if on else 'off'}." if ok else "I couldn't change the phone's wifi.")
            return True

        # Open an app on the phone: "open instagram on my phone"
        m = re.search(r"(?:open|launch|start) (.+?) (?:on|in) (?:my |the )?(?:phone|mobile)", t)
        if m:
            name = m.group(1).strip()
            if not self._phone_ready():
                return True
            ok = phone_control.open_app(name)
            self._on_log(f"PHONE: open {name} {'ok' if ok else 'failed'}")
            self._speak(f"Opening {name} on your phone." if ok else f"I couldn't find an app called {name} on your phone.")
            return True

        # Google search on the phone: "google best pizza on my phone"
        m = re.search(r"(?:google|search google for|search the web for) (.+?) (?:on|in) (?:my |the )?(?:phone|mobile)", t)
        if m:
            query = m.group(1).strip()
            if not self._phone_ready():
                return True
            ok = phone_control.web_search(query)
            self._on_log(f"PHONE: web search '{query}' {'ok' if ok else 'failed'}")
            self._speak(f"Searching for {query} on your phone." if ok else "I couldn't start that search on your phone.")
            return True

        # Search the phone: "search my phone for contact john", "find resume on my phone"
        m = re.search(r"(?:search|find|look)\s+(?:on\s+)?(?:my\s+|the\s+)?(?:phone|mobile)\s+for\s+(.+)$", t) or \
            re.search(r"(?:search for|find|look for)\s+(.+?)\s+(?:on|in)\s+(?:my\s+|the\s+)?(?:phone|mobile)", t)
        if m:
            query, kind = m.group(1).strip(), None
            m2 = re.match(r"(contact|number|app|application|file)s?\s+(?:called\s+|named\s+|for\s+)?(.+)$", query)
            if m2:
                kind = {"contact": "contacts", "number": "contacts", "app": "apps",
                        "application": "apps", "file": "files"}[m2.group(1)]
                query = m2.group(2).strip()
            if not self._phone_ready():
                return True
            self._speak(f"Searching your phone for {query}.")
            res = phone_control.search_phone(query, kind)
            contacts, apps, files = res["contacts"], res["apps"], res["files"]
            if not (contacts or apps or files):
                self._speak(f"I didn't find anything for {query} on your phone.")
                return True
            card = []
            if contacts:
                card.append("Contacts: " + ", ".join(f"{n} {num}" for n, num in contacts[:4]))
            if apps:
                card.append("Apps: " + ", ".join(apps[:4]))
            if files:
                card.append("Files: " + ", ".join(os.path.basename(f) for f in files[:5]))
            self.hologram.show_info_card(f"Search phone: {query}", " | ".join(card))
            bits = [f"{len(lst)} {word}{'' if len(lst) == 1 else 's'}"
                    for lst, word in ((contacts, "contact"), (apps, "app"), (files, "file")) if lst]
            spoken = "I found " + ", ".join(bits) + "."
            if contacts:
                spoken += f" Top contact: {contacts[0][0]}."
            elif files:
                spoken += f" Top file: {os.path.basename(files[0])}."
            self._on_log(f"PHONE: search '{query}' -> {', '.join(bits)}")
            self._speak(spoken)
            return True

        return False

    # ---- local (no-API) commands, including hologram diagrams -------------

    def _handle_local_command(self, text):
        t = text.lower()

        if t.strip() in ("stop", "stop it", "be quiet", "silence"):
            self._interrupt_all_speech()
            self._on_log("VOICE: stop command")
            return True

        if t.strip() in ("repeat that", "say that again", "what did you say", "can you repeat that", "repeat"):
            if self.last_reply:
                self._speak(self.last_reply)
            else:
                self._speak("I haven't said anything yet.")
            return True

        m = re.search(r"take a note[:\s]+(.+)$", t)
        if m:
            note_text = text[m.start(1):m.end(1)].strip()
            if not note_text:
                self._speak("What would you like me to note down?")
                return True
            try:
                with open(NOTES_FILE, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M')}] {note_text}\n")
                self._on_log(f"NOTES: added '{note_text[:60]}'")
                self._speak("Noted.")
            except Exception as e:
                self._on_log(f"NOTES: failed to save ({e})")
                self._speak("Sorry, I couldn't save that note.")
            return True

        if any(k in t for k in ("read my notes", "read notes", "what are my notes", "list my notes")):
            if not os.path.exists(NOTES_FILE):
                self._speak("You don't have any notes yet.")
                return True
            try:
                with open(NOTES_FILE, "r", encoding="utf-8") as f:
                    lines = [l.strip() for l in f.readlines() if l.strip()]
            except Exception:
                lines = []
            if not lines:
                self._speak("You don't have any notes yet.")
            else:
                recent = lines[-5:]
                spoken = "; ".join(re.sub(r"^\[.*?\]\s*", "", l) for l in recent)
                self._speak(f"Here are your last {len(recent)} notes: {spoken}")
            return True

        if "clear my notes" in t or ("delete" in t and "notes" in t):
            try:
                open(NOTES_FILE, "w").close()
                self._on_log("NOTES: cleared")
                self._speak("Cleared your notes.")
            except Exception:
                self._speak("I couldn't clear your notes.")
            return True

        # ---- vision: QR/barcode + scene description ("what am I looking at") ----
        if any(k in t for k in ("what am i looking at", "what is this", "what's this",
                                 "scan this", "read this qr", "read this code",
                                 "read this barcode", "what do you see", "what's in front of me",
                                 "describe what you see", "describe the camera")):
            frame = self.latest_frame
            if frame is None:
                self._speak("I don't have a camera frame right now.")
                return True
            try:
                codes = vision.read_codes(frame)
            except Exception as e:
                self._on_log(f"VISION: code scan failed ({e})")
                codes = []
            if codes:
                self._on_log(f"VISION: decoded {codes}")
                self.hologram.show_info_card("What am I looking at?", "Code found: " + "; ".join(codes))
                self._speak("I found a code: " + "; ".join(codes))
                return True
            if not self.client:
                self._speak("I need the Groq API connected to describe what I'm looking at.")
                return True
            self._speak("Let me take a look.")
            try:
                description = vision.describe_scene(self.client, frame)
                self.hologram.show_info_card("What am I looking at?", description)
                self._on_log(f"VISION: {description}")
                self._speak(description)
            except Exception as e:
                self._on_log(f"VISION: describe_scene failed ({e})")
                self._speak("Sorry, I hit an error looking at the camera.")
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

        if any(k in t for k in ("check face recognition", "is face recognition working", "face recognition status", "diagnose face")):
            if not self.face_id.detection_available:
                self._speak("Face detection isn't available at all — the cascade classifier failed to load. "
                            "That's almost always an OpenCV install conflict: uninstall opencv-python, "
                            "opencv-python-headless, and opencv-contrib-python, then reinstall just "
                            "opencv-contrib-python.")
            elif not self.face_id.available:
                self._speak("Face detection works, but recognition doesn't — opencv-contrib-python's face "
                            "module isn't loading, so I can detect a face but can't match it to a name. "
                            "Reinstall opencv-contrib-python cleanly to fix it.")
            else:
                count = len(self.face_id.people)
                if count:
                    self._speak(f"Face recognition is working. {count} {'person is' if count == 1 else 'people are'} enrolled: "
                                + ", ".join(self.face_id.people.keys()))
                else:
                    self._speak("Face recognition is working, but nobody's enrolled yet. "
                                "Say 'remember my face as' followed by your name.")
            return True

        if "what time" in t or "current time" in t:
            self._speak(time.strftime("It's %I:%M %p"))
            return True

        # ---- computer utilities: status, screenshot, lock -------------------------
        if any(k in t for k in ("system status", "system stats", "battery", "how's my computer", "how is my computer")):
            status = system_control.get_system_status()
            self._on_log("SYSTEM: status readout")
            self._speak(status or "System stats aren't available. Install psutil.")
            return True

        if "screenshot" in t or "screen shot" in t:
            ok = system_control.take_screenshot()
            self._on_log(f"SYSTEM: screenshot {'ok' if ok else 'failed'}")
            self._speak("Screenshot saved to your Pictures folder." if ok else "I couldn't take a screenshot.")
            return True

        # \block\b so "unlock my phone" doesn't lock the PC
        if re.search(r"\block\b", t) and any(k in t for k in ("computer", "pc", "laptop", "screen")):
            self._speak("Locking your computer.")
            self._on_log("SYSTEM: locking workstation")
            system_control.lock_workstation()
            return True

        # ---- timers ---------------------------------------------------------
        m = re.search(r"(?:set a |set )?timer for (\d+)\s*(second|minute|hour)s?(?:\s+(?:for|called|named)\s+(.+))?", t)
        if m:
            amount, unit, label = int(m.group(1)), m.group(2), m.group(3)
            seconds = amount * {"second": 1, "minute": 60, "hour": 3600}[unit]
            display_label = label.strip() if label else f"{amount} {unit}{'s' if amount != 1 else ''} timer"
            self._start_timer(seconds, display_label)
            self._speak(f"Timer set for {amount} {unit}{'s' if amount != 1 else ''}")
            return True

        if "cancel" in t and "timer" in t:
            count = len(self.active_timers)
            self.active_timers.clear()
            self._on_log(f"VOICE: cancelled {count} timer(s)")
            self._speak(f"Cancelled {count} timer{'s' if count != 1 else ''}" if count else "No timers running")
            return True

        if "how much time" in t or ("timer" in t and any(k in t for k in ("left", "remaining"))):
            if not self.active_timers:
                self._speak("No timers running")
            else:
                parts = []
                for timer in self.active_timers:
                    remaining = max(0, round(timer["ends_at"] - time.time()))
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

        # ---- code editing (VS Code / Arduino IDE) --------------------------------
        m = re.search(r"write (?:(python|javascript|js|cpp|c\+\+|c|html|arduino) )?code called (.+?) that (.+)$", t)
        if m:
            language, name, description = (m.group(1) or "python"), m.group(2).strip(), m.group(3).strip()
            if not self.client:
                self._speak("I need the Groq API connected to generate code.")
                return True
            self._speak(f"Writing {name} now, one moment.")
            try:
                path = code_control.get_or_make_path(name, language)
                code_text = code_control.generate_code(self.client, description, language)
                code_control.write_file_content(path, code_text)
                opener = code_control.open_in_arduino if path.endswith(".ino") else code_control.open_in_vscode
                ok, err = opener(path)
                self._on_log(f"CODE: generated '{name}' ({language}) -> {path}")
                self._speak(f"Done — I wrote {name} and opened it" if ok
                            else f"I wrote {name}, but couldn't open the editor — {err[:80] if err else ''}")
            except Exception as e:
                self._on_log(f"CODE: generation failed ({e})")
                self._speak("Sorry, I hit an error generating that code.")
            return True

        m = re.search(r"edit (?:code |the file |the sketch |the project )?(.+?) to (.+)$", t)
        if m:
            name, instruction = m.group(1).strip(), m.group(2).strip()
            if not self.client:
                self._speak("I need the Groq API connected to edit code.")
                return True
            path = code_control.resolve_project_path(name) or code_control.find_arduino_sketch(name)
            if not path:
                self._speak(f"I couldn't find a file called {name}. Say 'write code called {name} that ...' to create it first.")
                return True
            self._speak(f"Updating {name} now, one moment.")
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    current = f.read()
                updated = code_control.edit_code(self.client, current, instruction)
                diff_summary = code_control.summarize_diff(current, updated)
                code_control.write_file_content(path, updated)
                opener = code_control.open_in_arduino if path.endswith(".ino") else code_control.open_in_vscode
                ok, err = opener(path)
                self._on_log(f"CODE: edited '{name}' ({diff_summary}) -> {path}")
                self._speak(f"Updated {name}: {diff_summary}. Opened it" if ok
                            else f"Updated {name} ({diff_summary}), but couldn't open the editor — {err[:80] if err else ''}")
            except Exception as e:
                self._on_log(f"CODE: edit failed ({e})")
                self._speak("Sorry, I hit an error editing that file.")
            return True

        m = re.search(r"(?:new|create) arduino sketch (?:called |named )?(.+)", t)
        if m:
            name = m.group(1).strip()
            path = code_control.create_arduino_sketch(name)
            ok, err = code_control.open_in_arduino(path)
            self._on_log(f"CODE: created arduino sketch '{name}' -> {path}")
            self._speak(f"Created a new Arduino sketch called {name} and opened it" if ok
                        else f"Created the sketch, but I couldn't open the Arduino IDE — {err[:80] if err else ''}")
            return True

        m = re.search(r"(?:new|create) (python|javascript|js|cpp|c\+\+|c|html)?\s*(?:script|file)\s+(?:called |named )?(.+)", t)
        if m:
            language = (m.group(1) or "python").strip()
            name = m.group(2).strip()
            path = code_control.create_code_file(name, language)
            ok, err = code_control.open_in_vscode(path)
            self._on_log(f"CODE: created {language} file '{name}' -> {path}")
            self._speak(f"Created {name} and opened it in VS Code" if ok
                        else f"Created the file, but I couldn't open VS Code — {err[:80] if err else ''}")
            return True

        m = re.search(r"open (.+?) in (?:vs code|vscode|visual studio code)", t)
        if m:
            name = m.group(1).strip()
            path = code_control.resolve_project_path(name)
            if not path:
                self._speak(f"I don't have a project called {name} saved. Add it to code_projects.json, "
                            f"or say 'new script called {name}' to create one.")
                return True
            ok, err = code_control.open_in_vscode(path)
            self._on_log(f"CODE: open '{name}' in VS Code {'ok' if ok else 'failed'}")
            self._speak(f"Opening {name} in VS Code" if ok else f"I couldn't open that in VS Code — {err[:80] if err else ''}")
            return True

        m = re.search(r"open (.+?) in arduino", t)
        if m:
            name = m.group(1).strip()
            path = code_control.find_arduino_sketch(name)
            if not path:
                self._speak(f"I don't have a sketch called {name}. Say 'new arduino sketch called {name}' to create one.")
                return True
            ok, err = code_control.open_in_arduino(path)
            self._on_log(f"CODE: open sketch '{name}' {'ok' if ok else 'failed'}")
            self._speak(f"Opening {name} in the Arduino IDE" if ok else f"I couldn't open the Arduino IDE — {err[:80] if err else ''}")
            return True

        # ---- phone: WhatsApp / apps / wifi / search (must come BEFORE PC app
        # launching, or "open chrome on my phone" would open Chrome on the PC) ----
        if self._handle_phone_command(t):
            return True

        # ---- app launching ------------------------------------------------------
        app_hit = next((a for a in system_control.APP_COMMANDS if a in t), None)
        if app_hit and any(k in t for k in ("open", "launch", "start")):
            ok = system_control.launch_app(app_hit)
            self._on_log(f"APP: {'launched' if ok else 'failed to launch'} {app_hit}")
            self._speak(f"Opening {app_hit}" if ok else f"I couldn't open {app_hit}")
            return True

        # ---- volume ---------------------------------------------------------
        if "volume" in t:
            m = re.search(r"volume to (\d+)", t)
            if m:
                pct = int(m.group(1))
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
            system_control.media_play_pause()
            self._on_log("MEDIA: play/pause")
            self._speak("Done")
            return True
        if any(k in t for k in ("next song", "next track", "skip song", "skip track")):
            system_control.media_next()
            self._on_log("MEDIA: next")
            self._speak("Skipping")
            return True
        if any(k in t for k in ("previous song", "previous track", "last song", "go back a song")):
            system_control.media_previous()
            self._on_log("MEDIA: previous")
            self._speak("Going back")
            return True

        # ---- phone (requires ADB setup — see phone_control.py) ------------------
        if re.search(r"\b(call me|give me a call|call my phone|ring me)\b", t):
            number = phone_control.load_my_number()
            if not number:
                self._speak("I don't have your number saved. Create my_number.txt next to api_key.txt with your phone number.")
                return True
            connected, _ = phone_control.is_device_connected()
            if not connected:
                self._speak("I can't see your phone — check it's connected via USB with debugging enabled")
                return True
            ok, output = phone_control.call_number(number)
            self._on_log(f"PHONE: call me {'ok' if ok else 'failed'} — {output[:80]}")
            self._speak("Calling you now" if ok else "I couldn't reach your phone to call you")
            return True

        if "unlock" in t and "phone" in t:
            connected, _ = phone_control.is_device_connected()
            if not connected:
                self._speak("I can't see your phone — check it's connected via USB with debugging enabled")
                return True
            ok, output = phone_control.unlock_with_pin()
            self._on_log(f"PHONE: unlock {'ok' if ok else 'failed'} — {output[:80]}")
            self._speak("Phone unlocked" if ok else "I couldn't unlock the phone — check phone_pin.txt is set up")
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

        m = re.search(r"select orbit (\w+)", t)
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
            count = NUMBER_WORDS.get(count_word, 1) if count_word else 1
            delta = count if direction == "add" else -count
            method = {
                "proton": self.hologram.add_protons,
                "neutron": self.hologram.add_neutrons,
                "electron": self.hologram.add_electrons,
            }[particle]
            method(delta)
            self._on_log(f"EDIT: {direction} {count} {particle}{'s' if count != 1 else ''} -> {self.hologram.mode_label}")
            verb = "Added" if direction == "add" else "Removed"
            self._speak(f"{verb} {count} {particle}{'s' if count != 1 else ''}. Now {self.hologram.protons} protons, "
                        f"{self.hologram.neutrons} neutrons, {self.hologram.electron_count} electrons.")
            return True

        if "new element" in t or ("start" in t and "element" in t):
            self.hologram.protons = 1
            self.hologram.neutrons = 0
            self.hologram.electron_count = 1
            self.hologram._ensure_atom_mode()
            self.hologram._update_custom_label()
            self._on_log("EDIT: started new custom element from scratch")
            self._speak("Starting a new element with one proton and one electron. Tell me what to add.")
            return True

        # ---- Arduino/IoT Mode -------------------------------------------------------
        if any(k in t for k in ("connect to my arduino", "connect my arduino", "connect arduino",
                                 "connect to my esp32", "connect my esp32", "connect esp32",
                                 "connect iot device", "connect to my device")):
            ok, msg = self.telemetry.start()
            self._on_log(f"IOT: connect requested -> {msg}")
            self._speak(msg if not ok else "Connected. Say 'show my telemetry' to see it on the display.")
            return True

        if "telemetry" in t and any(k in t for k in ("hide", "close", "stop", "dismiss")):
            self.telemetry.stop()
            self.hologram.hide_telemetry()
            self._on_log("IOT: telemetry display closed")
            self._speak("Closing telemetry")
            return True

        if "telemetry" in t and any(k in t for k in ("show", "display")):
            name_m = re.search(r"(?:show|display)\s+(?:my\s+|the\s+)?(.+?)\s+telemetry", t)
            label = name_m.group(1).strip() if name_m and name_m.group(1).strip() else "device"
            ok, msg = self.telemetry.start()
            self.hologram.show_telemetry(label)
            self._on_log(f"IOT: showing '{label}' telemetry (link started={ok}: {msg})")
            self._speak(f"Connecting to your {label} telemetry now." if ok else
                        f"Showing the {label} display, but couldn't connect: {msg}")
            return True

        # ---- Astronomy Mode: overview ---------------------------------------------
        if any(k in t for k in ("astronomy mode", "what can astronomy mode show")):
            names = ", ".join(sorted(CONSTELLATIONS.keys())).title()
            self.hologram.show_info_card("Astronomy Mode", f"Star maps for: {names}.")
            self._on_log("DISPLAY: astronomy mode overview")
            self._speak(f"Astronomy mode can show star maps for constellations like Orion, "
                        f"the Big Dipper, Cassiopeia, and more. Just say 'show Orion', for example.")
            return True

        # ---- Astronomy Mode: constellations -----------------------------------------
        constellation_hit = next((c for c in CONSTELLATIONS if c in t), None)
        if not constellation_hit:
            alias_hit = next((a for a in CONSTELLATION_ALIASES if a in t), None)
            constellation_hit = CONSTELLATION_ALIASES.get(alias_hit)
        if constellation_hit and any(k in t for k in ("show", "display", "find", "locate")):
            self.hologram.load_constellation(constellation_hit)
            self._on_log(f"DISPLAY: {constellation_hit} constellation")
            self._speak(f"Displaying {constellation_hit.title()}")
            return True

        # ---- Lab Mode: overview -------------------------------------------------
        if any(k in t for k in ("lab mode", "what can lab mode show", "lab mode options")):
            summary = ("Molecules like water or methane, atoms, planets, satellites, DNA, "
                       "math graphs, physics simulations like a pendulum or projectile, and 3D shapes.")
            self.hologram.show_info_card("Lab Mode", summary)
            self._on_log("DISPLAY: lab mode overview")
            self._speak("Lab mode can show " + summary + " Just ask, like 'show a water molecule' "
                        "or 'graph sine of x'.")
            return True

        # ---- Lab Mode: molecules -------------------------------------------------
        molecule_hit = next((m for m in MOLECULES if m in t), None)
        if not molecule_hit:
            alias_hit = next((a for a in MOLECULE_ALIASES if a in t), None)
            molecule_hit = MOLECULE_ALIASES.get(alias_hit)
        if molecule_hit and any(k in t for k in ("show", "display", "model", "molecule", "draw")):
            self.hologram.load_molecule(molecule_hit)
            self._on_log(f"DISPLAY: {molecule_hit} molecule")
            self._speak(f"Displaying a {molecule_hit} molecule")
            return True

        # ---- Lab Mode: math graphs ------------------------------------------------
        m = re.search(r"(?:graph|plot)\s+(?:me\s+)?(?:y\s*=\s*)?(.+)$", t)
        if m and any(k in t for k in ("graph", "plot")):
            expr = m.group(1).strip()
            if self.hologram.load_math_graph(expr):
                self._on_log(f"DISPLAY: graph y={expr}")
                self._speak(f"Graphing y equals {expr}")
            else:
                self._speak("I couldn't graph that expression. Try something like 'graph sine of x' or 'graph x squared'.")
            return True

        # ---- Lab Mode: physics simulations -----------------------------------------
        if "pendulum" in t and any(k in t for k in ("show", "simulate", "display")):
            self.hologram.load_physics_sim("pendulum")
            self._on_log("DISPLAY: pendulum simulation")
            self._speak("Simulating a pendulum")
            return True
        if any(k in t for k in ("projectile", "trajectory")) and any(k in t for k in ("show", "simulate", "display")):
            self.hologram.load_physics_sim("projectile")
            self._on_log("DISPLAY: projectile simulation")
            self._speak("Simulating a projectile")
            return True

        # ---- Lab Mode: satellites ---------------------------------------------------
        if "satellite" in t and any(k in t for k in ("show", "display")):
            name_m = re.search(r"(?:show|display)\s+(?:a\s+|the\s+)?(.+?)\s+satellite", t)
            sat_name = name_m.group(1).strip().upper() if name_m and name_m.group(1).strip() else "ISS"
            self.hologram.load_satellite(sat_name)
            self._on_log(f"DISPLAY: satellite {sat_name}")
            self._speak(f"Displaying {sat_name} orbiting Earth")
            return True

        if "solar system" in t:
            self.hologram.load_solar_system()
            self._on_log("DISPLAY: solar system")
            self._speak("Displaying the solar system")
            return True

        if any(k in t for k in ("network", "constellation", "node graph", "show my systems", "system map")):
            self.hologram.load_network()
            self._on_log("DISPLAY: network view")
            self._speak("Here's a map of my systems")
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

        if "weather" in t:
            if any(k in t for k in ("hide", "close", "dismiss")):
                self.hologram.hide_weather()
                self._on_log("DISPLAY: weather hidden")
                self._speak("Closing the weather display")
                return True

            m = re.search(r"weather (?:in|at|for)\s+([a-zA-Z\s]+)", t)
            location_query = m.group(1).strip() if m else None

            # If the location matches a US state, ask for weather at its
            # capital-ish/general area and flag it so the display shows a
            # US map with that state highlighted instead of the icon.
            state_key = None
            if location_query:
                state_key = next((s for s in US_STATE_POSITIONS if s in location_query), None)

            data = self._fetch_weather_data(location_query)
            if data:
                if state_key:
                    data["state"] = state_key
                self.hologram.show_weather(data)
                self._on_log(f"DISPLAY: weather for {data['location']}" + (f" (map: {state_key})" if state_key else ""))
                temp_part = f"{round(data['temp_c'])} degrees" if data["temp_c"] is not None else "an unknown temperature"
                self._speak(f"It's {temp_part} and {data['description']} in {data['location']}.")
            else:
                if self.last_weather_error == "not_found":
                    self._speak(f"I couldn't find a place called {location_query}. Try being more specific, "
                                f"like adding a country or state.")
                elif self.last_weather_error == "no_location":
                    self._speak("I couldn't figure out your location automatically — try naming a place, "
                                "like 'weather in Chicago'.")
                elif self.last_weather_error == "network":
                    self._speak("I couldn't reach the weather service right now — check your internet connection.")
                else:
                    self._speak("Sorry, I couldn't get the weather right now — check the log for details.")
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
        element_hit = next((name for name in ELEMENTS if name in t), None)
        if element_hit:
            result = self.hologram.load_atom(element_hit)
            if result:
                symbol, name = result
                self._on_log(f"DISPLAY: {name} atom")
                self._speak(f"Displaying a {name} atom, symbol {symbol}")
            else:
                self._speak(f"I don't have a model for {element_hit}, but I can show common elements up to gold.")
            return True

        if "reset" in t and ("display" in t or "hologram" in t or "diagram" in t):
            self.hologram.load_demo()
            self._on_log("DISPLAY: reset to demo")
            self._speak("Resetting the display")
            return True

        return False

    # ---- weather (Open-Meteo — free, no API key) ---------------------------

    # WMO weather codes (used by Open-Meteo's forecast API) mapped onto
    # the small set of icon categories the hologram widget knows how to
    # draw, plus a short human-readable description for each code.
    # Reference: https://open-meteo.com/en/docs (see "WMO Weather
    # interpretation codes").
    WMO_CODE_MAP = {
        0: ("sunny", "clear sky"),
        1: ("sunny", "mainly clear"),
        2: ("partly_cloudy", "partly cloudy"),
        3: ("cloudy", "overcast"),
        45: ("fog", "fog"),
        48: ("fog", "depositing rime fog"),
        51: ("rain", "light drizzle"),
        53: ("rain", "moderate drizzle"),
        55: ("rain", "dense drizzle"),
        56: ("rain", "light freezing drizzle"),
        57: ("rain", "dense freezing drizzle"),
        61: ("rain", "slight rain"),
        63: ("rain", "moderate rain"),
        65: ("rain", "heavy rain"),
        66: ("rain", "light freezing rain"),
        67: ("rain", "heavy freezing rain"),
        71: ("snow", "slight snow fall"),
        73: ("snow", "moderate snow fall"),
        75: ("snow", "heavy snow fall"),
        77: ("snow", "snow grains"),
        80: ("rain", "slight rain showers"),
        81: ("rain", "moderate rain showers"),
        82: ("rain", "violent rain showers"),
        85: ("snow", "slight snow showers"),
        86: ("snow", "heavy snow showers"),
        95: ("storm", "thunderstorm"),
        96: ("storm", "thunderstorm with slight hail"),
        99: ("storm", "thunderstorm with heavy hail"),
    }

    def _geocode_location(self, query):
        """Open-Meteo's free geocoding endpoint: place name -> lat/lon.
        Returns (lat, lon, display_name) or None if nothing matched or
        the request failed."""
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({
                "name": query, "count": 1, "language": "en", "format": "json",
            })
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            self._on_log(f"VOICE: geocoding request failed ({e})")
            return None

        results = data.get("results")
        if not results:
            self._on_log(f"VOICE: geocoding found no match for '{query}'")
            return None

        r = results[0]
        parts = [r.get("name")]
        if r.get("admin1") and r.get("admin1") != r.get("name"):
            parts.append(r["admin1"])
        if r.get("country"):
            parts.append(r["country"])
        display_name = ", ".join(p for p in parts if p)
        return r["latitude"], r["longitude"], display_name

    def _ip_geolocate(self):
        """Guesses the user's location from their public IP via ipapi.co
        (free, keyless) — used when no location was named, e.g. 'what's
        the weather right now'. Returns (lat, lon, display_name) or None."""
        try:
            req = urllib.request.Request(
                "https://ipapi.co/json/", headers={"User-Agent": "aurora-hologram/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            self._on_log(f"VOICE: IP geolocation failed ({e})")
            return None

        lat, lon = data.get("latitude"), data.get("longitude")
        if lat is None or lon is None:
            self._on_log(f"VOICE: IP geolocation returned no coordinates ({data.get('reason', 'unknown reason')})")
            return None

        parts = [data.get("city"), data.get("region"), data.get("country_name")]
        display_name = ", ".join(p for p in parts if p) or "your location"
        return lat, lon, display_name

    def _fetch_weather_data(self, location_query):
        """Current conditions from Open-Meteo. Geocodes location_query
        first (or falls back to IP geolocation if no location was given),
        then pulls current temperature/humidity/wind/weather-code for
        that point. Sets self.last_weather_error to one of
        'not_found' | 'no_location' | 'network' | None on failure, so the
        caller can give a specific spoken response."""
        self.last_weather_error = None

        if location_query:
            geo = self._geocode_location(location_query)
            if geo is None:
                self.last_weather_error = "not_found"
                return None
        else:
            geo = self._ip_geolocate()
            if geo is None:
                self.last_weather_error = "no_location"
                return None

        lat, lon, display_name = geo

        try:
            self._on_log(f"VOICE: fetching weather for {display_name} ({lat:.3f}, {lon:.3f})...")
            url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "timezone": "auto",
            })
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            self._on_log(f"VOICE: open-meteo forecast request failed ({e})")
            self.last_weather_error = "network"
            return None

        current = data.get("current", {})
        code = current.get("weather_code")
        condition, description = self.WMO_CODE_MAP.get(code, ("cloudy", "unknown conditions"))

        result = {
            "location": display_name,
            "temp_c": current.get("temperature_2m"),
            "condition": condition,
            "description": description,
            "humidity": current.get("relative_humidity_2m"),
            # wind_speed_10m is already km/h by default in Open-Meteo's API
            "wind_kph": current.get("wind_speed_10m"),
            "updated_at": time.strftime("%H:%M"),
        }
        self._on_log(f"VOICE: weather parsed -> {result}")
        return result

    # ---- Groq brain ---------------------------------------------------------

    NEEDS_SEARCH_KEYWORDS = (
        "latest", "current", "today", "right now", "this week", "recent",
        "news", "score", "who won", "stock price", "happening now",
    )

    def _needs_search(self, text):
        t = text.lower()
        return any(k in t for k in self.NEEDS_SEARCH_KEYWORDS)

    def _ask_groq(self, text):
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
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "energy_threshold.txt")
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
                with self.microphone as source:
                    self._on_log("VOICE: calibrating for ambient noise (2 sec)...")
                    self.recognizer.adjust_for_ambient_noise(source, duration=2)
                self._clamp_energy_threshold()
                self._on_log(f"VOICE: calibration done (energy_threshold={self.recognizer.energy_threshold:.0f}), now listening")
            except Exception as e:
                self._on_log(f"VOICE: could not calibrate microphone ({e})")
                return

        loop_count = 0
        last_recalibration = time.time()
        while self._running:
            loop_count += 1

            # Ambient noise drifts over a long-running session (AC turning
            # on, other people talking, etc.) — a one-time calibration at
            # startup goes stale. Recalibrate periodically in the
            # background, but only if there's no manual override, and
            # always clamped so it can never drift up to shouting levels.
            if override is None and time.time() - last_recalibration > 120:
                try:
                    with self.microphone as source:
                        self.recognizer.adjust_for_ambient_noise(source, duration=1)
                    self._clamp_energy_threshold()
                    self._on_log(f"VOICE: recalibrated (energy_threshold={self.recognizer.energy_threshold:.0f})")
                except Exception:
                    pass
                last_recalibration = time.time()

            try:
                with self.microphone as source:
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
                self._speak("Yes?")
                continue

            self.state = "listening"
            self.last_heard = command
            self._on_log(f"HEARD: {command}")

            try:
                if self._handle_local_command(command):
                    continue

                self.state = "speaking"
                reply = self._ask_groq_stream(command)
                self.hologram.show_info_card(command, reply)
                self.state = "idle"
            except Exception as e:
                import traceback
                self._on_log(f"VOICE: command handling crashed ({type(e).__name__}: {e})")
                traceback.print_exc()
                self.state = "idle"
                # keep the loop alive no matter what went wrong above —
                # losing one command is much better than the whole voice
                # thread silently dying
