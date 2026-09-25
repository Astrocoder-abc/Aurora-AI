"""
Background voice pipeline: wake word -> speech-to-text -> Groq API brain
(groq/compound, built-in web search) -> natural neural speech reply.
Also handles "show me a diagram of X" and "select orbit N" style commands
by driving the hologram directly (no API call needed, so it's instant).

INTELLIGENCE (v3, all built in — no separate modules to wire up):
  - Wake word: fuzzy match with a false-positive blocklist and length
    gating, plus split-syllable pair matching ("or ora" -> aurora).
  - Search trigger: not just a fixed keyword list — also catches
    "who is X", "X vs Y", and any question naming a specific year.
  - Conversation memory: recent turns kept verbatim, older turns folded
    into a running summary instead of being silently dropped at a hard
    message-count cutoff.
  - Long-term memory: "Aurora, remember that ..." persists real facts
    to long_term_memory.json, surviving restarts, and is fed back into
    every chat reply as light context.
  - Intent fallback: if no local command regex matches, one cheap
    classification call checks whether the user meant a specific
    feature (weather/timer/atom/shape) that just didn't parse, so
    Aurora asks a clarifying question instead of answering blind.

STREAMING: general Q&A replies stream token-by-token from Groq and are
spoken sentence-by-sentence as they arrive. "Aurora, stop" cancels the
in-flight generation and clears any sentences still queued to be spoken.

PHONE (needs ADB setup — see phone_control.py):
  "Aurora, call mom on WhatsApp" / "video call Sam on WhatsApp"
  "Aurora, open Instagram on my phone"
  "Aurora, connect to my phone over wifi"
  "Aurora, connect my phone to home wifi" / "turn off wifi on my phone"
  "Aurora, search my phone for contact John" / "find resume on my phone"
  "Aurora, google best pizza on my phone"

COMPUTER: "system status", "take a screenshot", "lock my computer"

MEMORY:
  "Aurora, remember that my dog's name is Rex"
  "Aurora, forget that about my dog"
  "Aurora, what do you remember about me"

WEATHER: uses Open-Meteo (https://open-meteo.com) — free, no API key.
Geocodes a place name to lat/lon, then pulls current conditions for that
point. No location given -> IP-based geolocation (ipapi.co, also free).

TTS: Microsoft Edge's free neural voices via `edge-tts`, with the offline
Windows voice as an automatic fallback.

SETUP:
  1. Get a free API key at https://console.groq.com/ (no credit card).
  2. Put it in api_key.txt in this same folder, or set GROQ_API_KEY.
  3. Say "Aurora" + your request.
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

from jarvis_ui.hologram import ELEMENTS, SHAPES, SHAPE_ALIASES, US_STATE_POSITIONS, STAR_SYSTEMS
from jarvis_ui import system_control
from jarvis_ui import phone_control
from jarvis_ui import code_control

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

# ============================================================================
# Wake word — fuzzy, with a false-positive blocklist and length gating
# ============================================================================

WAKE_WORD_CORE = "aurora"
WAKE_WORD_FUZZY_THRESHOLD = 0.76
WAKE_WORD_MAX_LEN_DIFF = 2

WAKE_WORD_BLOCKLIST = {
    "adora", "aroma", "arena", "aurora's", "arrow", "arora's",
    "aura", "arrears", "aroura",
}


def _wake_ratio_ok(word):
    if word in WAKE_WORD_BLOCKLIST:
        return False
    if abs(len(word) - len(WAKE_WORD_CORE)) > WAKE_WORD_MAX_LEN_DIFF:
        return False
    return difflib.SequenceMatcher(None, word, WAKE_WORD_CORE).ratio() >= WAKE_WORD_FUZZY_THRESHOLD


def find_wake_word(text):
    """Returns (True, remaining_text_after_it) if a wake word is found —
    exact match, fuzzy single-word match (blocklisted + length-gated), or
    an adjacent-word-pair match for split mishearings like 'or ora'."""
    words = [w.strip(",.!?").lower() for w in text.split()]

    for i, w in enumerate(words):
        if not w:
            continue
        if w == WAKE_WORD_CORE or _wake_ratio_ok(w):
            after = " ".join(words[i + 1:])
            before = " ".join(words[:i])
            return True, (after if after else before)

    for i in range(len(words) - 1):
        if _wake_ratio_ok(words[i] + words[i + 1]):
            after = " ".join(words[i + 2:])
            before = " ".join(words[:i])
            return True, (after if after else before)

    return False, None


API_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "api_key.txt")
NOTES_FILE = os.path.join(os.path.dirname(__file__), "..", "notes.txt")
LTM_FILE = os.path.join(os.path.dirname(__file__), "..", "long_term_memory.json")

MODEL_FAST = "groq/compound"
MODEL_SEARCH = "groq/compound"

EDGE_VOICE = "en-GB-RyanNeural"

SYSTEM_PROMPT = (
    "You are Aurora, a witty, concise voice assistant speaking out loud "
    "through text-to-speech. Keep replies short, 1 to 3 sentences, since "
    "long replies are tedious to listen to. Be direct and helpful. Always "
    "respond to greetings warmly, even briefly.\n"
    "Reason from the actual conversation context before answering rather "
    "than pattern-matching the question in isolation — if the user says "
    "'what about tomorrow', check what was being discussed. If you don't "
    "know something or it depends on current information you don't have, "
    "say so plainly instead of guessing with false confidence.\n"
    "IMPORTANT: never use markdown formatting — no asterisks, bullet "
    "points, numbered lists, headers, or backticks. Plain spoken sentences "
    "only, since this text is read aloud, not displayed."
)


def clean_for_speech(text):
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
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval_node(node.left), _safe_eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval_node(node.operand))
    raise ValueError("unsupported expression")


def try_calculate(text):
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


# ============================================================================
# Smarter search-need detection
# ============================================================================

_RECENCY_WORDS = (
    "latest", "current", "currently", "today", "tonight", "right now",
    "this week", "this month", "this year", "recent", "recently",
    "news", "score", "scores", "who won", "stock price", "stock",
    "happening now", "upcoming", "just released", "just announced",
)
_QUESTION_ENTITY_RE = re.compile(
    r"\b(who is|who's|what is|what's|where is|where's|when is|when's|"
    r"how much (is|does|are)|how many|is there|has .* (released|launched|announced))\b"
)
_COMPARISON_RE = re.compile(r"\b(vs\.?|versus|compared to|better than|difference between)\b")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def needs_search(text):
    t = text.lower()
    if any(k in t for k in _RECENCY_WORDS):
        return True
    if _QUESTION_ENTITY_RE.search(t) or _COMPARISON_RE.search(t) or _YEAR_RE.search(t):
        return True
    return False


# ============================================================================
# Rolling conversation memory (recent turns + a folded summary of the rest)
# ============================================================================

_SUMMARY_PROMPT = (
    "Summarize this conversation so far in 2-3 short sentences, capturing "
    "any facts, names, or preferences the user shared that might matter "
    "later. Plain text, no markdown, no preamble."
)


class RollingMemory:
    def __init__(self, client, keep_recent=8, compress_above=14):
        self.client = client
        self.keep_recent = keep_recent
        self.compress_above = compress_above
        self.summary = None
        self.recent = []

    def add(self, role, content):
        self.recent.append({"role": role, "content": content})

    def maybe_compress(self, model, on_log=None):
        if len(self.recent) <= self.compress_above or not self.client:
            return
        to_fold = self.recent[: len(self.recent) - self.keep_recent]
        self.recent = self.recent[len(self.recent) - self.keep_recent:]

        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in to_fold)
        prior = f"Earlier summary: {self.summary}\n\n" if self.summary else ""
        try:
            resp = self.client.chat.completions.create(
                model=model,
                max_tokens=150,
                messages=[
                    {"role": "system", "content": _SUMMARY_PROMPT},
                    {"role": "user", "content": prior + transcript},
                ],
            )
            new_summary = (resp.choices[0].message.content or "").strip()
            if new_summary:
                self.summary = new_summary
        except Exception as e:
            if on_log:
                on_log(f"MEMORY: summarization failed ({e}), keeping raw history only")

    def get_messages(self):
        msgs = []
        if self.summary:
            msgs.append({"role": "system", "content": f"Earlier in this conversation: {self.summary}"})
        msgs.extend(self.recent)
        return msgs

    def clear_but_last(self):
        last = self.recent[-1] if self.recent else None
        self.recent = [last] if last else []
        self.summary = None


# ============================================================================
# Long-term memory — persists across restarts (long_term_memory.json)
# ============================================================================

_MAX_FACTS = 200
_MAX_INJECTED_CHARS = 800
_REMEMBER_RE = re.compile(r"^remember (?:that )?(.+)$")
_FORGET_FACT_RE = re.compile(r"^forget (?:that )?(.+)$")


def _slugify(text, max_words=4):
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()[:max_words]
    return "_".join(words) or f"fact_{int(time.time())}"


class LongTermMemory:
    """Plain facts Aurora remembers across restarts — separate from
    RollingMemory, which resets every session."""

    def __init__(self, on_log=None):
        self._on_log = on_log or (lambda msg: None)
        self.facts = self._load()

    def _load(self):
        if os.path.exists(LTM_FILE):
            try:
                with open(LTM_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self._on_log(f"MEMORY: could not load long_term_memory.json ({e})")
        return {}

    def _save(self):
        try:
            with open(LTM_FILE, "w", encoding="utf-8") as f:
                json.dump(self.facts, f, indent=2)
        except Exception as e:
            self._on_log(f"MEMORY: could not save long_term_memory.json ({e})")

    def remember(self, fact_text):
        fact_text = fact_text.strip().rstrip(".")
        if not fact_text:
            return False
        key = _slugify(fact_text)
        if len(self.facts) >= _MAX_FACTS and key not in self.facts:
            del self.facts[next(iter(self.facts))]
        self.facts[key] = {"text": fact_text, "saved_at": time.strftime("%Y-%m-%d")}
        self._save()
        return True

    def forget_matching(self, topic):
        topic = topic.strip().lower()
        if not topic:
            return []
        removed = []
        for key in [k for k, v in self.facts.items() if topic in v["text"].lower()]:
            removed.append(self.facts[key]["text"])
            del self.facts[key]
        if removed:
            self._save()
        return removed

    def all_facts(self):
        return [v["text"] for v in self.facts.values()]

    def as_context_block(self):
        if not self.facts:
            return None
        block = "Known facts about the user (only mention if relevant): " + "; ".join(self.all_facts())
        return block[:_MAX_INJECTED_CHARS]


# ============================================================================
# Intent fallback — one cheap classification call when no local regex fires
# ============================================================================

_INTENTS = {
    "weather": "asking about current weather/temperature/forecast anywhere",
    "timer": "setting, checking, or cancelling a timer",
    "atom": "asking to see/build a chemical element or atom model",
    "shape": "asking to see a 3d shape/model (sphere, cube, tower, dna, etc.)",
    "chat": "none of the above — general conversation or a question",
}

_INTENT_PROMPT = (
    "Classify the user's voice command into exactly one of these intents: "
    + ", ".join(_INTENTS) + ".\n"
    "Reply with ONLY JSON: {\"intent\": \"<one of the above>\"}. No explanation."
)

_CLARIFY_TEMPLATES = {
    "weather": "Sounds like you're asking about weather, but I couldn't catch a location — try 'weather in Tokyo'.",
    "timer": "Sounds like you want a timer, but I couldn't catch the duration — try 'timer for 5 minutes'.",
    "atom": "Sounds like you want an element shown, but I didn't recognize the name — which element?",
    "shape": "Sounds like you want a shape shown — sphere, cube, torus, pyramid, cylinder, the Eiffel Tower, a skyscraper, or DNA?",
}


def classify_intent(client, model, text, on_log=None):
    if not client:
        return "chat"
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=30,
            messages=[
                {"role": "system", "content": _INTENT_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        content = (resp.choices[0].message.content or "").strip().strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
        intent = json.loads(content).get("intent", "chat")
        return intent if intent in _INTENTS else "chat"
    except Exception as e:
        if on_log:
            on_log(f"INTENT: classification failed ({e}), falling back to chat")
        return "chat"


# ============================================================================
# Voice assistant
# ============================================================================

class VoiceAssistant:
    def __init__(self, hologram, face_id, on_log=None):
        self.hologram = hologram
        self.face_id = face_id
        self.state = "idle"
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
        self.active_timers = []
        self.last_weather_error = None
        self.pending_enrollment_name = None

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

        self.memory = RollingMemory(self.client)
        self.ltm = LongTermMemory(on_log=self._on_log)

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
            pass

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

    # ---- streaming Groq replies ---------------------------------------------

    _SENTENCE_END = re.compile(r"[.!?]\s|\n")

    def _build_messages(self):
        msgs = []
        ltm_block = self.ltm.as_context_block()
        if ltm_block:
            msgs.append({"role": "system", "content": ltm_block})
        msgs.extend(self.memory.get_messages())
        return msgs

    def _stream_and_speak(self, messages, use_search, question_for_card):
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
        self.memory.add("user", text)
        use_search = needs_search(text)
        already_spoken = False
        try:
            reply = self._stream_and_speak(self._build_messages(), use_search, text)
            already_spoken = True
        except Exception as e:
            msg = str(e)
            if "413" in msg or "too large" in msg.lower() or "request_too_large" in msg.lower():
                self._on_log("VOICE: request too large, clearing conversation memory and retrying")
                self.memory.clear_but_last()
                try:
                    reply = self._stream_and_speak(self._build_messages(), use_search, text)
                    already_spoken = True
                except Exception as e2:
                    reply = f"Sorry, still hit an error after clearing memory: {e2}"
            else:
                reply = f"Sorry, I hit an error talking to the API: {e}"

        if not already_spoken:
            self._speak(reply)

        trimmed = reply if len(reply) < 400 else reply[:400] + "..."
        self.memory.add("assistant", trimmed)
        self.memory.maybe_compress(MODEL_FAST, self._on_log)
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

        if re.search(r"connect (?:to )?(?:my )?phone (?:over|via|through|on) wifi|"
                     r"connect (?:to )?(?:my )?phone wirelessly|go wireless", t):
            self._speak("Connecting to your phone over wifi.")
            ok, info = phone_control.connect_wireless()
            self._on_log(f"PHONE: wireless connect {'ok' if ok else 'failed'} — {info}")
            self._speak("Connected wirelessly. You can unplug the cable now." if ok else info)
            return True

        m = re.search(r"connect (?:my )?phone to (?:the )?wifi (?:network )?(?:called |named )?(.+)$", t) or \
            re.search(r"connect (?:my )?phone to (?:the )?(.+?) wifi", t)
        if m:
            if not self._phone_ready():
                return True
            ok, msg = phone_control.wifi_connect(m.group(1).strip())
            self._on_log(f"PHONE: wifi connect {m.group(1).strip()} {'ok' if ok else 'failed'}")
            self._speak(msg)
            return True

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

        m = re.search(r"(?:open|launch|start) (.+?) (?:on|in) (?:my |the )?(?:phone|mobile)", t)
        if m:
            name = m.group(1).strip()
            if not self._phone_ready():
                return True
            ok = phone_control.open_app(name)
            self._on_log(f"PHONE: open {name} {'ok' if ok else 'failed'}")
            self._speak(f"Opening {name} on your phone." if ok else f"I couldn't find an app called {name} on your phone.")
            return True

        m = re.search(r"(?:google|search google for|search the web for) (.+?) (?:on|in) (?:my |the )?(?:phone|mobile)", t)
        if m:
            query = m.group(1).strip()
            if not self._phone_ready():
                return True
            ok = phone_control.web_search(query)
            self._on_log(f"PHONE: web search '{query}' {'ok' if ok else 'failed'}")
            self._speak(f"Searching for {query} on your phone." if ok else "I couldn't start that search on your phone.")
            return True

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
            self._speak(self.last_reply if self.last_reply else "I haven't said anything yet.")
            return True

        if t.strip() in ("what do you remember about me", "what do you know about me",
                         "what have you remembered", "list what you remember"):
            facts = self.ltm.all_facts()
            self._speak("I remember: " + "; ".join(facts) if facts else "I don't have anything saved about you yet.")
            return True

        m = _REMEMBER_RE.match(t.strip())
        if m:
            self.ltm.remember(m.group(1))
            self._on_log(f"MEMORY: saved fact '{m.group(1)[:60]}'")
            self._speak("Got it, I'll remember that.")
            return True

        m = _FORGET_FACT_RE.match(t.strip())
        if m and "face" not in t:
            removed = self.ltm.forget_matching(m.group(1))
            self._on_log(f"MEMORY: forgot {len(removed)} fact(s) matching '{m.group(1)[:40]}'")
            self._speak("Forgot it." if removed else "I didn't have anything matching that saved.")
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

        if re.search(r"\block\b", t) and any(k in t for k in ("computer", "pc", "laptop", "screen")):
            self._speak("Locking your computer.")
            self._on_log("SYSTEM: locking workstation")
            system_control.lock_workstation()
            return True

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

        if any(k in t for k in ("plus", "minus", "times", "multiplied", "divided", "percent of")) or \
                re.search(r"\d\s*[+\-*/]\s*\d", t):
            calc = try_calculate(t)
            if calc:
                result, expr = calc
                result_str = f"{result:g}"
                self._on_log(f"CALC: {expr} = {result_str}")
                self._speak(f"That's {result_str}")
                return True

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

        if self._handle_phone_command(t):
            return True

        app_hit = next((a for a in system_control.APP_COMMANDS if a in t), None)
        if app_hit and any(k in t for k in ("open", "launch", "start")):
            ok = system_control.launch_app(app_hit)
            self._on_log(f"APP: {'launched' if ok else 'failed to launch'} {app_hit}")
            self._speak(f"Opening {app_hit}" if ok else f"I couldn't open {app_hit}")
            return True

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

        shape_hit = next((s for s in SHAPES if s in t), None)
        if not shape_hit:
            alias_hit = next((a for a in SHAPE_ALIASES if a in t), None)
            shape_hit = SHAPE_ALIASES.get(alias_hit)
        if shape_hit and any(k in t for k in ("show", "display", "model", "draw", "load")):
            self.hologram.load_shape(shape_hit)
            self._on_log(f"DISPLAY: {shape_hit} model")
            self._speak(f"Displaying a {shape_hit} model")
            return True

        if any(k in t for k in ("sphere", "globe", "ball", "orb", "round thing")) \
                and any(k in t for k in ("show", "display", "model", "draw", "load")):
            self.hologram.load_shape("sphere")
            self._on_log("DISPLAY: sphere model")
            self._speak("Displaying a sphere")
            return True

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

    WMO_CODE_MAP = {
        0: ("sunny", "clear sky"), 1: ("sunny", "mainly clear"),
        2: ("partly_cloudy", "partly cloudy"), 3: ("cloudy", "overcast"),
        45: ("fog", "fog"), 48: ("fog", "depositing rime fog"),
        51: ("rain", "light drizzle"), 53: ("rain", "moderate drizzle"),
        55: ("rain", "dense drizzle"), 56: ("rain", "light freezing drizzle"),
        57: ("rain", "dense freezing drizzle"), 61: ("rain", "slight rain"),
        63: ("rain", "moderate rain"), 65: ("rain", "heavy rain"),
        66: ("rain", "light freezing rain"), 67: ("rain", "heavy freezing rain"),
        71: ("snow", "slight snow fall"), 73: ("snow", "moderate snow fall"),
        75: ("snow", "heavy snow fall"), 77: ("snow", "snow grains"),
        80: ("rain", "slight rain showers"), 81: ("rain", "moderate rain showers"),
        82: ("rain", "violent rain showers"), 85: ("snow", "slight snow showers"),
        86: ("snow", "heavy snow showers"), 95: ("storm", "thunderstorm"),
        96: ("storm", "thunderstorm with slight hail"), 99: ("storm", "thunderstorm with heavy hail"),
    }

    def _geocode_location(self, query):
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
            "wind_kph": current.get("wind_speed_10m"),
            "updated_at": time.strftime("%H:%M"),
        }
        self._on_log(f"VOICE: weather parsed -> {result}")
        return result

    # ---- main listen loop -----------------------------------------------------

    MIN_ENERGY_THRESHOLD = 50
    MAX_ENERGY_THRESHOLD = 600

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

                intent = classify_intent(self.client, MODEL_FAST, command, self._on_log) if self.client else "chat"
                if intent in _CLARIFY_TEMPLATES:
                    self._on_log(f"INTENT: guessed '{intent}' but no local handler matched")
                    self._speak(_CLARIFY_TEMPLATES[intent])
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
