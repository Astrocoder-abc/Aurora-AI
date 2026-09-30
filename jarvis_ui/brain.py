"""
Aurora brain. MERGED from: smart_brain, code_control (AI half), vision (describe_scene).

Model: Groq openai/gpt-oss-120b, reasoning_effort=medium (compound / compound-mini are gone).
  * Function-calling tools mirror Aurora's real capabilities (display, weather, timers,
    PC + phone control, notes, code writing, file search, webcam).
  * Groq's built-in browser_search is added only for questions that need live info.
  * Streaming: text deltas go to a callback so speech can start on the first sentence.
  * Reasoning tokens COUNT toward max_completion_tokens, so budgets are generous
    (the old 300/1500 limits would truncate or return empty text with this model).
"""
import base64
import difflib
import json
import re
import time

import cv2

MODEL = "openai/gpt-oss-120b"
VISION_MODELS = ("qwen/qwen3.8-27b", "meta-llama/llama-4-scout-17b-16e-instruct",
                 "meta-llama/llama-4-maverick-17b-128e-instruct")   # tried in order (gpt-oss has no vision)
EFFORT = "medium"
CHAT_TOKENS = 3000
CODE_TOKENS = 8000
MAX_TOOL_ROUNDS = 4
_EXTRA = {"reasoning_effort": EFFORT, "include_reasoning": False}

SYSTEM_PROMPT = (
    "You are Aurora, a witty, concise voice assistant on the user's Windows PC, speaking aloud through "
    "text-to-speech. Reply in 1 to 3 short spoken sentences. Never use markdown, lists, headers or "
    "backticks. Greet warmly and briefly. Reason from the conversation; if unsure, say so plainly.\n"
    "You control Aurora through tools: holographic displays (atoms, shapes, molecules, constellations, "
    "star systems, solar system, graphs, heart), weather, timers, PC volume/media/apps, the user's own "
    "Android phone, notes, code files, a local file vault, and the webcam (you CAN see through it with "
    "describe_camera). Use a tool whenever the user wants an "
    "action or display instead of only describing it, then confirm in one short sentence. Do not call "
    "a tool for plain questions. Current date and time: {now}."
)


def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


_S = lambda d: {"type": "string", "description": d}
TOOLS = [
    _fn("show_display", "Show something on the hologram display.", {
        "kind": {"type": "string", "enum": ["atom", "shape", "molecule", "constellation", "star_system",
                                            "solar_system", "network", "graph", "satellite", "physics", "heart"]},
        "name": _S("Element (carbon), shape (sphere, cube, torus, pyramid, cylinder, eiffel tower, skyscraper, dna), "
                   "molecule (water, methane, benzene...), constellation (orion...), star system (trappist-1...), "
                   "satellite (ISS) or physics sim (pendulum, projectile)"),
        "expression": _S("For graph: the function of x, e.g. 'x squared plus 2 x' or 'sine of x'")}, ["kind"]),
    _fn("get_weather", "Show and read out current weather.", {"location": _S("City or state; empty = auto-detect")}),
    _fn("set_timer", "Start a countdown timer.", {"seconds": {"type": "integer"}, "label": _S("Optional name")}, ["seconds"]),
    _fn("pc_control", "Control this PC.", {
        "action": {"type": "string", "enum": ["volume_up", "volume_down", "mute", "set_volume", "play_pause", "next_track",
                                              "previous_track", "screenshot", "lock", "status", "open_app", "open_website"]},
        "value": _S("Volume 0-100, app name, or site (google, youtube, github, gmail)")}, ["action"]),
    _fn("phone_control", "Control the user's own connected Android phone via ADB.", {
        "action": {"type": "string", "enum": ["call", "whatsapp_call", "whatsapp_video_call", "open_app", "web_search",
                                              "wifi_on", "wifi_off", "unlock"]},
        "target": _S("Contact name (or 'me'), app name, or search query")}, ["action"]),
    _fn("edit_atom", "Add or remove particles on the atom currently displayed.", {
        "particle": {"type": "string", "enum": ["proton", "neutron", "electron"]}, "delta": {"type": "integer"}},
        ["particle", "delta"]),
    _fn("take_note", "Save a note.", {"text": _S("Note text")}, ["text"]),
    _fn("read_notes", "Return the user's latest notes."),
    _fn("get_time", "Return the current local time."),
    _fn("write_code", "Generate a code file with AI and open it in VS Code or the Arduino IDE.", {
        "name": _S("File/sketch name"), "language": {"type": "string", "enum": ["python", "javascript", "cpp", "c", "html", "arduino"]},
        "description": _S("What the code should do")}, ["name", "language", "description"]),
    _fn("describe_camera", "Look through the webcam and describe what is visible, or read a QR code or barcode."),
    _fn("search_files", "Search the user's local knowledge vault (their notes/documents).", {"query": _S("Keywords")}, ["query"]),
]


# ---- helpers ----------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9']+")


def fuzzy_hit(text, keywords, threshold=0.82):
    """Keyword match tolerant of speech-to-text mishearings."""
    t = text.lower()
    words = _WORD.findall(t)
    for kw in keywords:
        kw = kw.lower()
        if kw in t:
            return True
        if " " not in kw and any(abs(len(w) - len(kw)) <= 2 and difflib.SequenceMatcher(None, w, kw).ratio() >= threshold
                                 for w in words):
            return True
    return False


_RECENCY = ("latest", "current", "currently", "today", "tonight", "right now", "this week", "this month", "this year",
            "recent", "news", "score", "who won", "stock", "happening now", "upcoming", "just released", "just announced")
_ENTITY_RE = re.compile(r"\b(who is|who's|how much (is|does|are)|has .* (released|launched|announced))\b")
_VS_RE = re.compile(r"\b(vs\.?|versus|compared to|better than)\b")
_YEAR_RE = re.compile(r"\b20(2[3-9]|[3-9]\d)\b")


def needs_search(text):
    t = text.lower()
    return any(k in t for k in _RECENCY) or bool(_ENTITY_RE.search(t) or _VS_RE.search(t) or _YEAR_RE.search(t))


def _too_large(e):
    m = str(e).lower()
    return "413" in m or "too large" in m or "request_too_large" in m


class RollingMemory:
    """Recent turns verbatim + running summary of anything older."""

    def __init__(self, client, on_log=None, keep_recent=8, compress_above=14):
        self.client, self.log = client, on_log or (lambda m: None)
        self.keep_recent, self.compress_above = keep_recent, compress_above
        self.summary, self.recent = None, []

    def add(self, role, content):
        self.recent.append({"role": role, "content": content})

    def pop_last(self):
        if self.recent:
            self.recent.pop()

    def get_messages(self):
        head = [{"role": "system", "content": f"Earlier in this conversation: {self.summary}"}] if self.summary else []
        return head + list(self.recent)

    def clear_but_last(self):
        self.recent, self.summary = self.recent[-1:], None

    def maybe_compress(self):
        if len(self.recent) <= self.compress_above or not self.client:
            return
        fold, self.recent = self.recent[:-self.keep_recent], self.recent[-self.keep_recent:]
        prior = f"Earlier summary: {self.summary}\n\n" if self.summary else ""
        try:
            s = _complete(self.client,
                          "Summarize this conversation in 2-3 short sentences, keeping facts, names and preferences "
                          "the user shared. Plain text, no markdown.",
                          prior + "\n".join(f"{m['role']}: {m['content']}" for m in fold), 800, effort="low")
            self.summary = s
        except Exception as e:
            self.log(f"MEMORY: summarization failed ({e})")


def _complete(client, system, user, max_tokens, effort=EFFORT):
    r = client.chat.completions.create(
        model=MODEL, max_completion_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        extra_body={"reasoning_effort": effort, "include_reasoning": False})
    ch = r.choices[0]
    text = (ch.message.content or "").strip()
    if not text:
        raise RuntimeError(f"model returned no text (finish_reason={ch.finish_reason})")
    return text


# ---- the agent ----------------------------------------------------------------
class Brain:
    def __init__(self, client, on_log, run_tool):
        """run_tool(name, args_dict) -> str result handed back to the model."""
        self.client, self.log, self.run_tool = client, on_log or (lambda m: None), run_tool
        self.memory = RollingMemory(client, self.log)

    def _messages(self):
        sys_msg = SYSTEM_PROMPT.format(now=time.strftime("%A %d %B %Y, %I:%M %p"))
        return [{"role": "system", "content": sys_msg}] + self.memory.get_messages()

    def _create(self, messages, tools):
        kw = dict(model=MODEL, messages=messages, stream=True, max_completion_tokens=CHAT_TOKENS, extra_body=_EXTRA)
        if tools:
            kw["tools"] = tools
        return self.client.chat.completions.create(**kw)

    def _round(self, messages, search, on_delta, cancel):
        attempts = ([TOOLS + [{"type": "browser_search"}]] if search else []) + [TOOLS, None]
        stream, last = None, None
        for tools in attempts:
            try:
                stream = self._create(messages, tools)
                break
            except Exception as e:
                last = e
                if _too_large(e):
                    raise
                self.log(f"BRAIN: request variant failed ({str(e)[:100]}), trying simpler one")
        if stream is None:
            raise last
        content, calls = "", {}
        for ev in stream:
            if cancel.is_set():
                break
            if not ev.choices:
                continue
            d = ev.choices[0].delta
            if getattr(d, "content", None):
                content += d.content
                on_delta(d.content)
            for tc in (getattr(d, "tool_calls", None) or []):
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["args"] += tc.function.arguments
        return content, [calls[i] for i in sorted(calls)]

    def _tool(self, call):
        try:
            args = json.loads(call["args"] or "{}")
        except Exception:
            args = {}
        try:
            out = self.run_tool(call["name"], args)
        except Exception as e:
            out = f"error: {e}"
        self.log(f"TOOL: {call['name']}({json.dumps(args)[:80]}) -> {str(out)[:80]}")
        return str(out)[:1500]

    def ask(self, text, on_delta, cancel):
        """Streams the reply through on_delta(str). Returns the full reply text."""
        self.memory.add("user", text)
        search, retried, parts, rounds = needs_search(text), False, [], 0
        messages = self._messages()
        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            try:
                content, calls = self._round(messages, search, on_delta, cancel)
            except Exception as e:
                if _too_large(e) and not retried:
                    retried, rounds = True, rounds - 1
                    self.log("BRAIN: request too large, trimming memory and retrying")
                    self.memory.clear_but_last()
                    messages = self._messages()
                    continue
                self.memory.pop_last()
                raise
            if content.strip():
                parts.append(content.strip())
            if not calls or cancel.is_set():
                break
            for i, c in enumerate(calls):
                c["id"] = c["id"] or f"call_{rounds}_{i}"
            messages.append({"role": "assistant", "content": content or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for c in calls]})
            for c in calls:
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": self._tool(c)})
        reply = " ".join(parts).strip()
        if reply:
            self.memory.add("assistant", reply if len(reply) < 500 else reply[:500] + "...")
        else:
            self.memory.pop_last()
        self.memory.maybe_compress()
        return reply


# ---- code generation / editing (was code_control's AI half) ---------------------
_GEN_SYS = ("You are a code generation engine. Output ONLY the complete source code for the file - no explanation, "
            "no markdown fences, no commentary.")
_EDIT_SYS = ("You are a code editing engine. Given a file's full contents and an instruction, output ONLY the complete "
             "updated file - no explanation, no markdown fences, no commentary.")


def strip_code_fences(text):
    text = text.strip()
    m = re.search(r"```[a-zA-Z0-9_+\-]*\n(.*?)\n?```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def generate_code(client, description, language="python"):
    return strip_code_fences(_complete(client, _GEN_SYS, f"Write {language} code that does the following: {description}", CODE_TOKENS))


def edit_code(client, current, instruction):
    return strip_code_fences(_complete(client, _EDIT_SYS, f"Current file contents:\n---\n{current}\n---\n\n"
                                                          f"Instruction: {instruction}\n\nOutput the complete updated file.", CODE_TOKENS))


# ---- vision (was vision.describe_scene) --------------------------------------------
def describe_scene(client, frame):
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("could not encode camera frame")
    url = "data:image/jpeg;base64," + base64.b64encode(buf).decode()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "Describe what you see in one or two short spoken sentences. Plain text, no markdown."},
        {"type": "image_url", "image_url": {"url": url}}]}]
    last = None
    for model in VISION_MODELS:
        try:
            r = client.chat.completions.create(model=model, max_completion_tokens=250, messages=msgs)
            text = (r.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            last = e
    if last:
        raise last
    return "I couldn't make anything out."
