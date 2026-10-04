"""
Adaptive interface: the dashboard layout changes only when you say "switch to <mode> mode".

  "Aurora, switch to coding mode"    git branch, recent files and code activity panel
  "Aurora, switch to studying mode"  notes + study timer (left) and flashcards/reference panel (right)
  "Aurora, switch to gaming mode"    performance HUD (CPU, RAM, temperature, session, foreground app)
  "Aurora, switch to standard mode"  the normal dashboard (also: normal / default)

Modes hide the dashboard panels they replace through hologram.hidden_panels (see the 3-line patch in
Hologram._draw_dashboard). Nothing is detected or switched automatically.
"""
import os
import re
import subprocess
import threading
import time

from jarvis_ui import dashboard_modes

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ALIASES = {"coding": "coding", "programming": "coding", "code": "coding", "studying": "studying", "study": "studying",
           "gaming": "gaming", "game": "gaming", "standard": "standard", "normal": "standard", "default": "standard"}
HIDDEN = {"standard": (), "coding": ("log",), "studying": ("system", "log", "readout"), "gaming": ("system", "log", "readout")}
REPLIES = {"coding": "Coding mode. Git status, recent files and code activity are on the right.",
           "studying": "Studying mode. Notes and timer on the left, flashcards and reference on the right.",
           "gaming": "Gaming mode. Performance HUD is on.",
           "standard": "Standard interface."}
_SWITCH = re.compile(r"switch\s+(?:over\s+)?(?:to|into)\s+(?:the\s+)?(" + "|".join(sorted(ALIASES, key=len, reverse=True))
                     + r")\s+mode$", re.I)


class AdaptiveInterface:
    def __init__(self, hologram, voice=None):
        self.h, self.voice, self.mode = hologram, voice, "standard"
        self._info, self._info_t, self._busy, self._started_reader = {}, 0.0, False, False

    def switch(self, mode):
        reader = getattr(self.voice, "game_session", None)
        if self.mode == "gaming" and mode != "gaming" and self._started_reader and reader:
            reader.stop()
            self._started_reader = False
        self.mode = mode
        self.h.hidden_panels = HIDDEN[mode]
        if mode == "gaming" and reader and not reader.connected:
            reader.start()
            self._started_reader = True
        self._info_t = 0.0
        self.h.log_event(f"INTERFACE: {mode} mode")

    # ---- background data (git, files, study stats) so drawing never blocks ------
    def _gather(self):
        info = {}
        try:
            git = lambda *a: subprocess.run(["git", "-C", BASE, *a], capture_output=True, text=True, timeout=3).stdout
            info["branch"], info["dirty"] = git("rev-parse", "--abbrev-ref", "HEAD").strip(), len(git("status", "--porcelain").splitlines())
        except Exception:
            info["branch"] = ""
        try:
            if self.mode == "coding":
                info["files"] = dashboard_modes._recent_files(7)
            elif self.mode == "studying":
                from jarvis_ui import aurora_utilities as au, system_control as sc
                info.update(minutes=au.minutes_today(), decks=au.list_decks(), notes=sc.read_notes(6))
        except Exception:
            pass
        self._info, self._busy = info, False

    def _refresh(self):
        if self._busy or time.time() - self._info_t < 3.0:
            return
        self._busy, self._info_t = True, time.time()
        threading.Thread(target=self._gather, daemon=True).start()

    # ---- drawing ----------------------------------------------------------------
    def _panel(self, x, y, w, hh, title, theme):
        self.h._draw_panel(x, y, w, hh, theme, chamfer=14)
        self.h._draw_terminal_header(x + 18, y + 16, title)
        return x + 18, y + 42

    def _text(self, lines, x, y, w, limit, color=(190, 225, 245), step=17):
        h = self.h
        for line in lines:
            for part in h._wrap_text(h.font_small, line, w):
                if y > limit - step:
                    return y
                h._blit_text(h.font_small, part, x, y, color=color)
                y += step
        return y

    def _label(self, text, x, y):
        self.h._blit_text(self.h.font_small, text, x, y, color=(90, 130, 160))
        return y + 20

    def draw(self, theme):
        h = self.h
        if self.mode == "standard" or getattr(getattr(h, "modes", None), "active", None) == "presentation":
            return
        self._refresh()
        hh = max(220, h.height - 92 - 112)
        getattr(self, "_" + self.mode)(theme, hh)

    def _coding(self, theme, hh):
        h, info = self.h, self._info
        x, y = self._panel(h.width - 316, 92, 300, hh, "CODE", theme)
        limit = 92 + hh
        branch = info.get("branch")
        y = self._text([f"branch {branch}   {info.get('dirty', 0)} changed" if branch else "no git repository"],
                       x, y, 264, limit, (210, 235, 255)) + 6
        y = self._label("RECENT FILES", x, y)
        files = [f"{time.strftime('%H:%M', time.localtime(t))}  {p}" for t, _, p in info.get("files", [])]
        y = self._text(files or ["(none yet)"], x, y, 264, limit) + 6
        y = self._label("CODE ACTIVITY", x, y)
        with h._event_log_lock:
            log = [l for l in h.event_log if "CODE:" in l or "SANDBOX:" in l][:4]
        y = self._text(log or ["(none yet)"], x, y, 264, limit) + 6
        y = self._label("TRY", x, y)
        self._text(['"write python code called ..."', '"edit <name> to ..."', '"test in sandbox: ..."',
                    '"why isn\'t this code working"'], x, y, 264, limit, (140, 180, 210))

    def _studying(self, theme, hh):
        h, info, v = self.h, self._info, self.voice
        notes_h = int(hh * 0.55)
        x, y = self._panel(16, 92, 230, notes_h, "NOTES", theme)
        self._text([f"- {n}" for n in info.get("notes", [])] or ['say "remember this: ..."'], x, y, 194, 92 + notes_h)
        ty = 92 + notes_h + 12
        x, y = self._panel(16, ty, 230, hh - notes_h - 12, "TIMER", theme)
        now, lines = time.time(), []
        for t in list(getattr(v, "active_timers", [])):
            m, s = divmod(max(0, int(t["ends_at"] - now)), 60)
            lines.append(f"{m:02d}:{s:02d}  {t['label']}")
        lines += [f"studied today: {info.get('minutes', 0)} min"] + ([] if lines else ['say "start a pomodoro"'])
        self._text(lines, x, y, 194, 92 + hh)
        x, y = self._panel(h.width - 316, 92, 300, hh, "REFERENCE", theme)
        limit, session = 92 + hh, getattr(getattr(v, "_productivity", None), "session", None)
        if session is not None and session.card:
            y = self._label(f"FLASHCARD {session.i + 1}/{len(session.cards)}  ({session.deck})", x, y)
            y = self._text(["Q: " + session.card["q"]] + (["A: " + session.card["a"]] if session.revealed else []),
                           x, y, 264, limit, (210, 235, 255)) + 6
        y = self._label("DECKS", x, y)
        decks = [f"{k}: {n} cards" for k, n in info.get("decks", {}).items()]
        y = self._text(decks or ['say "make 8 flashcards about ..."'], x, y, 264, limit) + 6
        y = self._label("TRY", x, y)
        self._text(['"quiz me on ..."', '"translate ... to Spanish"', '"summarize my notes"'], x, y, 264, limit, (140, 180, 210))

    def _gaming(self, theme, hh):
        h = self.h
        d = getattr(getattr(self.voice, "game_session", None), "latest", None) or {}
        x, y = self._panel(16, 92, 230, 310, "GAME HUD", theme)
        fps = d.get("fps")
        col = (120, 130, 140) if not isinstance(fps, (int, float)) else (60, 255, 140) if fps >= 50 else (255, 200, 80) if fps >= 30 else (255, 90, 80)
        h._blit_text(h.font_big, str(fps if fps is not None else "--"), x, y, color=col)
        y = self._label("FPS (Aurora render loop)", x, y + 34) + 4
        for label, key in (("CPU", "cpu"), ("RAM", "ram")):
            val = d.get(key)
            frac = val / 100 if isinstance(val, (int, float)) else None
            h._blit_text(h.font_small, label, x, y, color=(150, 200, 230))
            h._blit_text(h.font_small, f"{val}%" if frac is not None else "N/A", x + 150, y, color=(200, 235, 255))
            h._draw_segmented_bar(x, y + 18, 194, 7, frac or 0.0, h._stat_status_color(label, frac))
            y += 38
        for label, val in (("TEMP", f"{d.get('temp_c')} C" if d.get("temp_c") not in (None, "N/A") else "N/A"),
                           ("SESSION", d.get("session", "--")), ("APP", h._truncate(str(d.get("foreground", "--")), 14)),
                           ("REC", d.get("recording", "no"))):
            h._blit_text(h.font_small, label, x, y, color=(120, 160, 185))
            h._blit_text(h.font_small, str(val), x + 80, y, color=(255, 120, 110) if (label == "REC" and val == "yes") else (210, 235, 255))
            y += 20


def install(hologram, voice=None):
    """Attach the adaptive interface to a Hologram (idempotent) and return it."""
    ui = getattr(hologram, "adaptive_interface", None)
    if not isinstance(ui, AdaptiveInterface):
        ui = hologram.adaptive_interface = AdaptiveInterface(hologram, voice)
        hologram.overlays.append(ui.draw)
    if voice is not None:
        ui.voice = voice
    return ui


def handle_command(voice, text):
    """Voice entry point for voice_assistant.py. Returns True if handled."""
    m = _SWITCH.match(text.strip(" .!?,"))
    if not m:
        return False
    mode = ALIASES[m.group(1).lower()]
    install(voice.hologram, voice).switch(mode)
    voice._speak(REPLIES[mode])
    return True
