"""
Dashboard display modes drawn over the hologram: Project Timeline and Presentation Mode.

TIMELINE   "Aurora, show my project timeline" (also: ... today / this week / last week), "older", "newer",
           "close timeline". Merges git commits, recently edited project files and Aurora's own logged
           events (code, experiments, cowork, notes, searches, monitor alerts -> timeline_events.jsonl).

PRESENTATION  "Aurora, presentation mode" / "start presentation called robotics"
           Slides come from presentations/<name>.md or presentation.md (a built-in sample deck otherwise):
               # Slide title
               - bullet
               Notes: speaker notes
               Demo: show me a carbon atom        <- any normal voice command; runs when the slide appears
               ---                                <- next slide
           Voice: next slide / previous slide / first slide / last slide / slide 3, show or hide speaker notes,
           read the speaker notes, timer for 10 minutes, how much time is left, blank screen / unblank,
           run the demo / stop the demo, end presentation. Any display command ("show the solar system")
           appears live in a window beside the slide. Monitor alerts go silent while presenting.

Requires the three small hooks in hologram.py (overlays, event_listeners, quiet_alerts) and
dashboard_modes.install(hologram, voice) in main.py.
"""
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter

from OpenGL.GL import (glBegin, glEnd, glVertex2f, glColor4f, glLineWidth,
                       GL_QUADS, GL_LINES, GL_LINE_LOOP)

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
EVENTS_FILE = os.path.join(BASE, "timeline_events.jsonl")
DECK_DIR = os.path.join(BASE, "presentations")

RECORDED = ("CODE:", "EXPERIMENT:", "COWORK:", "NOTES:", "SEARCH:", "SANDBOX:", "MONITOR:", "GAME:", "VAULT:", "TIMER:")
FILE_EXTS = {".py", ".ino", ".md", ".js", ".html", ".cpp", ".c"}
SKIP_DIRS = {"venv", "env", "site-packages", "node_modules", "__pycache__", "snapshots", "face_data",
             "recordings", "cowork_exports", "aurora_sandbox", "experiments"}
KIND_COLORS = {"git": (0.3, 0.85, 1.0), "file": (1.0, 0.75, 0.3), "aurora": (0.4, 1.0, 0.6), "alert": (1.0, 0.35, 0.3)}
NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
STANDBY = 'STANDBY — SAY "AURORA"'

SAMPLE_DECK = [
    {"title": "Meet Aurora", "bullets": ["Voice and gesture controlled holographic dashboard", "Runs locally on this laptop"],
     "notes": "Introduce the project and why you built it.", "demo": ""},
    {"title": "Live atoms", "bullets": ["Real Bohr-model electron shells", "Edit protons, neutrons and electrons by voice"],
     "notes": "Say add a proton to change the element.", "demo": "show me a carbon atom"},
    {"title": "The solar system", "bullets": ["Eight planets with true colours", "Point and pinch to reshape an orbit"],
     "notes": "", "demo": "show me the solar system"},
    {"title": "Thank you", "bullets": ["Questions?"], "notes": "", "demo": ""},
]

_lock = threading.Lock()


def _to_int(word):
    return int(word) if word.isdigit() else NUM.get(word, 0)


def _mmss(sec):
    sec = int(abs(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _rect(x0, y0, x1, y1, alpha):
    glColor4f(0.01, 0.02, 0.045, alpha)
    glBegin(GL_QUADS)
    glVertex2f(x0, y0); glVertex2f(x1, y0); glVertex2f(x1, y1); glVertex2f(x0, y1)
    glEnd()


# ============================================================================
# TIMELINE DATA
# ============================================================================

def record_event(text):
    kind = "alert" if text.startswith("MONITOR:") else "aurora"
    try:
        with _lock, open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, "text": text[:120]}) + "\n")
    except OSError:
        pass


def _load_events(limit=400):
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
            out.append((e["t"], e["kind"], e["text"]))
        except Exception:
            continue
    return out


def _git_commits(limit=40):
    try:
        r = subprocess.run(["git", "-C", BASE, "log", f"-n{limit}", "--pretty=format:%ct|%s"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        ts, _, msg = line.partition("|")
        if ts.isdigit():
            out.append((int(ts), "git", msg[:80]))
    return out


def _recent_files(limit=40):
    found = []
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in FILE_EXTS or fn == "notes.txt":
                path = os.path.join(root, fn)
                try:
                    found.append((os.path.getmtime(path), "file", os.path.relpath(path, BASE).replace("\\", "/")))
                except OSError:
                    continue
    found.sort(reverse=True)
    return found[:limit]


def collect(since=None, limit=300):
    """[(timestamp, kind, text)] newest first."""
    items = _load_events() + _git_commits() + _recent_files()
    if since:
        items = [i for i in items if i[0] >= since]
    items.sort(key=lambda i: i[0], reverse=True)
    return items[:limit]


# ============================================================================
# TIMELINE MODE
# ============================================================================

class Timeline:
    ROW_H = 34

    def __init__(self, modes):
        self.m = modes
        self.items, self.page, self.max_page, self.label = [], 0, 0, "all time"

    def open(self, since, label):
        self.m.enter("timeline", "PROJECT TIMELINE")
        self.items, self.page, self.label = collect(since), 0, label
        return f"Here's your project timeline, {len(self.items)} events." if self.items else "I couldn't find any project activity yet."

    def scroll(self, delta):
        self.page = max(0, min(self.max_page, self.page + delta))

    def draw(self, h, theme):
        pw, ph = max(560, min(900, h.width - 500)), h.height - 260
        px, py = (h.width - pw) / 2, (h.height - ph) / 2 + 10
        a = h._materialize_progress()
        h._draw_panel(px, py, pw, ph, theme, chamfer=20, fill_alpha=0.6, alpha_mult=a)
        h._blit_text(h.font, f"PROJECT TIMELINE  -  {self.label.upper()}", px + 24, py + 14, color=(180, 220, 245))

        rows = max(1, int((ph - 100) // self.ROW_H))
        pages = max(1, -(-len(self.items) // rows))
        self.max_page = pages - 1
        self.page = min(self.page, self.max_page)
        shown = self.items[self.page * rows:(self.page + 1) * rows]
        rail_x, y0 = px + 150, py + 56
        if not shown:
            h._blit_text(h.font_small, "No project activity found yet.", px + 24, y0, color=(160, 190, 220))
        else:
            glColor4f(theme[0], theme[1], theme[2], 0.4 * a)
            glLineWidth(1.5)
            glBegin(GL_LINES)
            glVertex2f(rail_x, y0 + 8); glVertex2f(rail_x, y0 + (len(shown) - 1) * self.ROW_H + 8)
            glEnd()
        chars = int((pw - 190) / 7)
        for i, (ts, kind, text) in enumerate(shown):
            y, col = y0 + i * self.ROW_H, KIND_COLORS.get(kind, (0.7, 0.7, 0.7))
            glColor4f(col[0], col[1], col[2], 0.25 * a)
            h._draw_circle_2d(rail_x, y + 8, 8)
            glColor4f(col[0], col[1], col[2], a)
            h._draw_circle_2d(rail_x, y + 8, 4)
            h._blit_text(h.font_small, time.strftime("%b %d %H:%M", time.localtime(ts)), px + 24, y, color=(140, 180, 210))
            h._blit_text(h.font_small, h._truncate(text, chars), rail_x + 20, y, color=(210, 235, 255))

        counts = Counter(k for _, k, _ in self.items)
        footer = (f"git {counts['git']}   files {counts['file']}   events {counts['aurora']}   alerts {counts['alert']}"
                  f"      page {self.page + 1}/{pages}      say older, newer or close timeline")
        h._blit_text(h.font_small, h._truncate(footer, chars + 20), px + 24, py + ph - 34, color=(110, 150, 175))


# ============================================================================
# PRESENTATION MODE
# ============================================================================

def parse_slides(text):
    slides = []
    for block in re.split(r"^\s*---+\s*$", text, flags=re.M):
        s = {"title": "", "bullets": [], "notes": "", "demo": ""}
        for line in block.strip().splitlines():
            ln = line.strip()
            if not ln:
                continue
            low = ln.lower()
            if ln.startswith("#") and not s["title"]:
                s["title"] = ln.lstrip("# ").strip()
            elif low.startswith("notes:"):
                s["notes"] = ln[6:].strip()
            elif low.startswith("demo:"):
                s["demo"] = ln[5:].strip()
            else:
                s["bullets"].append(ln.lstrip("-*• ").strip())
        if s["title"] or s["bullets"]:
            slides.append(s)
    return slides


def load_deck(name=None):
    """-> (slides, label). Falls back to presentation.md, then the sample deck."""
    paths = []
    safe = re.sub(r"[^\w\- ]", "", name or "").strip().replace(" ", "_")
    if safe:
        paths += [os.path.join(DECK_DIR, safe + ".md"), os.path.join(BASE, safe + ".md")]
    paths.append(os.path.join(BASE, "presentation.md"))
    for p in paths:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    slides = parse_slides(f.read())
                if slides:
                    return slides, os.path.splitext(os.path.basename(p))[0]
            except OSError:
                continue
    return SAMPLE_DECK, "sample deck"


def _run_silent(voice, phrase):
    """Runs a normal voice command without speaking its confirmation."""
    if voice is None or not hasattr(voice, "_handle_local_command"):
        return ""
    if getattr(voice, "_capture", None) is not None:
        return voice._handle_local_command(phrase)
    return voice._run_local(phrase)


class Presentation:
    def __init__(self, modes):
        self.m = modes
        self.slides, self.name, self.i = [], "", 0
        self.blank = self.show_notes = False
        self.started, self.timer_end, self.timer_total, self.warned, self.saved_tx = 0.0, None, 0, set(), 0.0

    @property
    def active(self):
        return self.m.active == "presentation"

    # ---- lifecycle ---------------------------------------------------------
    def open(self, name=None):
        h = self.m.h
        self.m.enter("presentation", "PRESENTATION MODE")
        self.slides, self.name = load_deck(name)
        self.i, self.blank, self.show_notes = 0, False, False
        self.started, self.timer_end, self.warned = time.time(), None, set()
        self.saved_tx = h.translate_x
        h.quiet_alerts = True
        self._apply_slide()
        return f"Presentation mode. {len(self.slides)} slides from {self.name}. Say next slide to move on."

    def leave(self):
        self.m.h.translate_x = self.saved_tx
        self.m.h.quiet_alerts = False

    def close(self):
        self.leave()
        self.m.active = None
        self.m.h.mode, self.m.h.mode_label = "empty", STANDBY
        return "Presentation mode off."

    def _stage(self):
        h = self.m.h
        h.hide_weather()
        h.mode, h.mode_label, h.info_card, h.orbits, h.selected_index = "info", "PRESENTATION MODE", None, [], None

    def _apply_slide(self):
        demo = self.slides[self.i]["demo"]
        if demo:
            _run_silent(self.m.voice, demo)
            if self.m.h.mode in ("info", "empty"):
                self._stage()
        else:
            self._stage()

    # ---- navigation / timer ---------------------------------------------------
    def goto(self, j):
        if not 0 <= j < len(self.slides):
            return f"There's no slide {j + 1}. This deck has {len(self.slides)} slides."
        self.i = j
        self._apply_slide()
        return ""

    def step(self, d):
        j = self.i + d
        if j < 0:
            return "This is the first slide."
        if j >= len(self.slides):
            return "That was the last slide."
        return self.goto(j)

    def set_timer(self, seconds):
        self.timer_end, self.timer_total, self.warned = time.time() + seconds, seconds, set()

    def _tick(self, now):
        if self.timer_end is None:
            return
        left = self.timer_end - now
        for mark, msg in ((60, "One minute left."), (0, "Time is up.")):
            if left <= mark and mark not in self.warned and (mark == 0 or self.timer_total > 120):
                self.warned.add(mark)
                self.m.say(msg)

    def _timer_text(self, now):
        if self.timer_end is None:
            return _mmss(now - self.started), (140, 190, 220)
        left = self.timer_end - now
        if left <= 0:
            return "-" + _mmss(left), (255, 70, 70) if int(now * 2) % 2 else (255, 150, 150)
        return _mmss(left), (255, 200, 80) if left <= 60 else (140, 255, 190)

    # ---- voice ---------------------------------------------------------------------
    def command(self, t):
        """Reply string ('' = handled silently) or None if the phrase isn't for presentation mode."""
        n = len(self.slides)
        if re.search(r"\b(?:end|exit|close|leave|stop)\s+(?:the\s+)?(?:presentation|presenting)\b|\bpresentation mode\s+off\b", t):
            return self.close()
        if re.search(r"\bblank (?:the )?screen\b|\bblack ?out\b", t):
            self.blank = True
            return ""
        if re.search(r"\bun-?blank\b|\b(?:show|restore) (?:the )?(?:screen|slide)\b", t):
            self.blank = False
            return ""
        if re.search(r"\b(?:speaker|presenter|slide) notes\b", t):
            if re.search(r"\b(?:hide|close)\b", t):
                self.show_notes = False
                return ""
            if re.search(r"\bread\b", t):
                return self.slides[self.i]["notes"] or "There are no notes on this slide."
            self.show_notes = True
            return ""
        m = re.search(r"\btimer\b(?:\s+for)?\s+(\d+)\s*(second|minute|hour)", t) or re.search(r"(\d+)[\s-]*(second|minute|hour)s?[\s-]*timer", t)
        if m:
            amount = int(m.group(1))
            self.set_timer(amount * {"second": 1, "minute": 60, "hour": 3600}[m.group(2)])
            return f"Presentation timer set for {amount} {m.group(2)}{'s' if amount != 1 else ''}."
        if self.timer_end is not None and re.search(r"\b(?:cancel|clear|remove|stop)\b.*\btimer\b", t):
            self.timer_end = None
            return "Timer cleared."
        if self.timer_end is not None and re.search(r"\b(?:how much time|time left|time remaining)\b", t):
            mins, secs = divmod(max(0, int(self.timer_end - time.time())), 60)
            return f"{mins} minutes {secs} seconds left." if mins else f"{secs} seconds left."
        if re.search(r"\bhow long\b.*\bpresent", t):
            return f"You've been presenting for {int((time.time() - self.started) // 60)} minutes."
        if re.search(r"\bnext slide\b|^(?:next|forward)$", t):
            return self.step(1)
        if re.search(r"\b(?:previous|prior) slide\b|\bback a slide\b|^back$", t):
            return self.step(-1)
        if re.search(r"\bfirst slide\b|\bstart over\b|\bfrom the (?:top|beginning)\b", t):
            return self.goto(0)
        if re.search(r"\b(?:last|final) slide\b", t):
            return self.goto(n - 1)
        m = re.search(r"\bslide\s+(?:number\s+)?(\d+|" + "|".join(NUM) + r")\b", t)
        if m:
            return self.goto(_to_int(m.group(1)) - 1)
        if re.search(r"\b(?:run|start|play|restart)\s+(?:the\s+)?demo\b", t):
            if not self.slides[self.i]["demo"]:
                return "This slide has no demo."
            self._apply_slide()
            return ""
        if re.search(r"\b(?:stop|end|clear|close)\s+(?:the\s+)?demo\b", t):
            self._stage()
            return ""
        return None

    # ---- drawing ---------------------------------------------------------------------
    def draw(self, h, theme):
        now = time.time()
        self._tick(now)
        w, hh = h.width, h.height
        if self.blank:
            _rect(0, 0, w, hh, 1.0)
            return

        demo = h.mode not in ("info", "empty")           # any display command shows up in a window beside the slide
        left, right, top, bottom = w * 0.44, w - 340, 100, hh - 130
        ppu = hh / 4.97
        target = self.saved_tx + (((left + right) / 2 - w / 2) / ppu if demo else 0.0)
        h.translate_x += (target - h.translate_x) * 0.12
        if demo:
            for r in ((0, 0, w, top), (0, bottom, w, hh), (0, top, left, bottom), (right, top, w, bottom)):
                _rect(*r, 0.96)
            glColor4f(theme[0], theme[1], theme[2], 0.5)
            glLineWidth(1.5)
            glBegin(GL_LINE_LOOP)
            glVertex2f(left, top); glVertex2f(right, top); glVertex2f(right, bottom); glVertex2f(left, bottom)
            glEnd()
        else:
            _rect(0, 0, w, hh, 0.97)

        s, n = self.slides[self.i], len(self.slides)
        h._blit_text(h.font_small, f"PRESENTATION  -  {self.name.upper()}", 70, 34, color=(120, 160, 185))
        txt, col = self._timer_text(now)
        tw = h.font_big.size(txt)[0] if h.font_big else 100
        h._blit_text(h.font_big, txt, w - tw - 70, 26, color=col)

        text_w = (left - 100) if demo else min(w - 140, 1100)
        y = 100
        for line in h._wrap_text(h.font_huge, s["title"], text_w)[:2]:
            h._blit_text(h.font_huge, line, 70, y, color=(215, 240, 255))
            y += 62
        glColor4f(theme[0], theme[1], theme[2], 0.8)
        glLineWidth(2.0)
        glBegin(GL_LINES)
        glVertex2f(70, y + 4); glVertex2f(250, y + 4)
        glEnd()
        y += 28
        limit = hh - (270 if self.show_notes else 130)
        for b in s["bullets"]:
            glColor4f(theme[0], theme[1], theme[2], 0.9)
            h._draw_circle_2d(78, y + 14, 4)
            for ln in h._wrap_text(h.font_big, b, text_w - 34)[:3]:
                h._blit_text(h.font_big, ln, 94, y, color=(190, 225, 245))
                y += 36
            y += 14
            if y > limit:
                break

        if self.show_notes:
            h._draw_panel(70, hh - 250, text_w, 150, theme, chamfer=14, fill_alpha=0.7)
            h._blit_text(h.font_small, "SPEAKER NOTES", 90, hh - 238, color=(90, 130, 160))
            ny = hh - 214
            for ln in h._wrap_text(h.font_small, s["notes"] or "No notes for this slide.", text_w - 40)[:6]:
                h._blit_text(h.font_small, ln, 90, ny, color=(200, 225, 245))
                ny += 18
        h._blit_text(h.font_small, f"{self.i + 1} / {n}", 70, hh - 86, color=(140, 180, 210))
        h._draw_segmented_bar(70, hh - 60, w - 140, 8, (self.i + 1) / n, theme, segments=min(n, 40))


# ============================================================================
# MODE CONTROLLER + VOICE ENTRY POINT
# ============================================================================

_TL_OPEN = re.compile(r"\btimeline mode\b|\bproject timeline\b|\bmy timeline\b|"
                      r"\b(?:show|open|display|create|make|build)\s+(?:me\s+)?(?:a\s+|the\s+)?timeline"
                      r"(?:\s+(?:of|for)\s+(?:my|the)\s+(?:project|work|progress))?"
                      r"(?:\s+(?:from\s+)?(?:today|this week|last week|the past week|this month))?$")


class DashboardModes:
    def __init__(self, hologram):
        self.h, self.voice, self.active = hologram, None, None
        self.timeline, self.presentation = Timeline(self), Presentation(self)

    def say(self, text):
        if self.voice is not None:
            self.voice.speak_now(text)

    def enter(self, name, label):
        """Switch the display into one of the overlay modes (mode 'info' hides the 3D core and readouts)."""
        h = self.h
        labs = getattr(h, "labs", None)
        if labs is not None:
            labs.close()
        if self.active == "presentation":
            self.presentation.leave()
        h.hide_weather()
        h.info_card, h.orbits, h.selected_index = None, [], None
        h.mode, h.mode_label = "info", label
        h._trigger_materialize()
        self.active = name

    def draw(self, theme):
        h = self.h
        labs = getattr(h, "labs", None)
        if labs is not None and labs.active:
            return
        if self.active == "timeline":
            if h.mode != "info" or h.info_card is not None:      # something else took over the display
                self.active = None
            else:
                self.timeline.draw(h, theme)
        elif self.active == "presentation":
            self.presentation.draw(h, theme)

    def route(self, t):
        tl = self.timeline
        if re.search(r"\b(?:close|hide|exit|dismiss|end)\b.*\btimeline\b|\btimeline\b.*\b(?:off|close)\b", t):
            if self.active == "timeline":
                self.active, self.h.mode, self.h.mode_label = None, "empty", STANDBY
                return "Timeline closed."
            return None
        if _TL_OPEN.search(t):
            now = time.localtime()
            if "today" in t:
                return tl.open(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)), "today")
            if re.search(r"this week|last week|past week", t):
                return tl.open(time.time() - 7 * 86400, "past week")
            if "this month" in t:
                return tl.open(time.time() - 30 * 86400, "past month")
            return tl.open(None, "all time")
        if self.active == "timeline":
            if re.search(r"\b(?:older|earlier|next page|page down)\b", t):
                tl.scroll(1)
                return ""
            if re.search(r"\b(?:newer|more recent|previous page|page up)\b", t):
                tl.scroll(-1)
                return ""

        p = self.presentation
        if p.active:
            return p.command(t)
        if re.search(r"\bpresentation mode\b|\b(?:start|open|begin)\s+(?:the\s+|my\s+|a\s+)?presentation\b", t):
            if re.search(r"\b(?:off|exit|end|close|stop)\b", t):
                return "Presentation mode isn't on."
            m = re.search(r"\b(?:called|named|about|for)\s+(.+)$", t)
            return p.open(m.group(1) if m else None)
        return None


def install(hologram, voice=None):
    """Attach the modes to a Hologram (idempotent) and return the controller."""
    modes = getattr(hologram, "modes", None)
    if not isinstance(modes, DashboardModes):          # also replaces auto-created attributes (e.g. test mocks)
        modes = hologram.modes = DashboardModes(hologram)
        hologram.overlays.append(modes.draw)
        hologram.event_listeners.append(lambda text: record_event(text) if text.startswith(RECORDED) else None)
    if voice is not None:
        modes.voice = voice
    return modes


def handle_command(voice, text):
    """Voice entry point for voice_assistant.py. Returns True if handled."""
    modes = install(voice.hologram, voice)
    reply = modes.route(text.lower().strip(" .?!"))
    if reply is None:
        return False
    if reply:
        voice._speak(reply)
    return True
