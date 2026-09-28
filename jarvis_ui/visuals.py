"""
jarvis_ui/visuals.py - Voice-to-Hologram (human heart) + Math Visualizer.

  "Aurora, create a diagram of the human heart"  -> beating 3D heart with
                                                     aorta / vena cava / pulmonary artery
  "Aurora, plot x squared"                        -> auto-scaled graph
  "Aurora, plot sine of x" / "graph 2 x plus 1" / "plot x cubed minus 3 x"
  "Aurora, plot square root of x" / "plot e to the x" / "plot x to the power of 4"

Everything (drawing, parsing, routing) lives here. Wiring is at the bottom.
"""

import math
import re

from OpenGL.GL import (glBegin, glEnd, glVertex3f, glPushMatrix, glPopMatrix,
                       glScalef, GL_LINE_LOOP, GL_LINE_STRIP, GL_LINES)

# ============================================================================
# HEART MODEL
# ============================================================================

def _heart_curve(n=48):
    """Half of the classic heart outline (t = 0..pi, x >= 0) as (radius, y)."""
    pts = []
    for i in range(n + 1):
        t = math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((x / 16 * 0.95, (y + 2.5) / 14.5 * 1.2))
    return pts


_CURVE = _heart_curve()


def _tube(points, radius, segs=12):
    """Wire tube: XZ rings at each centre point plus two side lines."""
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
    """Stylised wireframe heart. `h` is the Hologram (uses h.elapsed to beat)."""
    beat = 1.0 + 0.05 * math.sin(h.elapsed * 5.0) ** 8
    glPushMatrix()
    glScalef(beat, beat, beat)

    # rings (latitudes) - squashed in z so it reads as a flattened organ
    for r, y in _CURVE[2:-1:2]:
        glBegin(GL_LINE_LOOP)
        for j in range(28):
            a = 2 * math.pi * j / 28
            glVertex3f(r * math.cos(a), y, r * 0.65 * math.sin(a))
        glEnd()
    # meridians
    for k in range(12):
        a = math.pi * 2 * k / 12
        ca, sa = math.cos(a), math.sin(a)
        glBegin(GL_LINE_STRIP)
        for r, y in _CURVE:
            glVertex3f(r * ca, y, r * 0.65 * sa)
        glEnd()

    # septum (divides left / right side)
    glBegin(GL_LINES)
    glVertex3f(0.04, 0.55, 0.0)
    glVertex3f(-0.02, -1.15, 0.0)
    glVertex3f(0.04, 0.55, 0.0)
    glVertex3f(0.04, -0.1, 0.0)
    glEnd()

    # aorta: arches up and over to the left
    aorta = [(-0.05 - 0.38 * math.sin(a), 0.5 + 0.5 * math.sin(a * 0.5 + 0.0) + 0.25 * math.sin(a), 0.0)
             for a in [i * math.pi / 8 for i in range(0, 9)]]
    aorta = [(-0.10 - 0.45 * (1 - math.cos(a)) / 2 * 2, 0.45 + 0.55 * math.sin(a), 0.0)
             for a in [i * math.pi * 0.9 / 8 for i in range(9)]]
    _tube(aorta, 0.11)
    # superior vena cava (right side of the heart)
    _tube([(0.5, 0.25 + i * 0.1, 0.05) for i in range(8)], 0.10)
    # pulmonary artery (front)
    _tube([(-0.1 + 0.03 * i, 0.4 + i * 0.09, 0.32) for i in range(6)], 0.09)

    glPopMatrix()


# ============================================================================
# MATH VISUALIZER
# ============================================================================

_FUNCS = [("natural log", "log"), ("absolute value", "abs"), ("square root", "sqrt"),
          ("exponential", "exp"), ("tangent", "tan"), ("cosine", "cos"), ("sine", "sin"),
          ("root", "sqrt"), ("log", "log"), ("abs", "abs"), ("tan", "tan"),
          ("cos", "cos"), ("sin", "sin")]
_ALLOWED = {"x", "sin", "cos", "tan", "sqrt", "abs", "exp", "log", "pi", "e"}


def speech_to_expr(text):
    """'x squared plus 2 x' -> 'x**2 + 2*x'. Returns None if unparseable."""
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

    e = re.sub(r"(\d)\s*(?=[a-z(])", r"\1*", e)       # 2 x / 2x -> 2*x
    e = re.sub(r"[^a-z0-9+\-*/().^ ]", "", e).strip()
    if not e or set(re.findall(r"[a-z]+", e)) - _ALLOWED:
        return None
    return e


def plot(h, expr):
    """Samples y=f(x) on [-6, 6], auto-scales y to fit the display, and puts
    the hologram in graph mode. Returns True on success."""
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
# VOICE ROUTER
# ============================================================================

def handle_command(hologram, speak, on_log, text):
    """Returns True if `text` was handled (and already spoken)."""
    t = text.lower().strip()

    if "heart" in t and "rate" not in t and \
            re.search(r"\b(diagram|create|show|draw|display|model|3d)\b", t):
        hologram.load_shape("heart")
        on_log("DISPLAY: human heart model")
        speak("Here's a 3D diagram of the human heart. Aorta, vena cava and pulmonary artery included.")
        return True

    m = re.search(r"\b(?:plot|graph)\s+(.+)$", t)
    if m:
        expr = speech_to_expr(m.group(1))
        if expr and plot(hologram, expr):
            on_log(f"DISPLAY: graph y={expr}")
            speak(f"Plotting y equals {m.group(1).strip()}")
        else:
            speak("I couldn't plot that. Try 'plot x squared' or 'plot sine of x'.")
        return True

    return False


# -----------------------------------------------------------------------
# WIRING (3 small edits):
#
# 1. jarvis_ui/hologram.py
#    a) SHAPES set: add "heart"
#         SHAPES = {"sphere", ..., "dna", "heart"}
#    b) end of Hologram._draw_shape():
#         elif name == "heart":
#             from jarvis_ui import visuals
#             visuals.draw_heart(self)
#
# 2. jarvis_ui/voice_assistant.py
#    a) top imports:   from jarvis_ui import visuals
#    b) first lines of _handle_local_command(), after `t = text.lower()`:
#         if visuals.handle_command(self.hologram, self._speak, self._on_log, t):
#             return True
# -----------------------------------------------------------------------
