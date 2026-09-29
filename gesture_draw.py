"""
Gesture Drawing — point your index finger and draw a shape in the air;
Aurora recognises it and puts the matching object/diagram on the hologram.

  circle   -> sphere        square/rectangle -> cube     triangle -> pyramid
  line     -> graph y = x   wave             -> graph y = sin(x)
  spiral   -> solar system

Voice: "Aurora, draw mode" / "stop drawing" / "clear drawing"
Draw:  point (index only) to draw, drop the pointing pose to finish.

Everything (recognizer, drawer, overlay, voice router) lives in this file.
See the WIRING notes at the bottom.
"""

import math
import threading
import time

import cv2
import numpy as np

# ---- recognizer -------------------------------------------------------------

def _resample(pts, n=64):
    p = np.array(pts, np.float32)
    d = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    if d[-1] < 1e-6:
        return p
    t = np.linspace(0, d[-1], n)
    return np.stack([np.interp(t, d, p[:, 0]), np.interp(t, d, p[:, 1])], 1).astype(np.float32)


def _swings(y, thr):
    """Number of big up/down direction swings in a signal."""
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
    """points: list of (x, y) normalised 0..1. Returns a shape name or None."""
    if len(points) < 12:
        return None
    p = _resample(points)
    size = float((p.max(0) - p.min(0)).max())
    if size < 0.08:
        return None

    L = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    gap = float(np.linalg.norm(p[0] - p[-1]))
    c = p.mean(0)
    ang = np.unwrap(np.arctan2(p[:, 1] - c[1], p[:, 0] - c[0]))
    turns = abs(float(ang[-1] - ang[0])) / (2 * math.pi)
    rad = np.linalg.norm(p - c, axis=1)

    if turns > 1.4 and abs(rad[:8].mean() - rad[-8:].mean()) > 0.25 * rad.max():
        return "spiral"

    if gap < 0.25 * L and turns > 0.7:  # closed shape
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


# ---- drawer -------------------------------------------------------------------

REPLY = {
    "circle": "a sphere", "square": "a cube", "triangle": "a pyramid",
    "line": "a graph of y equals x", "wave": "a sine wave graph", "spiral": "the solar system",
}


class GestureDrawer:
    def __init__(self, hologram, speak=None, on_log=None):
        self.hologram = hologram
        self.speak = speak or (lambda t: None)
        self.log = on_log or (lambda t: None)
        self.active = False
        self.stroke = []
        self.cursor = None
        self.fade = []            # last finished stroke, shown briefly
        self.fade_t = 0.0
        self.toast = ("", 0.0)
        self._last_point = 0.0

    def set_active(self, on):
        self.active = on
        self.stroke, self.fade, self.cursor = [], [], None

    def _say(self, text):
        self.toast = (text, time.time())
        threading.Thread(target=self.speak, args=(text,), daemon=True).start()

    def update(self, hand):
        """Call every frame with the primary HandInfo (or None). Returns the
        recognised shape name on the frame a stroke finishes, else None."""
        if not self.active:
            return None
        now = time.time()
        if hand is not None and hand.raw_gesture == "point":
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
        self._say(f"That's {REPLY[shape]}.")

    def render(self, h):
        """Call inside Hologram._draw_dashboard (ortho mode)."""
        if not self.active:
            return
        from OpenGL.GL import glBegin, glEnd, glColor4f, glLineWidth, glVertex2f, GL_LINE_STRIP
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
            h._blit_text(h.font_big, text, h.width / 2 - h.font_big.size(text)[0] / 2,
                         100, color=(210, 240, 255))


# ---- voice router ---------------------------------------------------------------

def handle_command(drawer, speak, text):
    t = text.lower()
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


# -----------------------------------------------------------------------------------
# WIRING (4 small edits):
#
# 1. hand_tracker.py — HandInfo.__init__: add   self.tip = (0.5, 0.5)
#    and in HandTracker.read(), right after `info.raw_gesture = raw_gesture`:
#        info.tip = (lm[8].x, lm[8].y)
#
# 2. hologram.py — in _draw_dashboard(), just before self._draw_scanlines():
#        if getattr(self, "drawer", None):
#            self.drawer.render(self)
#
# 3. voice_assistant.py — top of _handle_local_command, after the "stop" block:
#        drawer = getattr(self, "drawer", None)
#        if drawer:
#            from jarvis_ui import gesture_draw
#            if gesture_draw.handle_command(drawer, self._speak, t):
#                return True
#
# 4. main.py:
#    - import:  from jarvis_ui.gesture_draw import GestureDrawer
#    - after the voice setup try/except:
#          drawer = GestureDrawer(hologram, speak=voice.speak_now, on_log=hologram.log_event)
#          hologram.drawer = drawer
#          voice.drawer = drawer
#    - right after `result, debug_frame = tracker.read()`:
#          drawer.update(result.hands[0] if result.detected else None)
#    - replace the `hologram.update(primary.yaw_norm, ...)` line with:
#          if drawer.active:   # hold rotation steady while drawing
#              hologram.update(hologram.rotation_y / 90, hologram.rotation_x / 60, 0.0, target_dt=dt)
#          else:
#              hologram.update(primary.yaw_norm, primary.pitch_norm, primary.pinch_amount, target_dt=dt)
#    - in the events loop change:  if gesture == "point":
#                             to:  if gesture == "point" and not drawer.active:
# -----------------------------------------------------------------------------------
