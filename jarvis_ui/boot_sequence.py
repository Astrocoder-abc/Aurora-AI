"""
Aurora V6.5 boot sequence: a frameless splash window shown BEFORE the main window opens, animated while the real
initialisation (imports, hand tracker, face recognition, voice) runs on a worker thread.

Look: deep-navy panel with a faint grid, rounded cyan frame, letter-spaced title, scrolling boot log with teal status
words, glowing reactor core with rotating dashed rings, and a progress bar with a moving knob.

    splash = BootSequence()                      # opens the window immediately
    splash.note("LOADING VOICE PIPELINE")        # thread-safe: adds a real stage to the log
    splash.run(lambda: worker_finished())        # animates (main thread) until init is done and the sequence has played
    splash.close()                               # closes the window so the main Aurora window can open
"""
import math
import os
import random
import threading
import time

import pygame
from OpenGL.GL import *

TITLE, VERSION, SUBTITLE = "AURORA", "V6.5", "UNIFIED INTELLIGENCE & AUTONOMOUS SYSTEMS"
W, H = 920, 594
SEQUENCE_SECONDS, LINE_START, LINE_STEP, FADE_OUT = 6.2, 0.75, 0.30, 0.45

BG = (0.015, 0.09, 0.27)
BG_FLASH = (0.03, 0.20, 0.65)               # the flat bright-blue panel seen in the first moments
CYAN = (0.12, 0.82, 1.0)
LABEL, STATUS, TITLE_COL = (170, 228, 255), (95, 245, 220), (125, 245, 225)

SCRIPT = [("PROCESSING CALENDAR SYNCHRONIZATION", "ACTIVE"), ("SIGNAL SMART HOME SERVICES", "SYNCHRONIZED"),
          ("UNIVERSAL AI KEYWORD LISTENER", "ARMED"), ("SPEECH AND AUDIO SYNTHETIC CORE", "ONLINE"),
          ("ENVIRONMENTAL CLIMATE MODULES", "ONLINE"), ("LOADING HIGH-FREQUENCY NLP TOKENIZER", "READY"),
          ("INITIALIZING MULTIMODAL CONTEXT STREAMS", "ACTIVE"), ("SYNCING LOCAL MEMORY VECTOR DATABASE", "COMPILED"),
          ("RESOLVING HARDWARE TEMPERATURE SENSORS", "NOMINAL"), ("BOOTING AUDIO PRE-PROCESSING FILTERS", "ACTIVE"),
          ("PARSING CUSTOM USER BEHAVIOR PATTERNS", "LOADED"), ("CALIBRATING GYROSCOPIC TAPPING SENSORS", "SYNCHRONIZED"),
          ("CONNECTING CLOUD REPLICATED MEMORY STACK", "SECURED"), ("OPTIMIZING CORE THREAD POOL PIPELINES", "STABLE")]
TAIL = ["CONFIGURING ENVIRONMENT", "LOADING POWER AURORA", "INITIALIZING GUI ENGINE", "LOADING UI LAYERS",
        "LOADING WORKSPACE", "ACTIVATING SYSTEMS"]


def _lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def _ramp(t, t0, t1):
    return max(0.0, min(1.0, (t - t0) / (t1 - t0)))


def _rounded(x, y, w, h, r, seg=8):
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        pts += [(cx + r * math.cos(math.radians(a0 + 90 * i / seg)), cy + r * math.sin(math.radians(a0 + 90 * i / seg)))
                for i in range(seg + 1)]
    return pts


def _line(pts, width, rgba, mode=GL_LINE_STRIP):
    glLineWidth(width)
    glColor4f(*rgba)
    glBegin(mode)
    for px, py in pts:
        glVertex2f(px, py)
    glEnd()


def _arc(cx, cy, r, a0, a1, width, rgba, steps=14):
    _line([(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
            cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)], width, rgba)


def _fan(cx, cy, r, inner, outer, seg=48):
    glBegin(GL_TRIANGLE_FAN)
    glColor4f(*inner)
    glVertex2f(cx, cy)
    glColor4f(*outer)
    for i in range(seg + 1):
        a = 2 * math.pi * i / seg
        glVertex2f(cx + r * math.cos(a), cy + r * math.sin(a))
    glEnd()


def _quad(x0, y0, x1, y1, rgba):
    glColor4f(*rgba)
    glBegin(GL_QUADS)
    glVertex2f(x0, y0); glVertex2f(x1, y0); glVertex2f(x1, y1); glVertex2f(x0, y1)
    glEnd()


class BootSequence:
    def __init__(self):
        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        pygame.display.init()
        pygame.font.init()
        try:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
        except Exception:
            pass
        pygame.display.set_mode((W, H), pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME)
        pygame.display.set_caption("AURORA V6.5")
        glViewport(0, 0, W, H)
        glMatrixMode(GL_PROJECTION); glLoadIdentity(); glOrtho(0, W, H, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW); glLoadIdentity()
        glDisable(GL_DEPTH_TEST)
        glEnable(GL_BLEND)
        glEnable(GL_LINE_SMOOTH)
        glEnable(GL_POINT_SMOOTH)
        mono = "consolas,couriernew,dejavusansmono,menlo,monospace"
        self.f_title = pygame.font.SysFont(mono, int(H * 0.075), bold=True)
        self.f_log = pygame.font.SysFont(mono, int(H * 0.028), bold=True)
        self.f_sub = pygame.font.SysFont(mono, int(H * 0.019), bold=True)
        self.f_status = pygame.font.SysFont(mono, int(H * 0.022), bold=True)
        self._tex, self._lock = {}, threading.Lock()
        self.lines, self._pending, self._script_i, self._tail_i = [], [], 0, 0      # lines: [text, status, born]
        self.scroll, self.progress, self.t0, self.quit = 0.0, 0.0, time.time(), False
        rnd = random.Random(7)
        self.dots = [(rnd.uniform(0.04, 0.96), rnd.uniform(0.08, 0.9), rnd.uniform(1.2, 2.6), rnd.uniform(0, 6.28)) for _ in range(42)]
        self.clock = pygame.time.Clock()

    # ---- public API -------------------------------------------------------------
    def note(self, text):
        """Thread-safe: shows a real initialisation stage as a boot-log line."""
        with self._lock:
            self._pending.append(text.upper().rstrip(". ") )

    def run(self, is_done):
        """Animates until is_done() is true and the sequence has finished, then fades out."""
        end = None
        while not self.quit:
            t = time.time() - self.t0
            if end is None and is_done() and t >= SEQUENCE_SECONDS and self._tail_i >= len(TAIL):
                end = time.time()
            fade = 0.0 if end is None else (time.time() - end) / FADE_OUT
            self._frame(t, fade, done=end is not None)
            if fade >= 1.0:
                break

    def close(self):
        self._tex.clear()
        pygame.display.quit()

    # ---- text ---------------------------------------------------------------------
    def _blit(self, font, s, x, y, rgb, a=1.0):
        key = (id(font), s, rgb)
        if key not in self._tex:
            surf = font.render(s, True, rgb)
            w, h = surf.get_size()
            tex = glGenTextures(1)
            glBindTexture(GL_TEXTURE_2D, tex)
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, pygame.image.tostring(surf, "RGBA", False))
            self._tex[key] = (tex, w, h)
        tex, w, h = self._tex[key]
        x, y = round(x), round(y)
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, tex)
        glColor4f(1, 1, 1, a)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex2f(x, y)
        glTexCoord2f(1, 0); glVertex2f(x + w, y)
        glTexCoord2f(1, 1); glVertex2f(x + w, y + h)
        glTexCoord2f(0, 1); glVertex2f(x, y + h)
        glEnd()
        glDisable(GL_TEXTURE_2D)
        return w

    def _spaced(self, font, s, x, y, rgb, a, gap):
        for ch in s:
            x += self._blit(font, ch, x, y, rgb, a) + gap

    # ---- frame --------------------------------------------------------------------------
    def _frame(self, t, fade, done):
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                self.quit = True
        self._advance(t, done)
        glClearColor(0, 0, 0, 1)
        glClear(GL_COLOR_BUFFER_BIT)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        win = _ramp(t, 0.0, 0.4)                                     # window fades in from translucent dark blue
        flash = 1.0 - _ramp(t, 0.9, 2.0)                             # bright flat blue settles to deep navy
        bg = tuple(_lerp(BG[i], BG_FLASH[i], flash) for i in range(3))
        _quad(0, 0, W, H, (*bg, 0.55 + 0.45 * win))
        self._grid(_ramp(t, 1.0, 2.0))
        self._dots(t, _ramp(t, 1.2, 2.2))
        self._frame_lines(_ramp(t, 0.7, 1.7), t)
        if t > 0.5:
            c = _ramp(t, 0.5, 0.9)
            self._spaced(self.f_title, TITLE, W * 0.06, H * 0.055, TITLE_COL, c, 3)
            self._spaced(self.f_title, VERSION, W * 0.06 + self.f_title.size(TITLE)[0] + len(TITLE) * 3 + 22, H * 0.055, TITLE_COL, c, 3)
            self._blit(self.f_sub, SUBTITLE, W * 0.062, H * 0.155, (150, 200, 235), c)
        self._log(t)
        cx = W * 0.78                                                # reactor stays fully inside the window
        self._reactor(cx, H * 0.47, min(H * 0.36, (W - cx - 30) / 1.02), t, _ramp(t, 0.5, 1.8))
        self._progress(t, done)
        if fade > 0:
            _quad(0, 0, W, H, (0, 0, 0, min(1.0, fade)))
        pygame.display.flip()
        self.clock.tick(60)

    def _advance(self, t, done):
        now = time.time()
        while self._script_i < len(SCRIPT) and t >= LINE_START + self._script_i * LINE_STEP:
            self.lines.append([SCRIPT[self._script_i][0] + "...", SCRIPT[self._script_i][1], now])
            self._script_i += 1
        if self._script_i >= len(SCRIPT):
            with self._lock:
                real, self._pending = self._pending, []
            for s in real:
                self.lines.append([s + "...", "", now])
            due = LINE_START + (len(SCRIPT) + self._tail_i) * LINE_STEP
            if self._tail_i < len(TAIL) and t >= due:
                self.lines.append([TAIL[self._tail_i] + "...", "", now])
                self._tail_i += 1
        target = len(self.lines)
        self.scroll += (target - self.scroll) * 0.18
        cap = 1.0 if done else 0.96
        self.progress += (min(cap, t / SEQUENCE_SECONDS) - self.progress) * 0.12

    # ---- layers --------------------------------------------------------------------------
    def _grid(self, a):
        if a <= 0:
            return
        glLineWidth(1.0)
        glBegin(GL_LINES)
        step = 22
        for i, x in enumerate(range(0, W + 1, step)):
            glColor4f(0.25, 0.55, 0.95, (0.20 if i % 4 == 0 else 0.10) * a)
            glVertex2f(x, 0); glVertex2f(x, H)
        for i, y in enumerate(range(0, H + 1, step)):
            glColor4f(0.25, 0.55, 0.95, (0.20 if i % 4 == 0 else 0.10) * a)
            glVertex2f(0, y); glVertex2f(W, y)
        glEnd()

    def _dots(self, t, a):
        if a <= 0:
            return
        for fx, fy, r, ph in self.dots:
            glColor4f(0.35, 0.85, 1.0, (0.35 + 0.35 * math.sin(t * 1.5 + ph)) * a)
            glPointSize(r * 2)
            glBegin(GL_POINTS)
            glVertex2f(fx * W, fy * H)
            glEnd()

    def _frame_lines(self, a, t):
        if a <= 0:
            return
        m = 9
        path = _rounded(m, m, W - 2 * m, H - 2 * m, 20)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        for width, al in ((9, 0.06), (5, 0.12)):                       # glow
            _line(path, width, (*CYAN, al * a), GL_LINE_LOOP)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _line(path, 2.6, (*CYAN, 0.95 * a), GL_LINE_LOOP)
        _line(_rounded(m + 7, m + 7, W - 2 * m - 14, H - 2 * m - 14, 14), 1.0, (*CYAN, 0.45 * a), GL_LINE_LOOP)
        glLineWidth(1.5)                                                # top-right notch with ticks
        glColor4f(*CYAN, 0.8 * a)
        glBegin(GL_LINES)
        x0 = W * 0.72
        glVertex2f(x0, m + 7); glVertex2f(W * 0.95, m + 7)
        for i in range(12):
            glVertex2f(x0 + i * 12, m + 7); glVertex2f(x0 + i * 12, m + (16 if i % 3 == 0 else 12))
        glEnd()
        _quad(m - 1, H * 0.42, m + 6, H * 0.46, (*CYAN, 0.9 * a))        # left tab marker
        _line([(m + 6, H * 0.30), (m + 6, H * 0.56)], 1.5, (*CYAN, 0.8 * a))

    def _log(self, t):
        rows, step, y0, x = 12, H * 0.034, H * 0.255, W * 0.062
        top = max(0.0, self.scroll - rows)               # fills from the top, then scrolls
        now = time.time()
        for i, (text, status, born) in enumerate(self.lines):
            rel = i - top
            if rel < -0.5 or rel > rows - 0.6:
                continue
            a = (min(1.0, max(0.0, rel / 2.0 + 0.3)) if top > 0 else 1.0) * min(1.0, (now - born) / 0.18)
            y = y0 + rel * step
            w = self._blit(self.f_log, text, x, y, LABEL, a)
            if status:
                self._blit(self.f_log, " " + status, x + w, y, STATUS, a)

    def _reactor(self, cx, cy, R, t, a):
        if a <= 0:
            return
        s = 1 - (1 - a) ** 3
        R *= s
        pulse = 1.0 + 0.05 * math.sin(t * 2.6)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        _fan(cx, cy, R * 0.55, (0.1, 0.5, 1.0, 0.30 * a), (0.1, 0.5, 1.0, 0.0))
        _fan(cx, cy, R * 0.26 * pulse, (0.35, 0.92, 1.0, 0.95 * a), (0.1, 0.55, 1.0, 0.0))
        _fan(cx, cy, R * 0.13 * pulse, (1.0, 1.0, 1.0, a), (0.5, 0.95, 1.0, 0.0))
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        for rf, w, al in ((0.36, 1.6, 0.9), (0.75, 1.0, 0.7), (1.0, 1.0, 0.25)):                  # solid rings
            _arc(cx, cy, R * rf, 0, 360, w, (*CYAN, al * a), 96)
        for rf, speed, n, span, width in ((0.62, 28, 8, 28, 3.2), (0.88, -18, 14, 12, 2.2)):      # rotating dashed rings
            for j in range(n):
                a0 = t * speed + j * 360 / n
                _arc(cx, cy, R * rf, a0, a0 + span, width, (*CYAN, 0.95 * a), 8)
        glLineWidth(1.0)
        glColor4f(*CYAN, 0.35 * a)
        glBegin(GL_LINES)
        for i in range(90):
            ang = math.radians(i * 4 - t * 5)
            ln = 9 if i % 5 == 0 else 4
            glVertex2f(cx + R * 0.96 * math.cos(ang), cy + R * 0.96 * math.sin(ang))
            glVertex2f(cx + (R * 0.96 + ln) * math.cos(ang), cy + (R * 0.96 + ln) * math.sin(ang))
        glEnd()

    def _progress(self, t, done):
        a = _ramp(t, 0.9, 1.6)
        if a <= 0:
            return
        x0, x1, y = W * 0.05, W * 0.95, H * 0.885
        head = x0 + (x1 - x0) * self.progress
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        _line([(x0, y), (head, y)], 9, (*CYAN, 0.12 * a))
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        _line([(x0, y), (head, y)], 4.0, (*CYAN, 0.95 * a))
        _fan(head, y, 8, (1, 1, 1, a), (*CYAN, 0.0))
        glColor4f(0.8, 1.0, 1.0, a)
        glPointSize(7)
        glBegin(GL_POINTS)
        glVertex2f(head, y)
        glEnd()
        label = (self.lines[-1][0] if self.lines else "INITIALIZING CORE GRAPHIC CONFIG...")
        self._blit(self.f_status, label, W * 0.05, H * 0.915, (80, 215, 255), a)
        _line(_rounded(W * 0.80, H * 0.93, W * 0.14, H * 0.03, 7, 4), 1.5, (*CYAN, 0.9 * a), GL_LINE_LOOP)
