"""
Aurora Extras — Sandbox + Spatial Desktop, combined in one file.

============================== SANDBOX =====================================
An isolated playground where Aurora can write files and run code WITHOUT
touching your real project, system files, or PATH.
  - All file ops are jailed to aurora_sandbox/ (path traversal blocked).
  - Code runs in a subprocess (not exec()'d in-process), fresh minimal
    env, cwd pinned to the sandbox, hard timeout.
  - reset_sandbox() nukes it clean, only on explicit request.

========================== SPATIAL DESKTOP =================================
Virtual panels arranged around you instead of overlapping windows. Each
panel has an ANGLE (0=front, 90=right, 180=behind, 270=left) and a DEPTH
(0=close, 3=far back). Voice:
  "Aurora, move system panel behind me"
  "Aurora, bring browser panel forward"
  "Aurora, move notes panel left/right"
  "Aurora, close weather panel" / "show my desktop"

Drop this whole file in as jarvis_ui/aurora_extras.py. Wiring instructions
(3 small snippets, nothing auto-applied) are at the very bottom.
"""

import math
import os
import re
import shutil
import subprocess
import sys
import time

# ============================================================================
# SANDBOX
# ============================================================================

SANDBOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "aurora_sandbox")
)
RUN_TIMEOUT_SECONDS = 10
MAX_OUTPUT_CHARS = 4000

_EXT_FOR_LANG = {"python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "html": ".html"}
_RUNNER_FOR_EXT = {".py": [sys.executable], ".js": ["node"]}


class SandboxError(Exception):
    pass


def _ensure_root():
    os.makedirs(SANDBOX_ROOT, exist_ok=True)


def _safe_filename(name):
    cleaned = re.sub(r"[^a-zA-Z0-9_\-. ]", "", name).strip().replace(" ", "_")
    return cleaned or "untitled"


def _safe_path(name):
    """Resolves `name` to a path INSIDE SANDBOX_ROOT or raises SandboxError.
    Blocks '..', absolute paths, symlink escapes."""
    _ensure_root()
    cleaned = _safe_filename(os.path.basename(name))
    candidate = os.path.realpath(os.path.join(SANDBOX_ROOT, cleaned))
    root_real = os.path.realpath(SANDBOX_ROOT)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        raise SandboxError(f"Refusing to touch a path outside the sandbox: {name}")
    return candidate


def write_file(name, content, language="python"):
    ext = _EXT_FOR_LANG.get(language.lower(), "")
    if ext and not name.lower().endswith(ext):
        name = name + ext
    path = _safe_path(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def read_file(name):
    path = _safe_path(name)
    if not os.path.exists(path):
        raise SandboxError(f"No such sandbox file: {name}")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def list_files():
    _ensure_root()
    return sorted(os.listdir(SANDBOX_ROOT))


def delete_file(name):
    path = _safe_path(name)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def reset_sandbox():
    """Only ever call this on an explicit user request."""
    if os.path.exists(SANDBOX_ROOT):
        shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)
    _ensure_root()


def run_file(name, timeout=RUN_TIMEOUT_SECONDS):
    """Runs a sandboxed file in a subprocess. No shell, no inherited env,
    cwd pinned to the sandbox, hard timeout. Returns (ok, stdout, stderr)."""
    path = _safe_path(name)
    if not os.path.exists(path):
        return False, "", f"No such sandbox file: {name}"

    ext = os.path.splitext(path)[1]
    runner = _RUNNER_FOR_EXT.get(ext)
    if not runner:
        return False, "", f"Don't know how to run '{ext}' files (supported: .py, .js)"

    minimal_env = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt":
        minimal_env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")

    try:
        result = subprocess.run(
            runner + [path], cwd=SANDBOX_ROOT, env=minimal_env,
            capture_output=True, text=True, timeout=timeout, shell=False,
        )
        return (result.returncode == 0,
                (result.stdout or "")[:MAX_OUTPUT_CHARS],
                (result.stderr or "")[:MAX_OUTPUT_CHARS])
    except subprocess.TimeoutExpired:
        return False, "", f"Timed out after {timeout}s (possible infinite loop)"
    except FileNotFoundError:
        return False, "", f"Interpreter not found for {ext} (is it installed and on PATH?)"
    except Exception as e:
        return False, "", str(e)


def write_and_run(name, content, language="python", timeout=RUN_TIMEOUT_SECONDS):
    path = write_file(name, content, language)
    ok, out, err = run_file(os.path.basename(path), timeout=timeout)
    return path, ok, out, err


def sandbox_summary():
    _ensure_root()
    files = list_files()
    if not files:
        return f"Sandbox is empty ({SANDBOX_ROOT})"
    return f"Sandbox has {len(files)} file(s): {', '.join(files[:8])}" + ("..." if len(files) > 8 else "")


# ============================================================================
# SPATIAL DESKTOP
# ============================================================================

FRONT, RIGHT, BEHIND, LEFT = 0, 90, 180, 270
DEFAULT_PANELS = {
    "system": {"angle": FRONT - 25, "depth": 0.6, "accent": (0.3, 0.85, 1.0)},
    "notes": {"angle": FRONT + 25, "depth": 0.6, "accent": (1.0, 0.75, 0.3)},
}
MIN_DEPTH, MAX_DEPTH = 0.0, 3.0
ANGLE_STEP = 45.0
DEPTH_STEP = 0.8


def _norm_angle(a):
    return a % 360.0


def _angle_delta(a, target):
    return (target - a + 180) % 360 - 180


class SpatialPanel:
    def __init__(self, name, angle=FRONT, depth=1.0, accent=(0.5, 0.8, 1.0), content=""):
        self.name = name
        self.angle = _norm_angle(angle)
        self.depth = max(MIN_DEPTH, min(MAX_DEPTH, depth))
        self.accent = accent
        self.content = content
        self._anim_angle = self.angle
        self._anim_depth = self.depth

    def update(self, dt, ease=6.0):
        da = _angle_delta(self._anim_angle, self.angle)
        self._anim_angle = _norm_angle(self._anim_angle + da * min(1.0, dt * ease))
        self._anim_depth += (self.depth - self._anim_depth) * min(1.0, dt * ease)


class SpatialDesktop:
    def __init__(self):
        self.panels = {}
        for name, cfg in DEFAULT_PANELS.items():
            self.panels[name] = SpatialPanel(name, cfg["angle"], cfg["depth"], cfg["accent"])
        self._last_update = time.time()

    def open_panel(self, name, content="", accent=(0.6, 0.85, 1.0)):
        key = name.strip().lower()
        if key in self.panels:
            self.panels[key].depth = 0.6
            self.panels[key].content = content or self.panels[key].content
            return self.panels[key]
        panel = SpatialPanel(key, angle=FRONT, depth=0.6, accent=accent, content=content)
        self.panels[key] = panel
        return panel

    def close_panel(self, name):
        return self.panels.pop(name.strip().lower(), None) is not None

    def find_panel(self, name):
        key = name.strip().lower()
        if key in self.panels:
            return self.panels[key]
        for pname, panel in self.panels.items():
            if key in pname or pname in key:
                return panel
        return None

    def move_behind(self, name):
        p = self.find_panel(name)
        if p:
            p.angle, p.depth = BEHIND, 2.2
        return p

    def bring_forward(self, name):
        p = self.find_panel(name)
        if p:
            p.angle, p.depth = FRONT, 0.5
        return p

    def move_left(self, name, step=ANGLE_STEP):
        p = self.find_panel(name)
        if p:
            p.angle = _norm_angle(p.angle - step)
        return p

    def move_right(self, name, step=ANGLE_STEP):
        p = self.find_panel(name)
        if p:
            p.angle = _norm_angle(p.angle + step)
        return p

    def push_back(self, name, step=DEPTH_STEP):
        p = self.find_panel(name)
        if p:
            p.depth = min(MAX_DEPTH, p.depth + step)
        return p

    def pull_closer(self, name, step=DEPTH_STEP):
        p = self.find_panel(name)
        if p:
            p.depth = max(MIN_DEPTH, p.depth - step)
        return p

    def reset_layout(self):
        for i, p in enumerate(self.panels.values()):
            p.angle = FRONT + (i - (len(self.panels) - 1) / 2) * ANGLE_STEP
            p.depth = 0.8

    def summary(self):
        if not self.panels:
            return "No panels are open."
        parts = []
        for p in self.panels.values():
            where = self._describe_angle(p.angle)
            near = "close" if p.depth < 0.8 else ("far behind you" if p.depth > 1.8 else "at arm's length")
            parts.append(f"{p.name} is {where}, {near}")
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
        """Uses Hologram's own _draw_panel/_blit_text helpers so panels
        match the rest of the UI. Call from _draw_dashboard()."""
        if not self.panels:
            return
        cx, cy = hologram.width / 2, hologram.height / 2 + 15
        for p in sorted(self.panels.values(), key=lambda p: -p._anim_depth):
            self._draw_panel_card(hologram, p, cx, cy)

    def _draw_panel_card(self, hologram, p, cx, cy):
        rel = _angle_delta(FRONT, p._anim_angle)
        side = max(-1.0, min(1.0, rel / 135.0))
        depth = p._anim_depth
        scale = max(0.28, 1.0 / (0.6 + depth))
        alpha_mult = max(0.15, 1.0 / (0.5 + depth * 0.8))
        is_behind = 135 < abs(rel) <= 180
        spread = 0.62 if not is_behind else 0.30

        pw, ph = 260 * scale, 150 * scale
        px = cx + side * hologram.width * spread - pw / 2
        py = cy - ph / 2 - (0 if not is_behind else 40 * scale)

        hologram._draw_panel(px, py, pw, ph, p.accent, chamfer=14 * scale,
                              fill_alpha=0.5, alpha_mult=alpha_mult)
        label = p.name.upper() + ("  (behind you)" if is_behind else "")
        if hologram.font_small:
            hologram._blit_text(hologram.font_small, label, px + 12, py + 10,
                                 color=tuple(int(min(255, c * 255 + 60)) for c in p.accent))
            if p.content and scale > 0.55:
                for i, line in enumerate(hologram._wrap_text(hologram.font_small, p.content, pw - 24)[:3]):
                    hologram._blit_text(hologram.font_small, line, px + 12, py + 34 + i * 16,
                                         color=(200, 220, 240))


_PANEL_WORD = r"(.+?)\s*panel"
_PATTERNS = [
    (re.compile(rf"move {_PANEL_WORD} behind me|send {_PANEL_WORD} back"), "behind"),
    (re.compile(rf"bring {_PANEL_WORD} forward|bring {_PANEL_WORD} (?:to the )?front"), "forward"),
    (re.compile(rf"move {_PANEL_WORD} left"), "left"),
    (re.compile(rf"move {_PANEL_WORD} right"), "right"),
    (re.compile(rf"push {_PANEL_WORD} back|move {_PANEL_WORD} (?:further |farther )?back"), "push_back"),
    (re.compile(rf"pull {_PANEL_WORD} closer|bring {_PANEL_WORD} closer"), "closer"),
    (re.compile(rf"close {_PANEL_WORD}|hide {_PANEL_WORD}|dismiss {_PANEL_WORD}"), "close"),
    (re.compile(rf"open (?:a |the )?{_PANEL_WORD}|show (?:me )?(?:the )?{_PANEL_WORD}"), "open"),
]


def parse_spatial_command(text):
    t = text.lower().strip()
    for pattern, action in _PATTERNS:
        m = pattern.search(t)
        if m:
            name = next(g for g in m.groups() if g)
            return action, name.strip()
    return None, None


def handle_spatial_command(desktop, text):
    """Returns (handled: bool, spoken_reply: str)."""
    t = text.lower().strip()

    if any(k in t for k in ("show my desktop", "show the desktop", "what panels", "list panels", "desktop status")):
        return True, desktop.summary()
    if "reset" in t and ("layout" in t or "desktop" in t):
        desktop.reset_layout()
        return True, "Layout reset."

    action, name = parse_spatial_command(t)
    if action is None:
        return False, ""

    dispatch = {
        "behind": (desktop.move_behind, "Moved {} behind you."),
        "forward": (desktop.bring_forward, "Bringing {} forward."),
        "left": (desktop.move_left, "Moved {} left."),
        "right": (desktop.move_right, "Moved {} right."),
        "push_back": (desktop.push_back, "Pushed {} back."),
        "closer": (desktop.pull_closer, "Brought {} closer."),
    }
    if action in dispatch:
        fn, msg = dispatch[action]
        p = fn(name)
        return True, (msg.format(p.name) if p else f"I don't have a panel called {name}.")
    if action == "close":
        ok = desktop.close_panel(name)
        return True, (f"Closed {name} panel." if ok else f"I don't have a panel called {name}.")
    if action == "open":
        p = desktop.open_panel(name)
        return True, f"Opened {p.name} panel."
    return False, ""


# ============================================================================
# VOICE COMMAND ROUTER — the ONE function to call from voice_assistant.py
# ============================================================================

def handle_command(hologram, client, on_log, speak, text):
    """Single entry point covering both sandbox and spatial-desktop voice
    commands. Returns True if it handled `text` (and already spoke the
    reply), False if the caller should keep trying other handlers.

    hologram: the Hologram instance (needs .spatial — see wiring below)
    client:   the Groq client (for sandbox code generation), or None
    on_log:   callable(str) for the event log
    speak:    callable(str) to speak a reply
    text:     the lowered command text
    """
    t = text.lower().strip()

    # ---- spatial desktop ----
    if not hasattr(hologram, "spatial"):
        hologram.spatial = SpatialDesktop()
    handled, reply = handle_spatial_command(hologram.spatial, t)
    if handled:
        speak(reply)
        return True

    # ---- sandbox ----
    m = re.search(r"(?:test|try|run) (?:this )?(?:code )?in (?:the )?sandbox[:\s]+(.+)$", t)
    if m:
        if not client:
            speak("I need the Groq API connected to generate sandbox code.")
            return True
        description = m.group(1).strip()
        speak("Writing and running that in the sandbox now.")
        try:
            from jarvis_ui import code_control  # reuse existing codegen helper
            code_text = code_control.generate_code(client, description, "python")
            path, ok, out, err = write_and_run("sandbox_test.py", code_text)
            on_log(f"SANDBOX: ran {path} -> ok={ok}")
            speak(f"Ran it. Output: {out.strip()[:200] or 'no output'}" if ok
                  else f"It errored: {err.strip()[:200]}")
        except Exception as e:
            speak(f"Sandbox error: {e}")
        return True

    if "reset" in t and "sandbox" in t:
        reset_sandbox()
        speak("Sandbox cleared.")
        return True

    if "sandbox" in t and any(k in t for k in ("what's in", "status", "show")):
        speak(sandbox_summary())
        return True

    return False


# -----------------------------------------------------------------------
# WIRING (nothing auto-applied — add these yourself):
#
# 1. In Hologram.__init__ (jarvis_ui/hologram.py):
#        from jarvis_ui import aurora_extras
#        self.spatial = aurora_extras.SpatialDesktop()
#
# 2. In Hologram._draw_dashboard(), right after self._draw_bottom_bar(...):
#        self.spatial.update()
#        self.spatial.render(self)
#
# 3. In VoiceAssistant._handle_local_command (jarvis_ui/voice_assistant.py),
#    near the top, before the other handlers:
#        from jarvis_ui import aurora_extras
#        if aurora_extras.handle_command(self.hologram, self.client,
#                                         self._on_log, self._speak, t):
#            return True
# -----------------------------------------------------------------------
