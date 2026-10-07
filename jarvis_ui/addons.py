"""
Aurora add-ons (merged: glue + visuals + gesture_draw + aurora_extras).

  1. install()            - runtime hooks onto the Hologram: heart model, flicker-free streaming
                            info card, draw-mode trail + spatial-panel overlays
  2. TipHandTracker()     - HandTracker that also reports the index fingertip (for gesture drawing)
  3. Heart + math plots   - "create a diagram of the human heart", "plot x squared"
  4. GestureDrawer        - draw a shape in the air -> matching hologram
  5. Spatial desktop      - virtual panels arranged around you ("move system panel behind me")
  6. Code sandbox         - "test in sandbox: <description>", jailed to aurora_sandbox/

Voice entry point for voice_assistant.py: handle_command(voice, text).
"""
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from jarvis_ui import system_control as sc
from OpenGL.GL import (glBegin, glEnd, glVertex2f, glVertex3f, glPushMatrix, glPopMatrix, glScalef,
                       glColor4f, glLineWidth, GL_LINE_LOOP, GL_LINE_STRIP, GL_LINES)

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


# ============================================================================
# 1. HOLOGRAM RUNTIME HOOKS + FINGERTIP TRACKER
# ============================================================================

def TipHandTracker(camera_index=0):
    """HandTracker whose primary HandInfo also carries `.tip` (index fingertip, normalised)."""
    from jarvis_ui.hand_tracker import HandTracker

    class _TipTracker(HandTracker):
        def _classify(self, lm, world, track):
            track.tip = (lm[8].x, lm[8].y)
            return super()._classify(lm, world, track)

        def read(self):
            result, frame = super().read()
            track = self._tracks.get(self._primary_id)
            if result.hands and track is not None:
                result.hands[0].tip = getattr(track, "tip", (0.5, 0.5))
            return result, frame

    return _TipTracker(camera_index=camera_index)


def install(hologram, drawer=None):
    """Applies the heart shape, streaming info-card fix and overlay drawing to a Hologram instance."""
    from jarvis_ui import hologram as hmod

    hmod.SHAPES.add("heart")
    orig_shape = hologram._draw_shape
    hologram._draw_shape = lambda name: draw_heart(hologram) if name == "heart" else orig_shape(name)

    orig_card = hologram.show_info_card

    def show_info_card(question, answer):
        card = hologram.info_card
        if hologram.mode == "info" and card and card.get("question") == question:
            card["answer"] = answer          # update in place: no restart of the materialize animation
        else:
            orig_card(question, answer)
    hologram.show_info_card = show_info_card

    orig_dash = hologram._draw_dashboard

    def draw_dashboard(theme):
        orig_dash(theme)
        spatial = getattr(hologram, "spatial", None)
        if drawer is not None or spatial is not None:
            hologram._begin_ortho()
            try:
                if drawer is not None:
                    drawer.render(hologram)
                if spatial is not None:
                    spatial.update()
                    spatial.render(hologram)
            finally:
                hologram._end_ortho()
    hologram._draw_dashboard = draw_dashboard


# ============================================================================
# 2. HEART MODEL + MATH PLOTTER
# ============================================================================

def _heart_curve(n=48):
    pts = []
    for i in range(n + 1):
        t = math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((x / 16 * 0.95, (y + 2.5) / 14.5 * 1.2))
    return pts


_CURVE = _heart_curve()


def _tube(points, radius, segs=12):
    for cx, cy, cz in points:
        glBegin(GL_LINE_LOOP)
        for j in range(segs):
            a = 2 * math.pi * j / segs
            glVertex3f(cx + radius * math.cos(a), cy, cz + radius * math.sin(a))
        glEnd()
    for off in (-radius, radius):
        glBegin(GL_LINE_STRIP)
        for cx, cy, cz in points:
            glVertex3f(cx + off, cy, cz)
        glEnd()


def draw_heart(h):
    """Stylised beating wireframe heart with aorta, vena cava and pulmonary artery."""
    beat = 1.0 + 0.05 * math.sin(h.elapsed * 5.0) ** 8
    glPushMatrix()
    glScalef(beat, beat, beat)
    for r, y in _CURVE[2:-1:2]:
        glBegin(GL_LINE_LOOP)
        for j in range(28):
            a = 2 * math.pi * j / 28
            glVertex3f(r * math.cos(a), y, r * 0.65 * math.sin(a))
        glEnd()
    for k in range(12):
        a = math.pi * 2 * k / 12
        ca, sa = math.cos(a), math.sin(a)
        glBegin(GL_LINE_STRIP)
        for r, y in _CURVE:
            glVertex3f(r * ca, y, r * 0.65 * sa)
        glEnd()
    glBegin(GL_LINES)
    glVertex3f(0.04, 0.55, 0.0); glVertex3f(-0.02, -1.15, 0.0)
    glVertex3f(0.04, 0.55, 0.0); glVertex3f(0.04, -0.1, 0.0)
    glEnd()
    aorta = [(-0.10 - 0.45 * (1 - math.cos(a)), 0.45 + 0.55 * math.sin(a), 0.0)
             for a in [i * math.pi * 0.9 / 8 for i in range(9)]]
    _tube(aorta, 0.11)
    _tube([(0.5, 0.25 + i * 0.1, 0.05) for i in range(8)], 0.10)
    _tube([(-0.1 + 0.03 * i, 0.4 + i * 0.09, 0.32) for i in range(6)], 0.09)
    glPopMatrix()


_FUNCS = [("natural log", "log"), ("absolute value", "abs"), ("square root", "sqrt"),
          ("exponential", "exp"), ("tangent", "tan"), ("cosine", "cos"), ("sine", "sin"),
          ("root", "sqrt"), ("log", "log"), ("abs", "abs"), ("tan", "tan"), ("cos", "cos"), ("sin", "sin")]
_ALLOWED = {"x", "sin", "cos", "tan", "sqrt", "abs", "exp", "log", "pi", "e"}


def speech_to_expr(text):
    """'x squared plus 2 x' -> 'x**2 + 2*x'. None if unparseable."""
    e = text.lower()
    e = re.sub(r"\b(?:the function|y equals|y is|f of x equals)\b", "", e)
    e = re.sub(r"\by\s*=", "", e)
    e = re.sub(r"^\s*(?:me|a|the)\s+", "", e)
    e = re.sub(r"\be to the (x|\d+)\b", r"exp(\1)", e)
    e = re.sub(r"\b(\w+)\s+squared\b", r"\1**2", e)
    e = re.sub(r"\b(\w+)\s+cubed\b", r"\1**3", e)
    e = re.sub(r"\s*(?:to the power of|raised to(?: the)?(?: power)?|to the)\s*(\d+)", r"**\1", e)
    for name, fn in _FUNCS:
        e = re.sub(rf"\b{name}\s+(?:of\s+)?(\w+(?:\*\*\d+)?)", fn + r"(\1)", e)
    e = re.sub(r"\bplus\b", "+", e)
    e = re.sub(r"\bminus\b", "-", e)
    e = re.sub(r"\btimes\b|\bmultiplied by\b", "*", e)
    e = re.sub(r"\bdivided by\b|\bover\b", "/", e)
    e = re.sub(r"(\d)\s*(?=[a-z(])", r"\1*", e)
    e = re.sub(r"[^a-z0-9+\-*/().^ ]", "", e).strip()
    if not e or set(re.findall(r"[a-z]+", e)) - _ALLOWED:
        return None
    return e


def plot(h, expr):
    """Samples y=f(x) on [-6, 6], auto-scales y, switches the hologram to graph mode."""
    expr = expr.replace("^", "**")
    raw = []
    for i in range(161):
        x = -6.0 + i * 0.075
        y = h._eval_graph_expr(expr, x)
        if isinstance(y, (int, float)) and not isinstance(y, bool) and math.isfinite(y):
            raw.append((x, y))
    if len(raw) < 2:
        return False
    mags = sorted(abs(y) for _, y in raw)
    p90 = mags[int(len(mags) * 0.9)]
    scale = min(1.5, 1.0 / p90) if p90 > 1e-9 else 1.0
    h.graph_points = [(x * 0.3, max(-1.5, min(1.5, y * scale)), 0.0) for x, y in raw]
    h.graph_expr = expr
    h.hide_weather()
    h.orbits = []
    h.selected_index = None
    h.mode = "graph"
    h.mode_label = f"GRAPH: y = {expr}"
    h._trigger_materialize()
    return True


# ============================================================================
# 3. GESTURE DRAWING
# ============================================================================

def _resample(pts, n=64):
    p = np.array(pts, np.float32)
    d = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    if d[-1] < 1e-6:
        return p
    t = np.linspace(0, d[-1], n)
    return np.stack([np.interp(t, d, p[:, 0]), np.interp(t, d, p[:, 1])], 1).astype(np.float32)


def _swings(y, thr):
    lo = hi = y[0]
    d = n = 0
    for v in y:
        hi, lo = max(hi, v), min(lo, v)
        if d <= 0 and v - lo > thr:
            d, hi, n = 1, v, n + 1
        elif d >= 0 and hi - v > thr:
            d, lo, n = -1, v, n + 1
    return n


def recognize(points):
    """points: [(x, y)] normalised 0..1 -> circle/square/triangle/line/wave/spiral or None."""
    if len(points) < 12:
        return None
    p = _resample(points)
    if float((p.max(0) - p.min(0)).max()) < 0.08:
        return None
    L = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    gap = float(np.linalg.norm(p[0] - p[-1]))
    c = p.mean(0)
    ang = np.unwrap(np.arctan2(p[:, 1] - c[1], p[:, 0] - c[0]))
    turns = abs(float(ang[-1] - ang[0])) / (2 * math.pi)
    rad = np.linalg.norm(p - c, axis=1)
    if turns > 1.4 and abs(rad[:8].mean() - rad[-8:].mean()) > 0.25 * rad.max():
        return "spiral"
    if gap < 0.25 * L and turns > 0.7:
        cnt = p.reshape(-1, 1, 2)
        per = cv2.arcLength(cnt, True)
        circ = 4 * math.pi * cv2.contourArea(cnt) / (per * per + 1e-9)
        if circ > 0.82:
            return "circle"
        n = len(cv2.approxPolyDP(cnt, 0.05 * per, True))
        if n == 3:
            return "triangle"
        if n == 4:
            return "square"
        return "circle" if circ > 0.7 else None
    if gap / (L + 1e-9) > 0.85:
        return "line"
    yr = float(np.ptp(p[:, 1]))
    if np.ptp(p[:, 0]) > 1.2 * yr and _swings(p[:, 1], 0.25 * yr) >= 3:
        return "wave"
    return None


DRAW_REPLY = {"circle": "a sphere", "square": "a cube", "triangle": "a pyramid",
              "line": "a graph of y equals x", "wave": "a sine wave graph", "spiral": "the solar system"}


class GestureDrawer:
    """Point with the index finger to draw; on release the shape is recognised and shown."""

    def __init__(self, hologram, speak=None, on_log=None):
        self.hologram = hologram
        self.speak = speak or (lambda t: None)
        self.log = on_log or (lambda t: None)
        self.active = False
        self.stroke, self.cursor, self.fade, self.fade_t = [], None, [], 0.0
        self.toast, self._last_point = ("", 0.0), 0.0

    def set_active(self, on):
        self.active = on
        self.stroke, self.fade, self.cursor = [], [], None

    def _say(self, text):
        self.toast = (text, time.time())
        threading.Thread(target=self.speak, args=(text,), daemon=True).start()

    def update(self, hand):
        """Call every frame with the primary HandInfo (or None). Returns the shape on the frame a stroke ends."""
        if not self.active:
            return None
        now = time.time()
        if hand is not None and hand.raw_gesture == "point" and hasattr(hand, "tip"):
            x, y = hand.tip
            if self.cursor:
                x, y = 0.5 * x + 0.5 * self.cursor[0], 0.5 * y + 0.5 * self.cursor[1]
            self.cursor = (x, y)
            if not self.stroke or math.hypot(x - self.stroke[-1][0], y - self.stroke[-1][1]) > 0.004:
                self.stroke.append((x, y))
            self._last_point = now
            return None
        if self.stroke and now - self._last_point > 0.45:
            stroke, self.stroke, self.cursor = self.stroke, [], None
            self.fade, self.fade_t = stroke, now
            shape = recognize(stroke)
            if not shape:
                self._say("I couldn't recognise that shape.")
                return None
            self._apply(shape)
            return shape
        return None

    def _apply(self, shape):
        h = self.hologram
        if shape == "circle":
            h.load_shape("sphere")
        elif shape == "square":
            h.load_shape("cube")
        elif shape == "triangle":
            h.load_shape("pyramid")
        elif shape == "line":
            h.load_math_graph("x")
        elif shape == "wave":
            h.load_math_graph("sin(x)")
        elif shape == "spiral":
            h.load_solar_system()
        self.log(f"DISPLAY: gesture drawing -> {shape}")
        self._say(f"That's {DRAW_REPLY[shape]}.")

    def render(self, h):
        if not self.active:
            return
        r, g, b = h._current_theme_color()
        now = time.time()

        def trail(pts, alpha):
            if len(pts) < 2:
                return
            glLineWidth(3.0)
            glColor4f(r, g, b, alpha)
            glBegin(GL_LINE_STRIP)
            for x, y in pts:
                glVertex2f(x * h.width, y * h.height)
            glEnd()

        trail(self.stroke, 0.95)
        if self.fade:
            trail(self.fade, max(0.0, 1.0 - (now - self.fade_t) / 1.2) * 0.8)
        if self.cursor:
            glColor4f(1, 1, 1, 0.9)
            h._draw_circle_2d(self.cursor[0] * h.width, self.cursor[1] * h.height, 6)
        if h.font_small:
            msg = "DRAW MODE - point and draw: circle, square, triangle, line, wave, spiral"
            h._blit_text(h.font_small, msg, h.width / 2 - h.font_small.size(msg)[0] / 2,
                         h.height - 120, color=(140, 200, 230))
        text, t0 = self.toast
        if text and now - t0 < 3 and h.font_big:
            h._blit_text(h.font_big, text, h.width / 2 - h.font_big.size(text)[0] / 2, 100, color=(210, 240, 255))


def _draw_command(drawer, speak, t):
    if any(k in t for k in ("stop drawing", "exit draw", "close draw", "draw mode off", "end drawing")):
        drawer.set_active(False)
        speak("Draw mode off.")
        return True
    if any(k in t for k in ("draw mode", "start drawing", "gesture drawing", "draw in the air")):
        drawer.set_active(True)
        speak("Draw mode on. Point with one finger and draw a shape.")
        return True
    if "clear" in t and "drawing" in t:
        drawer.set_active(drawer.active)
        speak("Cleared.")
        return True
    return False


# ============================================================================
# 4. SPATIAL DESKTOP (virtual panels around you)
# ============================================================================

FRONT, RIGHT, BEHIND, LEFT = 0, 90, 180, 270
DEFAULT_PANELS = {
    "system": {"angle": FRONT - 25, "depth": 0.6, "accent": (0.3, 0.85, 1.0)},
    "notes": {"angle": FRONT + 25, "depth": 0.6, "accent": (1.0, 0.75, 0.3)},
}
MIN_DEPTH, MAX_DEPTH, ANGLE_STEP, DEPTH_STEP = 0.0, 3.0, 45.0, 0.8


def _norm_angle(a):
    return a % 360.0


def _angle_delta(a, target):
    return (target - a + 180) % 360 - 180


class SpatialPanel:
    def __init__(self, name, angle=FRONT, depth=1.0, accent=(0.5, 0.8, 1.0), content=""):
        self.name, self.angle = name, _norm_angle(angle)
        self.depth = max(MIN_DEPTH, min(MAX_DEPTH, depth))
        self.accent, self.content = accent, content
        self._anim_angle, self._anim_depth = self.angle, self.depth

    def update(self, dt, ease=6.0):
        da = _angle_delta(self._anim_angle, self.angle)
        self._anim_angle = _norm_angle(self._anim_angle + da * min(1.0, dt * ease))
        self._anim_depth += (self.depth - self._anim_depth) * min(1.0, dt * ease)


class SpatialDesktop:
    def __init__(self):
        self.panels = {n: SpatialPanel(n, c["angle"], c["depth"], c["accent"]) for n, c in DEFAULT_PANELS.items()}
        self._last_update = time.time()

    def open_panel(self, name, content="", accent=(0.6, 0.85, 1.0)):
        key = name.strip().lower()
        if key in self.panels:
            self.panels[key].depth = 0.6
            self.panels[key].content = content or self.panels[key].content
            return self.panels[key]
        self.panels[key] = SpatialPanel(key, FRONT, 0.6, accent, content)
        return self.panels[key]

    def close_panel(self, name):
        p = self.find_panel(name)
        return p is not None and self.panels.pop(p.name, None) is not None

    def find_panel(self, name):
        key = re.sub(r"^(?:the|my|a)\s+", "", name.strip().lower())
        if key in self.panels:
            return self.panels[key]
        for pname, panel in self.panels.items():
            if key and (key in pname or pname in key):
                return panel
        return None

    def _apply(self, name, fn):
        p = self.find_panel(name)
        if p:
            fn(p)
        return p

    def move_behind(self, n): return self._apply(n, lambda p: (setattr(p, "angle", BEHIND), setattr(p, "depth", 2.2)))
    def bring_forward(self, n): return self._apply(n, lambda p: (setattr(p, "angle", FRONT), setattr(p, "depth", 0.5)))
    def move_left(self, n, s=ANGLE_STEP): return self._apply(n, lambda p: setattr(p, "angle", _norm_angle(p.angle - s)))
    def move_right(self, n, s=ANGLE_STEP): return self._apply(n, lambda p: setattr(p, "angle", _norm_angle(p.angle + s)))
    def push_back(self, n, s=DEPTH_STEP): return self._apply(n, lambda p: setattr(p, "depth", min(MAX_DEPTH, p.depth + s)))
    def pull_closer(self, n, s=DEPTH_STEP): return self._apply(n, lambda p: setattr(p, "depth", max(MIN_DEPTH, p.depth - s)))

    def reset_layout(self):
        for i, p in enumerate(self.panels.values()):
            p.angle = _norm_angle(FRONT + (i - (len(self.panels) - 1) / 2) * ANGLE_STEP)
            p.depth = 0.8

    def summary(self):
        if not self.panels:
            return "No panels are open."
        parts = []
        for p in self.panels.values():
            near = "close" if p.depth < 0.8 else ("far behind you" if p.depth > 1.8 else "at arm's length")
            parts.append(f"{p.name} is {self._describe_angle(p.angle)}, {near}")
        return "; ".join(parts)

    @staticmethod
    def _describe_angle(angle):
        a = _norm_angle(angle)
        if a < 22 or a >= 338:
            return "in front of you"
        if 22 <= a < 158:
            return "to your right"
        if 158 <= a < 202:
            return "behind you"
        return "to your left"

    def update(self):
        now = time.time()
        dt = max(1e-3, now - self._last_update)
        self._last_update = now
        for p in self.panels.values():
            p.update(dt)

    def render(self, hologram):
        cx, cy = hologram.width / 2, hologram.height / 2 + 15
        for p in sorted(self.panels.values(), key=lambda p: -p._anim_depth):
            self._draw_card(hologram, p, cx, cy)

    def _draw_card(self, hologram, p, cx, cy):
        rel = _angle_delta(FRONT, p._anim_angle)
        side = max(-1.0, min(1.0, rel / 135.0))
        depth = p._anim_depth
        scale = max(0.28, 1.0 / (0.6 + depth))
        alpha_mult = max(0.15, 1.0 / (0.5 + depth * 0.8))
        behind = 135 < abs(rel) <= 180
        pw, ph = 260 * scale, 150 * scale
        px = cx + side * hologram.width * (0.30 if behind else 0.62) - pw / 2
        py = cy - ph / 2 - (40 * scale if behind else 0)
        hologram._draw_panel(px, py, pw, ph, p.accent, chamfer=14 * scale, fill_alpha=0.5, alpha_mult=alpha_mult)
        if hologram.font_small:
            label = p.name.upper() + ("  (behind you)" if behind else "")
            hologram._blit_text(hologram.font_small, label, px + 12, py + 10,
                                color=tuple(int(min(255, c * 255 + 60)) for c in p.accent))
            if p.content and scale > 0.55:
                for i, line in enumerate(hologram._wrap_text(hologram.font_small, p.content, pw - 24)[:3]):
                    hologram._blit_text(hologram.font_small, line, px + 12, py + 34 + i * 16, color=(200, 220, 240))


_PW = r"(.+?)\s*panel"
_PATTERNS = [
    (re.compile(rf"move {_PW} behind me|send {_PW} back"), "behind"),
    (re.compile(rf"bring {_PW} forward|bring {_PW} (?:to the )?front"), "forward"),
    (re.compile(rf"move {_PW} left"), "left"),
    (re.compile(rf"move {_PW} right"), "right"),
    (re.compile(rf"push {_PW} back|move {_PW} (?:further |farther )?back"), "push_back"),
    (re.compile(rf"pull {_PW} closer|bring {_PW} closer"), "closer"),
    (re.compile(rf"close {_PW}|hide {_PW}|dismiss {_PW}"), "close"),
    (re.compile(rf"open (?:a |the )?{_PW}|show (?:me )?(?:the )?{_PW}"), "open"),
]


def parse_spatial_command(text):
    t = text.lower().strip()
    for pattern, action in _PATTERNS:
        m = pattern.search(t)
        if m:
            return action, next(g for g in m.groups() if g).strip()
    return None, None


def handle_spatial_command(desktop, t):
    """Returns (handled, spoken_reply)."""
    if any(k in t for k in ("show my desktop", "show the desktop", "what panels", "list panels", "desktop status")):
        return True, desktop.summary()
    if "reset" in t and ("layout" in t or "desktop" in t):
        desktop.reset_layout()
        return True, "Layout reset."
    action, name = parse_spatial_command(t)
    if action is None:
        return False, ""
    dispatch = {"behind": (desktop.move_behind, "Moved {} behind you."), "forward": (desktop.bring_forward, "Bringing {} forward."),
                "left": (desktop.move_left, "Moved {} left."), "right": (desktop.move_right, "Moved {} right."),
                "push_back": (desktop.push_back, "Pushed {} back."), "closer": (desktop.pull_closer, "Brought {} closer.")}
    if action in dispatch:
        fn, msg = dispatch[action]
        p = fn(name)
        return True, (msg.format(p.name) if p else f"I don't have a panel called {name}.")
    if action == "close":
        return True, (f"Closed {name} panel." if desktop.close_panel(name) else f"I don't have a panel called {name}.")
    p = desktop.open_panel(name)
    return True, f"Opened {p.name} panel."


# ============================================================================
# 5. CODE SANDBOX (jailed to aurora_sandbox/)
# ============================================================================

SANDBOX_ROOT = os.path.join(BASE, "aurora_sandbox")
RUN_TIMEOUT_SECONDS, MAX_OUTPUT_CHARS = 10, 4000
_EXT_FOR_LANG = {"python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "html": ".html"}
_RUNNER_FOR_EXT = {".py": [sys.executable], ".js": ["node"]}


class SandboxError(Exception):
    pass


def _safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9_\-. ]", "", name).strip().replace(" ", "_") or "untitled"


def _safe_path(name):
    os.makedirs(SANDBOX_ROOT, exist_ok=True)
    candidate = os.path.realpath(os.path.join(SANDBOX_ROOT, _safe_filename(os.path.basename(name))))
    root = os.path.realpath(SANDBOX_ROOT)
    if candidate != root and not candidate.startswith(root + os.sep):
        raise SandboxError(f"Refusing to touch a path outside the sandbox: {name}")
    return candidate


def sandbox_write(name, content, language="python"):
    ext = _EXT_FOR_LANG.get(language.lower(), "")
    if ext and not name.lower().endswith(ext):
        name += ext
    path = _safe_path(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def sandbox_list():
    os.makedirs(SANDBOX_ROOT, exist_ok=True)
    return sorted(os.listdir(SANDBOX_ROOT))


def sandbox_reset():
    shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)
    os.makedirs(SANDBOX_ROOT, exist_ok=True)


def sandbox_run(name, timeout=RUN_TIMEOUT_SECONDS):
    """Runs a sandboxed file in a subprocess (no shell, minimal env, pinned cwd). -> (ok, stdout, stderr)"""
    path = _safe_path(name)
    if not os.path.exists(path):
        return False, "", f"No such sandbox file: {name}"
    ext = os.path.splitext(path)[1]
    runner = _RUNNER_FOR_EXT.get(ext)
    if not runner:
        return False, "", f"Don't know how to run '{ext}' files (supported: .py, .js)"
    env = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    try:
        r = subprocess.run(runner + [path], cwd=SANDBOX_ROOT, env=env, capture_output=True, text=True,
                           timeout=timeout, shell=False, stdin=subprocess.DEVNULL)
        return r.returncode == 0, (r.stdout or "")[:MAX_OUTPUT_CHARS], (r.stderr or "")[:MAX_OUTPUT_CHARS]
    except subprocess.TimeoutExpired:
        return False, "", f"Timed out after {timeout}s (possible infinite loop)"
    except FileNotFoundError:
        return False, "", f"Interpreter not found for {ext}"
    except Exception as e:
        return False, "", str(e)


def sandbox_write_and_run(name, content, language="python", timeout=RUN_TIMEOUT_SECONDS):
    path = sandbox_write(name, content, language)
    ok, out, err = sandbox_run(os.path.basename(path), timeout)
    return path, ok, out, err


def _sandbox_command(voice, speak, t):
    m = re.search(r"(?:test|try|run) (?:this )?(?:code )?in (?:the )?sandbox[:\s]+(.+)$", t)
    if m:
        if not voice.client:
            speak("I need the Groq API connected to generate sandbox code.")
            return True
        speak("Writing and running that in the sandbox now.")
        try:
            from jarvis_ui import brain
            code = brain.generate_code(voice.client, m.group(1).strip(), "python")
            path, ok, out, err = sandbox_write_and_run("sandbox_test.py", code)
            voice._log(f"SANDBOX: ran {path} -> ok={ok}")
            speak(f"Ran it. Output: {out.strip()[:200] or 'no output'}" if ok else f"It errored: {err.strip()[:200]}")
        except Exception as e:
            speak(f"Sandbox error: {e}")
        return True
    if "reset" in t and "sandbox" in t:
        sandbox_reset()
        speak("Sandbox cleared.")
        return True
    if "sandbox" in t and any(k in t for k in ("what's in", "status", "show")):
        files = sandbox_list()
        speak(f"Sandbox has {len(files)} file(s): {', '.join(files[:8])}" if files else "Sandbox is empty.")
        return True
    return False


# ---- write / debug loop: model writes -> sandbox runs -> model fixes from the traceback
MAX_FIX_ROUNDS = 3
_LANG_OF_EXT = {".py": "python", ".js": "javascript"}
_NOT_NAMES = {"this", "that", "it", "these", "those", "them", "my", "the", "code", "error", "bug", "script", "file", "program"}
_DEBUG_RE = re.compile(r"(?:debug|fix)\s+(?:my\s+|the\s+)?(?:(?:code|script|file|program)\s+)?(?:called\s+)?([\w\-]+)"
                       r"(?:\s+(?:code|script|file|program))?$")
_WRITE_DEBUG_RE = re.compile(r"(?:write|make|build) (?:and (?:test|debug|run) )?(?:(python|javascript|js) )?code "
                             r"(?:called (.+?) )?that (.+?)(?: and (?:test|debug|fix|run)(?: it| any errors| the errors)?)?$")


def _unsafe(text):
    """Reason string if the text holds secrets or destructive intent (reuses Cowork's guard), else ''."""
    try:
        from jarvis_ui.aurora_plus import Guard
        ok, why = Guard.text_ok(text)
        return "" if ok else why
    except Exception:
        return ""


def debug_loop(client, code, goal="", name="debug_run", lang="python", rounds=MAX_FIX_ROUNDS):
    """Run code in the sandbox; on failure ask the model for a fix using the error. -> (code, ok, out, err, fixes)"""
    from jarvis_ui import brain
    for fixes in range(rounds + 1):
        bad = _unsafe(code)
        if bad:
            return code, False, "", f"blocked because {bad}", fixes
        _, ok, out, err = sandbox_write_and_run(name, code, lang)
        if ok or fixes == rounds:
            return code, ok, out, err, fixes
        code = brain.fix_code(client, code, err, goal, lang)


def _debug_command(voice, speak, t):
    """'write and debug code that ...' / 'debug sorter'. False when the phrase isn't for us."""
    m = _WRITE_DEBUG_RE.match(t)
    if m and re.search(r"\band (?:test|debug|run|fix)\b", t):
        lang = {"js": "javascript"}.get(m.group(1), m.group(1) or "python")
        name, desc = (m.group(2) or "sandbox_task").strip(), m.group(3).strip()
        if not voice.client:
            speak("I need the Groq API connected to write and debug code.")
            return True
        speak(f"Writing {name} and testing it in the sandbox.")
        try:
            from jarvis_ui import brain
            code, ok, out, err, fixes = debug_loop(voice.client, brain.generate_code(voice.client, desc, lang), desc, name, lang)
            voice._log(f"SANDBOX: write+debug '{name}' ok={ok} fixes={fixes}")
            if ok:
                path = sc.get_or_make_path(name, lang)
                sc.write_file_content(path, code)
                voice._open_code(path, f"Done. {name} runs cleanly after {fixes} fix{'' if fixes == 1 else 'es'}. Opened it")
            else:
                speak(f"I couldn't get it working after {fixes} fixes. Last error: {err.strip()[-150:]}")
        except Exception as e:
            speak(f"Sandbox error: {e}")
        return True
    m = _DEBUG_RE.match(t)
    if not m or m.group(1) in _NOT_NAMES:
        return False
    path = sc.resolve_project_path(m.group(1))
    if not path:
        return False                       # unknown file: let "debug this code" reach screen understanding
    lang = _LANG_OF_EXT.get(os.path.splitext(path)[1].lower())
    if not lang:
        speak("I can only run .py and .js files in the sandbox.")
    elif not voice.client:
        speak("I need the Groq API connected to debug code.")
    else:
        speak(f"Debugging {m.group(1)} in the sandbox.")
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                current = f.read()
            code, ok, out, err, fixes = debug_loop(voice.client, current, "", os.path.basename(path), lang)
            voice._log(f"SANDBOX: debug '{m.group(1)}' ok={ok} fixes={fixes}")
            if ok and not fixes:
                speak("It already runs without errors." + (f" Output: {out.strip()[:120]}" if out.strip() else ""))
            elif ok:
                diff = sc.summarize_diff(current, code)
                sc.write_file_content(path, code)          # keeps a .bak of the original
                voice._open_code(path, f"Fixed after {fixes} attempt{'' if fixes == 1 else 's'}: {diff}. Backup saved. Opened it")
            else:
                speak(f"Still failing after {fixes} fixes, so I left the file alone. Error: {err.strip()[-150:]}")
        except Exception as e:
            speak(f"Debug error: {e}")
    return True


# ============================================================================
# VOICE ENTRY POINT
# ============================================================================

def handle_command(voice, text):
    """Single router for draw mode, spatial panels, sandbox, heart and plots. True if handled."""
    t = text.lower().strip()
    H, speak = voice.hologram, voice._speak

    drawer = getattr(voice, "drawer", None)
    if drawer is not None and _draw_command(drawer, speak, t):
        return True

    if re.search(r"\b(panel|desktop|sandbox)\b", t):
        if not hasattr(H, "spatial"):
            H.spatial = SpatialDesktop()
        handled, reply = handle_spatial_command(H.spatial, t)
        if handled:
            speak(reply)
            return True
        if _sandbox_command(voice, speak, t):
            return True

    showing = bool(re.search(r"\b(show|display|draw|load|model|pull up|bring up)\b", t))
    if re.search(r"\bheart\b", t) and "rate" not in t and \
            re.search(r"\b(diagram|create|show|draw|display|model|3d)\b", t):
        H.load_shape("heart")
        voice._log("DISPLAY: human heart model")
        speak("Here's a 3D diagram of the human heart. Aorta, vena cava and pulmonary artery included.")
        return True

    m = re.match(r"\s*(?:plot|graph)\s+(.+)$", t)
    if m:
        expr = speech_to_expr(m.group(1))
        if expr and plot(H, expr):
            voice._log(f"DISPLAY: graph y={expr}")
            speak(f"Plotting y equals {m.group(1).strip()}")
        else:
            speak("I couldn't plot that. Try 'plot x squared' or 'plot sine of x'.")
        return True
    return False
