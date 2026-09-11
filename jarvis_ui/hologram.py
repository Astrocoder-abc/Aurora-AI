"""
Jarvis-style dashboard: a central holographic display surrounded by
dashboard chrome (top bar, system panel, event log, bottom status bar,
grid, glow). The central display can show:
  - "demo": the original 4-ring generic hologram
  - "atom": a real Bohr-model atom — correct number of electron shells
    and electrons per shell for whatever element was requested
  - "solar_system": the sun + planets, reusing the same orbit engine

EDITABLE: point to select the next orbit/shell, pinch + move to reshape
it (vertical = radius, horizontal = spin speed).

FULLSCREEN: launches windowed by default. Press F11 to toggle back to
a windowed view, ESC to quit. (Previous versions never processed pygame's
window events at all, which is almost certainly why the window sometimes
showed "Not Responding" in Windows — fixed here.)
"""

import math
import ctypes
from copy import deepcopy
import random
import time
import pygame
from pygame.locals import DOUBLEBUF, OPENGL, FULLSCREEN
from OpenGL.GL import *
from OpenGL.GLU import gluPerspective
from collections import deque, OrderedDict
import threading

from jarvis_ui.particle_core import ParticleCore
from jarvis_ui.core_animation import CoreAnimation
from jarvis_ui.particle_buffers import build_buffers, NEBULA_PALETTE
from jarvis_ui.overlays import overlay_rect, visible_page
from jarvis_ui.runtime import UI_BUILD, window_size

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

COLOR_LISTENING = (0.1, 1.0, 0.9)
COLOR_SPEAKING = (0.35, 0.85, 1.0)
COLOR_THINKING = (0.65, 0.4, 1.0)

THEMES = [
    (1.0, 0.56, 0.06),
    (0.1, 0.6, 1.0),
    (0.8, 0.2, 1.0),
    (1.0, 0.2, 0.4),
]

# Subsystem graph drawn across the whole screen around the voice core — a
# live constellation of the assistant's modules, in the style of the supplied
# reference dashboards. (label, fx, fy, family, gear): fx/fy are normalized
# screen positions; gear=True draws the radial "flower" cluster, otherwise a
# small tan terminal dot. Families map to colors.
GRAPH_FAMILIES = {
    "cyan": (0.25, 0.95, 0.85),
    "green": (0.45, 1.0, 0.55),
    "gold": (1.0, 0.78, 0.25),
    "pink": (1.0, 0.4, 0.65),
    "violet": (0.68, 0.45, 1.0),
    "tan": (0.82, 0.72, 0.55),
}
GRAPH_NODES = [
    ("VOICE LINK", 0.34, 0.17, "violet", True),
    ("WAKE WORD", 0.55, 0.27, "tan", False),
    ("PARTICLE FLOW", 0.66, 0.13, "cyan", True),
    ("HAND TRACKER", 0.84, 0.28, "cyan", True),
    ("OPEN-METEO", 0.78, 0.40, "tan", False),
    ("WEATHER DOCK", 0.87, 0.50, "gold", False),
    ("GROQ BRIDGE", 0.70, 0.44, "tan", False),
    ("TIMER SERVICE", 0.74, 0.76, "green", True),
    ("EDGE TTS", 0.60, 0.60, "tan", False),
    ("MEDIA CONTROL", 0.47, 0.82, "pink", True),
    ("SNAPSHOTS", 0.30, 0.74, "tan", False),
    ("FACE ID", 0.20, 0.66, "cyan", False),
    ("ADB PHONE", 0.13, 0.55, "tan", False),
    ("SYSTEM STATS", 0.12, 0.38, "gold", True),
    ("STAR CHART", 0.24, 0.45, "tan", False),
    ("SHAPE LIB", 0.35, 0.55, "tan", False),
    ("CALC CORE", 0.27, 0.64, "tan", False),
    ("US MAP", 0.17, 0.45, "green", False),
    ("EVENT LOG", 0.43, 0.68, "tan", False),
    ("THEME ENGINE", 0.62, 0.33, "pink", False),
]
# Extra constellation edges between node indices (besides hub spokes).
GRAPH_CROSS_LINKS = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7),
                     (7, 8), (8, 9), (9, 10), (10, 11), (11, 12), (12, 13),
                     (13, 14), (14, 15), (15, 16), (16, 17), (17, 11),
                     (18, 15), (18, 8), (19, 1), (19, 6), (0, 13), (9, 18)]

FLASH_COLORS = {
    "thumbs_up": (0.2, 1.0, 0.3),
    "thumbs_down": (1.0, 0.15, 0.15),
    "rock_sign": (0.8, 0.1, 1.0),
    "ok_sign": (0.2, 0.9, 1.0),
    "peace": (1.0, 1.0, 1.0),
    "throw": (1.0, 0.7, 0.1),
    "point": (1.0, 1.0, 1.0),
}

DEFAULT_ORBITS = [
    {"radius": 1.6, "tilt": 0, "speed": 35, "electrons": [0]},
    {"radius": 1.3, "tilt": 90, "speed": -50, "electrons": [60]},
    {"radius": 1.0, "tilt": 45, "speed": 70, "electrons": [120]},
    {"radius": 0.7, "tilt": -45, "speed": -90, "electrons": [200]},
]

# Full periodic table (atomic number, symbol, name). Electron shells are
# generated generically via fill_shells() below (simplified 2-8-8-18-18-32
# Bohr-model rule) rather than hand-written per element — accurate enough
# for a stylized hologram, and it means every one of the 118 elements
# works, not just a hand-picked subset.
PERIODIC_TABLE = [
    (1, "H", "hydrogen"), (2, "He", "helium"), (3, "Li", "lithium"),
    (4, "Be", "beryllium"), (5, "B", "boron"), (6, "C", "carbon"),
    (7, "N", "nitrogen"), (8, "O", "oxygen"), (9, "F", "fluorine"),
    (10, "Ne", "neon"), (11, "Na", "sodium"), (12, "Mg", "magnesium"),
    (13, "Al", "aluminum"), (14, "Si", "silicon"), (15, "P", "phosphorus"),
    (16, "S", "sulfur"), (17, "Cl", "chlorine"), (18, "Ar", "argon"),
    (19, "K", "potassium"), (20, "Ca", "calcium"), (21, "Sc", "scandium"),
    (22, "Ti", "titanium"), (23, "V", "vanadium"), (24, "Cr", "chromium"),
    (25, "Mn", "manganese"), (26, "Fe", "iron"), (27, "Co", "cobalt"),
    (28, "Ni", "nickel"), (29, "Cu", "copper"), (30, "Zn", "zinc"),
    (31, "Ga", "gallium"), (32, "Ge", "germanium"), (33, "As", "arsenic"),
    (34, "Se", "selenium"), (35, "Br", "bromine"), (36, "Kr", "krypton"),
    (37, "Rb", "rubidium"), (38, "Sr", "strontium"), (39, "Y", "yttrium"),
    (40, "Zr", "zirconium"), (41, "Nb", "niobium"), (42, "Mo", "molybdenum"),
    (43, "Tc", "technetium"), (44, "Ru", "ruthenium"), (45, "Rh", "rhodium"),
    (46, "Pd", "palladium"), (47, "Ag", "silver"), (48, "Cd", "cadmium"),
    (49, "In", "indium"), (50, "Sn", "tin"), (51, "Sb", "antimony"),
    (52, "Te", "tellurium"), (53, "I", "iodine"), (54, "Xe", "xenon"),
    (55, "Cs", "cesium"), (56, "Ba", "barium"), (57, "La", "lanthanum"),
    (58, "Ce", "cerium"), (59, "Pr", "praseodymium"), (60, "Nd", "neodymium"),
    (61, "Pm", "promethium"), (62, "Sm", "samarium"), (63, "Eu", "europium"),
    (64, "Gd", "gadolinium"), (65, "Tb", "terbium"), (66, "Dy", "dysprosium"),
    (67, "Ho", "holmium"), (68, "Er", "erbium"), (69, "Tm", "thulium"),
    (70, "Yb", "ytterbium"), (71, "Lu", "lutetium"), (72, "Hf", "hafnium"),
    (73, "Ta", "tantalum"), (74, "W", "tungsten"), (75, "Re", "rhenium"),
    (76, "Os", "osmium"), (77, "Ir", "iridium"), (78, "Pt", "platinum"),
    (79, "Au", "gold"), (80, "Hg", "mercury"), (81, "Tl", "thallium"),
    (82, "Pb", "lead"), (83, "Bi", "bismuth"), (84, "Po", "polonium"),
    (85, "At", "astatine"), (86, "Rn", "radon"), (87, "Fr", "francium"),
    (88, "Ra", "radium"), (89, "Ac", "actinium"), (90, "Th", "thorium"),
    (91, "Pa", "protactinium"), (92, "U", "uranium"), (93, "Np", "neptunium"),
    (94, "Pu", "plutonium"), (95, "Am", "americium"), (96, "Cm", "curium"),
    (97, "Bk", "berkelium"), (98, "Cf", "californium"), (99, "Es", "einsteinium"),
    (100, "Fm", "fermium"), (101, "Md", "mendelevium"), (102, "No", "nobelium"),
    (103, "Lr", "lawrencium"), (104, "Rf", "rutherfordium"), (105, "Db", "dubnium"),
    (106, "Sg", "seaborgium"), (107, "Bh", "bohrium"), (108, "Hs", "hassium"),
    (109, "Mt", "meitnerium"), (110, "Ds", "darmstadtium"), (111, "Rg", "roentgenium"),
    (112, "Cn", "copernicium"), (113, "Nh", "nihonium"), (114, "Fl", "flerovium"),
    (115, "Mc", "moscovium"), (116, "Lv", "livermorium"), (117, "Ts", "tennessine"),
    (118, "Og", "oganesson"),
]

ELEMENTS = {name: (symbol, atomic_number) for atomic_number, symbol, name in PERIODIC_TABLE}

# Precise most-common-isotope neutron counts for the elements people ask
# about most often; everything else falls back to an approximation (the
# real neutron:proton ratio drifts from ~1:1 for light elements toward
# ~1.5:1 for heavy ones — close enough for a stylized nucleus, and the
# nucleon-cluster render is capped/representative past ~40 particles
# anyway, so exact precision there wouldn't be visible even if hand-typed).
NEUTRONS = {
    "hydrogen": 0, "helium": 2, "lithium": 4, "beryllium": 5, "boron": 6,
    "carbon": 6, "nitrogen": 7, "oxygen": 8, "fluorine": 10, "neon": 10,
    "sodium": 12, "magnesium": 12, "aluminum": 14, "silicon": 14,
    "phosphorus": 16, "sulfur": 16, "chlorine": 18, "argon": 22,
    "potassium": 20, "calcium": 20, "iron": 30, "copper": 35, "gold": 118,
    "silver": 61, "lead": 125, "uranium": 146, "mercury": 121, "tin": 69,
    "platinum": 117, "titanium": 26, "zinc": 35, "nickel": 31, "tungsten": 110,
}


def _approx_neutrons(protons):
    if protons <= 20:
        return protons
    ratio = 1.0 + 0.5 * min(1.0, (protons - 20) / 80)
    return round(protons * ratio)


# Reverse lookup so a custom proton count that happens to match a real
# element can be labeled with its name/symbol.
ATOMIC_NUMBER_TO_ELEMENT = {
    atomic_number: (symbol, name) for atomic_number, symbol, name in PERIODIC_TABLE
}

# Simplified shell capacities (2-8-8-18-18-32 rule) used to build electron
# shells for ANY electron count, not just the known elements above — this
# is what lets custom/edited "elements" work.
SHELL_CAPACITIES = [2, 8, 8, 18, 18, 32]


def fill_shells(electron_count):
    shells = []
    remaining = max(0, electron_count)
    for cap in SHELL_CAPACITIES:
        if remaining <= 0:
            break
        take = min(cap, remaining)
        shells.append(take)
        remaining -= take
    if remaining > 0:
        shells.append(remaining)
    return shells

PLANETS = [
    ("Mercury", 0.55, 140), ("Venus", 0.75, 100), ("Earth", 0.95, 80),
    ("Mars", 1.15, 65), ("Jupiter", 1.5, 40), ("Saturn", 1.8, 30),
    ("Uranus", 2.05, -22), ("Neptune", 2.3, 18),
]

# Curated real star systems with their actual (approximate) known planet
# counts/naming. Anything NOT in this list still works — see
# _generate_procedural_system — so "every star system" is handled either
# with real data (for well-known ones) or a consistent generated layout
# (for anything else, including made-up names).
STAR_SYSTEMS = {
    "trappist-1": [(f"TRAPPIST-1{c}", 0.5 + i * 0.28, 90 - i * 10) for i, c in enumerate("bcdefgh")],
    "kepler-90": [(f"Kepler-90{c}", 0.5 + i * 0.28, 100 - i * 10) for i, c in enumerate("bcdefghi")],
    "proxima centauri": [("Proxima b", 0.6, 70), ("Proxima c", 1.3, -30), ("Proxima d", 0.4, 130)],
    "alpha centauri": [("Alpha Centauri Bb", 0.6, 85)],
    "55 cancri": [(f"55 Cancri {c}", 0.5 + i * 0.3, 95 - i * 12) for i, c in enumerate("bcdef")],
    "gliese 581": [(f"Gliese 581{c}", 0.5 + i * 0.3, 90 - i * 10) for i, c in enumerate("bcde")],
    "upsilon andromedae": [(f"Upsilon Andromedae {c}", 0.5 + i * 0.32, 80 - i * 12) for i, c in enumerate("bcd")],
    "hd 10180": [(f"HD 10180 {c}", 0.5 + i * 0.26, 100 - i * 9) for i, c in enumerate("bcdefgh")],
}


def _generate_procedural_system(name):
    """Deterministic 'fake' star system for any name not in the curated
    list above — same name always generates the same layout, so it feels
    consistent rather than random each time."""
    seed = sum(ord(c) for c in name) if name else 42
    rng = random.Random(seed)
    count = rng.randint(2, 6)
    planets = []
    for i in range(count):
        radius = 0.5 + i * 0.32 + rng.uniform(-0.04, 0.04)
        speed = rng.uniform(20, 130) * rng.choice([1, -1])
        planets.append((f"{name.title()} {chr(ord('b') + i)}", radius, speed))
    return planets

# Real chemistry shell naming convention (K, L, M, N, O, P, Q for shells
# 1-7) — used for the atom display's shell composition readout.
SHELL_NAMES = ["K", "L", "M", "N", "O", "P", "Q"]

# Distinct color per electron shell so the atom reads as layered structure
# rather than flat same-colored rings.
SHELL_COLORS = [
    (0.3, 0.65, 1.0), (0.3, 1.0, 0.6), (1.0, 0.65, 0.25),
    (0.8, 0.35, 1.0), (1.0, 0.35, 0.5), (0.35, 1.0, 1.0), (1.0, 0.9, 0.3),
]

SHAPES = {"sphere", "cube", "torus", "pyramid", "cylinder", "eiffel tower", "skyscraper", "dna"}

# Alternate phrasings that map onto one of the models above — these are
# stylized representations, not architecturally distinct per building, so
# well-known skyscrapers all render as the generic skyscraper model with
# their own label, rather than needing bespoke geometry for each one.
SHAPE_ALIASES = {
    "eiffel": "eiffel tower",
    "tower eiffel": "eiffel tower",
    "burj khalifa": "skyscraper",
    "empire state building": "skyscraper",
    "empire state": "skyscraper",
    "building": "skyscraper",
    "tower block": "skyscraper",
    "world trade center": "skyscraper",
    "one world trade center": "skyscraper",
    "double helix": "dna",
    "helix": "dna",
    "dna strand": "dna",
    "dna helix": "dna",
}

# Stylized (not cartographically precise) US state layout, normalized 0..1
# so it can be scaled into any panel size. Good enough to be immediately
# recognizable as "a US map" with the right state glowing, which is the
# actual goal here rather than GIS-grade accuracy.
US_STATE_POSITIONS = {
    "washington": (0.10, 0.08), "oregon": (0.09, 0.20), "california": (0.08, 0.42),
    "nevada": (0.16, 0.35), "idaho": (0.20, 0.18), "montana": (0.28, 0.10),
    "wyoming": (0.28, 0.25), "utah": (0.22, 0.38), "colorado": (0.32, 0.38),
    "arizona": (0.20, 0.55), "new mexico": (0.30, 0.55),
    "north dakota": (0.38, 0.10), "south dakota": (0.38, 0.22), "nebraska": (0.38, 0.32),
    "kansas": (0.40, 0.42), "oklahoma": (0.42, 0.55), "texas": (0.40, 0.70),
    "minnesota": (0.48, 0.12), "iowa": (0.48, 0.28), "missouri": (0.48, 0.42),
    "arkansas": (0.48, 0.55), "louisiana": (0.48, 0.70),
    "wisconsin": (0.54, 0.18), "illinois": (0.55, 0.32), "michigan": (0.60, 0.20),
    "indiana": (0.58, 0.32), "ohio": (0.62, 0.30), "kentucky": (0.58, 0.42),
    "tennessee": (0.56, 0.48), "mississippi": (0.52, 0.62), "alabama": (0.56, 0.62),
    "florida": (0.66, 0.85), "georgia": (0.62, 0.62), "south carolina": (0.66, 0.58),
    "north carolina": (0.68, 0.52), "virginia": (0.70, 0.44), "west virginia": (0.66, 0.40),
    "maryland": (0.72, 0.42), "delaware": (0.75, 0.42), "pennsylvania": (0.70, 0.32),
    "new jersey": (0.75, 0.36), "new york": (0.72, 0.22), "connecticut": (0.78, 0.28),
    "rhode island": (0.80, 0.28), "massachusetts": (0.79, 0.24), "vermont": (0.76, 0.16),
    "new hampshire": (0.78, 0.16), "maine": (0.82, 0.08),
    "alaska": (0.04, 0.90), "hawaii": (0.14, 0.92),
    "district of columbia": (0.715, 0.435),
}

US_STATE_ABBREV = {
    "washington": "WA", "oregon": "OR", "california": "CA", "nevada": "NV",
    "idaho": "ID", "montana": "MT", "wyoming": "WY", "utah": "UT",
    "colorado": "CO", "arizona": "AZ", "new mexico": "NM", "north dakota": "ND",
    "south dakota": "SD", "nebraska": "NE", "kansas": "KS", "oklahoma": "OK",
    "texas": "TX", "minnesota": "MN", "iowa": "IA", "missouri": "MO",
    "arkansas": "AR", "louisiana": "LA", "wisconsin": "WI", "illinois": "IL",
    "michigan": "MI", "indiana": "IN", "ohio": "OH", "kentucky": "KY",
    "tennessee": "TN", "mississippi": "MS", "alabama": "AL", "florida": "FL",
    "georgia": "GA", "south carolina": "SC", "north carolina": "NC",
    "virginia": "VA", "west virginia": "WV", "maryland": "MD", "delaware": "DE",
    "pennsylvania": "PA", "new jersey": "NJ", "new york": "NY", "connecticut": "CT",
    "rhode island": "RI", "massachusetts": "MA", "vermont": "VT",
    "new hampshire": "NH", "maine": "ME", "alaska": "AK", "hawaii": "HI",
    "district of columbia": "DC",
}

RADIUS_MIN, RADIUS_MAX = 0.35, 2.6
SPEED_MIN, SPEED_MAX = -180, 180


class Hologram:
    def __init__(self, fullscreen=False, windowed_size=(960, 640), windowed_only=True):
        pygame.init()
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except pygame.error:
            pass  # Audio is optional; keep the visual dashboard available.
        self.clock = pygame.time.Clock()
        self.particle_core = ParticleCore()
        self.core_animation = CoreAnimation()
        self.particle_quality = "balanced"
        self._core_dock = 0.0
        self._text_cache = OrderedDict()
        self.show_diagnostics = False
        self.reduced_motion = False
        self.voice_available = False
        self.last_heard = ""
        self.weather_status = None
        self.show_help = False
        self.info_scroll = 0
        self._info_page_lines = 5
        self._stats_sample_time = -1.0
        self._stats_cache = []
        self.windowed_size = windowed_size
        self.windowed_only = windowed_only
        self.fullscreen = bool(fullscreen and not windowed_only)
        self._set_display_mode(self.fullscreen)

        # Bahnschrift/Agency FB have a geometric, technical HUD look closer
        # to sci-fi interfaces than a plain monospace font — both ship
        # with Windows, so this is a free upgrade with no install needed.
        # Falls back gracefully through the list, ending at Consolas.
        font_candidates = ["segoeui", "dejavusans", "bahnschrift", "arial"]

        def _pick_font(size, bold=False):
            for name in font_candidates:
                try:
                    if not pygame.font.match_font(name):
                        continue
                    f = pygame.font.SysFont(name, size, bold=bold)
                    if f:
                        return f
                except Exception:
                    continue
            return pygame.font.Font(None, size)

        try:
            self.font = _pick_font(16)
            self.font_big = _pick_font(28, bold=True)
            self.font_small = _pick_font(13)
            self.font_huge = _pick_font(54, bold=True)
        except Exception:
            self.font = self.font_big = self.font_small = self.font_huge = None

        # Twinkling starfield — same seed every run, so it's not distracting
        # random noise each frame, just a fixed field of stars that twinkle.
        # Dense enough to read as deep space, with a few faint constellation
        # links between nearby stars (reference constellation dashboard).
        _star_rng = random.Random(7)
        self.stars = [
            (_star_rng.random(), _star_rng.random(), _star_rng.uniform(0, 6.28), _star_rng.choice([1, 1, 2, 2, 3]))
            for _ in range(420)
        ]
        self.star_links = []
        for i in range(0, len(self.stars) - 1, 7):
            ax, ay = self.stars[i][0], self.stars[i][1]
            bx, by = self.stars[i + 1][0], self.stars[i + 1][1]
            if math.hypot(ax - bx, ay - by) < 0.16:
                self.star_links.append((i, i + 1))

        # Uptime counter shown in the top-right stats block.
        self._start_time = time.time()

        self.rotation_x = 0.0
        self.rotation_y = 0.0
        self.roll = 0.0
        self.translate_x = 0.0
        self.translate_y = 0.0
        self.zoom = 1.0
        self.brightness = 1.0

        self.pulse_phase = 0.0
        self.sweep_phase = 0.0
        self.scanline_phase = 0.0
        self.elapsed = 0.0

        self.flash_color = (1.0, 1.0, 1.0)
        self.flash_intensity = 0.0

        self.theme_index = 0
        self._theme_prev_color = THEMES[0]
        self._theme_transition_start = -999.0  # already-complete, no transition on first frame
        self._theme_transition_duration = 0.6
        self.state = "idle"

        self.mode = "empty"
        self.mode_label = "AWAITING COMMAND"
        self.shape_name = "sphere"
        self.orbits = []
        self.selected_index = None
        self.protons = 1
        self.neutrons = 0
        self.electron_count = 1
        self.shell_summary = ""
        self.current_element_z = 1
        self.current_star_index = 0

        self.hud_lines = []
        self.event_log = deque(maxlen=6)
        self._event_log_lock = threading.Lock()
        self._last_time = time.time()
        self._fps = 0.0
        self.should_quit = False

        # Materialize transition: whenever content changes, it grows in
        # from nothing rather than snapping instantly into place.
        self._materialize_start = 0.0
        self._materialize_duration = 0.45

        # Sparkline history for the system panel (CPU% over the last
        # ~20 seconds), sampled once per frame in update().
        self.cpu_history = deque([0.0] * 40, maxlen=40)

        self.docked = False       # True -> hologram shifts left, widget panel gets the right side
        self._dock_shift = 0.0    # smoothed offset, eases toward the docked/undocked target
        self.weather = None       # dict set by show_weather(), or None
        self.map_highlight = None # US state key, drawn instead of the weather icon when set
        self.info_card = None     # {"question": str, "answer": str} for general Q&A, or None

        if PSUTIL_AVAILABLE:
            psutil.cpu_percent(interval=None)  # first call always returns 0.0 — prime it now

    # ---- display mode / fullscreen -----------------------------------------

    def _set_display_mode(self, fullscreen):
        fullscreen = fullscreen and not getattr(self, "windowed_only", True)
        if fullscreen:
            info = pygame.display.Info()
            self.width, self.height = info.current_w, info.current_h
            flags = DOUBLEBUF | OPENGL | FULLSCREEN
        else:
            info = pygame.display.Info()
            self.width, self.height = window_size((info.current_w, info.current_h), self.windowed_size)
            flags = DOUBLEBUF | OPENGL

        try:
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 1)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 4)
        except Exception:
            pass  # MSAA unsupported on this GPU/driver — fine, just less smooth

        try:
            pygame.display.set_mode((self.width, self.height), flags)
        except pygame.error:
            # Unsupported MSAA usually fails at set_mode, not gl_set_attribute.
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLEBUFFERS, 0)
            pygame.display.gl_set_attribute(pygame.GL_MULTISAMPLESAMPLES, 0)
            pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption(f"AURORA — Nebula Core | {UI_BUILD}")
        self._init_gl_state()

    def _init_gl_state(self):
        glClearColor(0.006, 0.005, 0.004, 1.0)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_LINE_SMOOTH)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        try:
            glEnable(GL_MULTISAMPLE)
        except Exception:
            pass

        # Depth fog: without any shading, a wireframe object can look
        # flat from certain angles since the outline is the only depth
        # cue. Fading distant lines toward the background color gives a
        # real 3D depth signal from ANY viewing angle, not just ones
        # where the silhouette happens to reveal volume.
        try:
            glEnable(GL_FOG)
            glFogi(GL_FOG_MODE, GL_LINEAR)
            glFogfv(GL_FOG_COLOR, (0.006, 0.005, 0.004, 1.0))
            glFogf(GL_FOG_START, 4.0)
            glFogf(GL_FOG_END, 11.0)
            glHint(GL_FOG_HINT, GL_NICEST)
        except Exception:
            pass

        glViewport(0, 0, self.width, self.height)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        gluPerspective(45, (self.width / self.height), 0.1, 50.0)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glTranslatef(0.0, 0.0, -6)

    def toggle_fullscreen(self):
        if self.windowed_only:
            self.log_event("WINDOWED: fullscreen disabled for this launch")
            return
        self.fullscreen = not self.fullscreen
        self._set_display_mode(self.fullscreen)

    def process_events(self):
        """Pump pygame's event queue — MUST be called every frame, or
        Windows will mark the window 'Not Responding' even while it's
        rendering fine. Also handles F11 (toggle fullscreen), ESC/window
        close (quit)."""
        self.clock.tick(60)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.should_quit = True
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F11:
                    self.toggle_fullscreen()
                elif event.key == pygame.K_1:
                    self.load_demo()
                elif event.key == pygame.K_2:
                    self.load_atom("carbon")
                elif event.key == pygame.K_3:
                    self.load_solar_system()
                elif event.key == pygame.K_TAB:
                    self.cycle_theme(1)
                elif event.key == pygame.K_r:
                    self.zoom = 1.0
                    self.translate_x = self.translate_y = self.roll = 0.0
                elif event.key == pygame.K_F3:
                    self.show_diagnostics = not self.show_diagnostics
                elif event.key == pygame.K_h:
                    self.show_help = not self.show_help
                elif event.key == pygame.K_ESCAPE:
                    if self.show_help:
                        self.show_help = False
                    else:
                        self.should_quit = True

    # ---- external control API -------------------------------------------

    def set_state(self, state):
        self.state = state

    def apply_zoom_delta(self, delta, sensitivity=4.0):
        self.zoom = max(0.4, min(3.0, self.zoom + delta * sensitivity))

    def apply_roll_delta(self, delta_deg, sensitivity=1.0):
        self.roll = (self.roll + delta_deg * sensitivity) % 360

    def apply_pan_delta(self, dx, dy, sensitivity=6.0):
        self.translate_x = max(-3.0, min(3.0, self.translate_x + dx * sensitivity))
        self.translate_y = max(-2.0, min(2.0, self.translate_y - dy * sensitivity))

    def trigger_flash(self, gesture_name, intensity=1.0):
        self.flash_color = FLASH_COLORS.get(gesture_name, (1.0, 1.0, 1.0))
        self.flash_intensity = intensity

    def _start_theme_transition(self):
        self._theme_prev_color = self._current_theme_color()
        self._theme_transition_start = self.elapsed

    def cycle_theme(self, direction=1):
        self._start_theme_transition()
        self.theme_index = (self.theme_index + direction) % len(THEMES)

    def set_theme_index(self, index):
        self._start_theme_transition()
        self.theme_index = index % len(THEMES)

    def _current_theme_color(self):
        """Eased blend from whatever the theme was before the last
        change toward the new one, instead of an instant color snap."""
        target = THEMES[self.theme_index]
        t = min(1.0, (self.elapsed - self._theme_transition_start) / self._theme_transition_duration)
        if t >= 1.0:
            return target
        ease = 1 - (1 - t) ** 3
        return tuple(self._theme_prev_color[i] + (target[i] - self._theme_prev_color[i]) * ease for i in range(3))

    def adjust_brightness(self, delta):
        self.brightness = max(0.4, min(1.8, self.brightness + delta))

    def set_hud_lines(self, lines):
        self.hud_lines = lines

    def log_event(self, text):
        with self._event_log_lock:
            self.event_log.appendleft(f"{time.strftime('%H:%M:%S')}  {text}")

    # ---- docking (make room for a side widget) -----------------------------

    def dock_left(self):
        self.docked = True

    def undock(self):
        self.docked = False

    def show_weather(self, data):
        """data: dict with keys location, temp_c, condition, description,
        humidity, wind_kph, updated_at, and optionally 'state' (a US state
        name key into US_STATE_POSITIONS) to show a map instead of the
        generic weather icon. Automatically docks the hologram left."""
        self.weather_status = None
        self.info_card = None
        if self.mode == "info":
            self.mode = "empty"
        self.weather = data
        state = data.get("state")
        self.map_highlight = state if state in US_STATE_POSITIONS else None
        self.dock_left()
        self._trigger_materialize()

    def set_weather_status(self, status, message):
        self.info_card = None
        if self.mode == "info":
            self.mode = "empty"
        self.weather = None  # Never leave old readings looking like a fresh lookup.
        self.weather_status = {"status": status, "message": message}
        self.show_help = False
        self.dock_left()

    def hide_weather(self):
        self.weather_status = None
        self.weather = None
        self.map_highlight = None
        self.undock()

    def show_info_card(self, question, answer):
        """General Q&A gets a visual readout too, not just speech —
        this is the fallback for anything that doesn't match one of the
        specialized displays (atom, shape, weather, etc.)."""
        self.hide_weather()
        self.mode = "info"
        self.mode_label = "RESPONSE"
        self.info_scroll = 0
        self.info_card = {"question": question, "answer": answer}
        self.orbits = []
        self.selected_index = None
        self._trigger_materialize()

    def hide_info_card(self):
        self.info_card = None

    def set_particle_quality(self, quality):
        if quality not in ("performance", "balanced", "cinematic"):
            raise ValueError("Unknown particle quality")
        self.particle_quality = quality
        self.log_event(f"GRAPHICS: {quality} quality")

    def scroll_info(self, direction):
        if self.info_card:
            self.info_scroll = max(0, self.info_scroll + direction * self._info_page_lines)

    def show_core(self):
        self.hide_weather()
        self.info_card = None
        self.show_help = False
        self.mode = "empty"
        self.mode_label = "VOICE CORE"
        self.orbits = []
        self.selected_index = None
        self._trigger_materialize()

    def new_element(self):
        self.hide_weather()
        self.info_card = None
        self.protons, self.neutrons, self.electron_count = 1, 0, 1
        self.current_element_z = 1
        self.mode = "atom"
        self._rebuild_atom_orbits()
        self._update_custom_label()
        self._trigger_materialize()

    def _trigger_materialize(self):
        if self.mode not in ("empty", "info"):
            self.info_card = None
        self.show_help = False
        self._materialize_start = self.elapsed

    # ---- content: atom / solar system / demo ------------------------------

    def load_atom(self, element_query):
        """Rebuild the display as a Bohr-model atom for the requested
        element. Returns (symbol, name) on success, or None if the
        element wasn't recognized."""
        key = element_query.strip().lower()
        match = ELEMENTS.get(key)
        if not match:
            for name, (symbol, atomic_number) in ELEMENTS.items():
                if symbol.lower() == key:
                    match = (symbol, atomic_number)
                    key = name
                    break
        if not match:
            return None

        symbol, atomic_number = match
        self.hide_weather()
        self.protons = atomic_number
        self.neutrons = NEUTRONS.get(key, _approx_neutrons(atomic_number))
        self.electron_count = atomic_number
        self.current_element_z = atomic_number
        self._rebuild_atom_orbits()
        self.mode = "atom"
        self.mode_label = f"{key.upper()} ({symbol}) — {self.protons}p {self.neutrons}n {self.electron_count}e"
        self._trigger_materialize()
        return symbol, key

    def next_element(self):
        self.current_element_z = self.current_element_z % 118 + 1
        symbol, name = ATOMIC_NUMBER_TO_ELEMENT[self.current_element_z]
        self.load_atom(name)
        return symbol, name, self.current_element_z

    def previous_element(self):
        self.current_element_z = (self.current_element_z - 2) % 118 + 1
        symbol, name = ATOMIC_NUMBER_TO_ELEMENT[self.current_element_z]
        self.load_atom(name)
        return symbol, name, self.current_element_z

    # ---- particle editing: build custom elements ---------------------------

    def _rebuild_atom_orbits(self):
        shells = fill_shells(self.electron_count)
        new_orbits = []
        base_radius = 0.55
        for i, count in enumerate(shells):
            radius = base_radius + i * 0.45
            tilt = (i * 35) % 180 - 60
            speed = (50 - i * 12) * (1 if i % 2 == 0 else -1)
            phases = [j * (360 / count) for j in range(count)] if count else []
            new_orbits.append({"radius": radius, "tilt": tilt, "speed": speed, "electrons": phases, "shell": i})
        self.orbits = new_orbits
        self.selected_index = None

        shell_parts = [f"{SHELL_NAMES[i] if i < len(SHELL_NAMES) else i+1}:{c}e" for i, c in enumerate(shells)]
        self.shell_summary = "  ".join(shell_parts)

    def _update_custom_label(self):
        known = ATOMIC_NUMBER_TO_ELEMENT.get(self.protons)
        if known:
            symbol, name = known
            base = f"{name.upper()} ({symbol})"
        else:
            base = "CUSTOM ELEMENT"
        charge = self.protons - self.electron_count
        charge_str = f"  charge {charge:+d}" if charge != 0 else ""
        self.mode_label = f"{base} — {self.protons}p {self.neutrons}n {self.electron_count}e{charge_str}"

    def _ensure_atom_mode(self):
        self.hide_weather()
        self.info_card = None
        if self.mode != "atom":
            self.mode = "atom"
            self._rebuild_atom_orbits()

    def add_protons(self, n=1):
        """Adding/removing protons changes the element itself, and keeps
        the atom neutral by default (electrons follow along)."""
        self._ensure_atom_mode()
        previous = self.protons
        self.protons = max(1, min(118, self.protons + n))
        self.current_element_z = self.protons
        self.electron_count = max(0, min(118, self.electron_count + self.protons - previous))
        self._rebuild_atom_orbits()
        self._update_custom_label()
        return self.protons - previous

    def add_neutrons(self, n=1):
        """Neutrons don't affect electron shells — just the isotope."""
        self._ensure_atom_mode()
        previous = self.neutrons
        self.neutrons = max(0, min(300, self.neutrons + n))
        self._update_custom_label()
        return self.neutrons - previous

    def add_electrons(self, n=1):
        """Electrons alone (protons unchanged) creates an ion."""
        self._ensure_atom_mode()
        previous = self.electron_count
        self.electron_count = max(0, min(118, self.electron_count + n))
        self._rebuild_atom_orbits()
        self._update_custom_label()
        return self.electron_count - previous

    def load_solar_system(self):
        self.hide_weather()
        new_orbits = []
        for name, radius, speed in PLANETS:
            new_orbits.append({
                "radius": radius, "tilt": 2, "speed": speed, "electrons": [0],
                "label": name,
            })
        self.orbits = new_orbits
        self.selected_index = None
        self.mode = "solar_system"
        self.mode_label = "SOLAR SYSTEM"
        self._trigger_materialize()

    def load_star_system(self, name):
        """Any star system, real or not: curated real data for well-known
        ones (TRAPPIST-1, Kepler-90, etc.), a consistent generated layout
        for anything else. Reuses the solar-system rendering style."""
        self.hide_weather()
        key = name.strip().lower()
        if key in STAR_SYSTEMS:
            planets = STAR_SYSTEMS[key]
            label = key.title()
            generated = False
        else:
            planets = _generate_procedural_system(key)
            label = key.title() if key else "Unknown"
            generated = True

        key_list = list(STAR_SYSTEMS.keys())
        if key in STAR_SYSTEMS:
            self.current_star_index = key_list.index(key)

        new_orbits = []
        for pname, radius, speed in planets:
            new_orbits.append({"radius": radius, "tilt": 2, "speed": speed, "electrons": [0], "label": pname})
        self.orbits = new_orbits
        self.selected_index = None
        self.mode = "solar_system"
        self.mode_label = f"{label.upper()} SYSTEM" + (" (est.)" if generated else "")
        self._trigger_materialize()
        return label, len(planets), generated

    def next_star_system(self):
        keys = list(STAR_SYSTEMS.keys())
        self.current_star_index = (self.current_star_index + 1) % len(keys)
        return self.load_star_system(keys[self.current_star_index])

    def previous_star_system(self):
        keys = list(STAR_SYSTEMS.keys())
        self.current_star_index = (self.current_star_index - 1) % len(keys)
        return self.load_star_system(keys[self.current_star_index])

    def load_demo(self):
        self.hide_weather()
        self.orbits = deepcopy(DEFAULT_ORBITS)
        self.selected_index = None
        self.mode = "demo"
        self.mode_label = "DEMO DISPLAY"
        self._trigger_materialize()

    def load_shape(self, shape_name):
        """Switch to a generic wireframe primitive (sphere, cube, torus,
        pyramid, cylinder) instead of the orbit/atom display."""
        if shape_name not in SHAPES:
            return False
        self.hide_weather()
        self.mode = "shape"
        self.orbits = []
        self.shape_name = shape_name
        self.mode_label = f"{shape_name.upper()} MODEL"
        self.selected_index = None
        self._trigger_materialize()
        return True

    # ---- orbit editing -----------------------------------------------------

    def select_orbit(self, index):
        """Select a specific orbit/shell by 0-based index. Returns True on
        success, False if the index is out of range."""
        if 0 <= index < len(self.orbits):
            self.selected_index = index
            return True
        return False

    def deselect_orbit(self):
        self.selected_index = None

    def select_next_orbit(self):
        if not self.orbits:
            self.selected_index = None
            return
        if self.selected_index is None:
            self.selected_index = 0
        else:
            self.selected_index += 1
            if self.selected_index >= len(self.orbits):
                self.selected_index = None

    def edit_selected_orbit(self, pitch_norm, yaw_norm, dt, rate=2.0):
        if self.selected_index is None or self.selected_index >= len(self.orbits):
            return
        orbit = self.orbits[self.selected_index]
        orbit["radius"] = max(RADIUS_MIN, min(RADIUS_MAX, orbit["radius"] - pitch_norm * rate * dt * 2.0))
        orbit["speed"] = max(SPEED_MIN, min(SPEED_MAX, orbit["speed"] + yaw_norm * rate * dt * 60.0))

    def reset_orbits(self):
        self.show_core()

    # ---- per-frame update --------------------------------------------------

    def update(self, yaw_norm, pitch_norm, pinch_amount, target_dt=1 / 60):
        target_y = yaw_norm * 90
        target_x = pitch_norm * 60
        self.rotation_y += (target_y - self.rotation_y) * 0.15
        self.rotation_x += (target_x - self.rotation_x) * 0.15

        self.pulse_phase += target_dt * (4 + pinch_amount * 6)
        self.sweep_phase += target_dt * 60
        self.scanline_phase += target_dt * 40
        self.elapsed += target_dt

        dock_target = -2.4 if self.docked else 0.0
        self._dock_shift += (dock_target - self._dock_shift) * 0.08

        for orbit in self.orbits:
            delta = orbit["speed"] * target_dt
            orbit["electrons"] = [(p + delta) % 360 for p in orbit["electrons"]]

        if self.flash_intensity > 0:
            self.flash_intensity = max(0.0, self.flash_intensity - target_dt * 2.0)

        now = time.time()
        frame_dt = now - self._last_time
        self._last_time = now
        if frame_dt > 0:
            self._fps = 0.9 * self._fps + 0.1 * (1.0 / frame_dt)

    # ---- 3D drawing helpers --------------------------------------------------

    def _draw_ring(self, radius, segments=64):
        glBegin(GL_LINE_LOOP)
        for i in range(segments):
            theta = 2.0 * math.pi * i / segments
            glVertex3f(radius * math.cos(theta), radius * math.sin(theta), 0.0)
        glEnd()

    def _draw_wireframe_sphere(self, radius, lat_count=5, lon_count=6, segments=48):
        # Latitude rings: horizontal circles at different heights
        for i in range(1, lat_count + 1):
            phi = math.radians(-75 + i * (150 / (lat_count + 1)))
            y = radius * math.sin(phi)
            r = radius * math.cos(phi)
            glPushMatrix()
            glTranslatef(0, y, 0)
            self._draw_ring(r, segments)
            glPopMatrix()
        # Longitude great circles: vertical circles through the poles,
        # rotated around Y at even intervals
        for i in range(lon_count):
            azimuth = 180.0 * i / lon_count
            glPushMatrix()
            glRotatef(azimuth, 0, 1, 0)
            glBegin(GL_LINE_LOOP)
            for j in range(segments):
                theta = 2.0 * math.pi * j / segments
                glVertex3f(radius * math.cos(theta), radius * math.sin(theta), 0.0)
            glEnd()
            glPopMatrix()

    def _draw_wireframe_cube(self, size):
        s = size
        verts = [
            (-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
            (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s),
        ]
        edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
                 (0, 4), (1, 5), (2, 6), (3, 7)]
        glBegin(GL_LINES)
        for a, b in edges:
            glVertex3f(*verts[a]); glVertex3f(*verts[b])
        glEnd()

    def _draw_wireframe_torus(self, major_r, minor_r, major_segments=16, minor_segments=12):
        for i in range(major_segments):
            theta = 2.0 * math.pi * i / major_segments
            cx, cz = major_r * math.cos(theta), major_r * math.sin(theta)
            glPushMatrix()
            glTranslatef(cx, 0, cz)
            glRotatef(math.degrees(theta) + 90, 0, 1, 0)
            glBegin(GL_LINE_LOOP)
            for j in range(minor_segments):
                phi = 2.0 * math.pi * j / minor_segments
                glVertex3f(minor_r * math.cos(phi), minor_r * math.sin(phi), 0.0)
            glEnd()
            glPopMatrix()

    def _draw_wireframe_pyramid(self, size):
        apex = (0, size, 0)
        base = [(-size, -size, -size), (size, -size, -size),
                 (size, -size, size), (-size, -size, size)]
        glBegin(GL_LINE_LOOP)
        for v in base:
            glVertex3f(*v)
        glEnd()
        glBegin(GL_LINES)
        for v in base:
            glVertex3f(*apex); glVertex3f(*v)
        glEnd()

    def _draw_wireframe_cylinder(self, radius, height, segments=32):
        glPushMatrix()
        glTranslatef(0, height / 2, 0)
        self._draw_ring(radius, segments)
        glPopMatrix()
        glPushMatrix()
        glTranslatef(0, -height / 2, 0)
        self._draw_ring(radius, segments)
        glPopMatrix()
        glBegin(GL_LINES)
        for i in range(8):
            theta = 2.0 * math.pi * i / 8
            x, z = radius * math.cos(theta), radius * math.sin(theta)
            glVertex3f(x, height / 2, z)
            glVertex3f(x, -height / 2, z)
        glEnd()

    def _draw_eiffel_tower(self, size=1.4):
        """Tapering 4-legged lattice tower with platforms and diagonal
        bracing at each stage, narrowing to a spike — a stylized, not
        architecturally exact, Eiffel Tower silhouette."""
        levels = [
            (size * 0.9, 0.0), (size * 0.45, size * 0.9),
            (size * 0.18, size * 1.5), (0.0, size * 2.0),
        ]

        def corners(half_w, y):
            return [(half_w, y, half_w), (half_w, y, -half_w),
                    (-half_w, y, -half_w), (-half_w, y, half_w)]

        for i in range(len(levels) - 1):
            w0, y0 = levels[i]
            w1, y1 = levels[i + 1]
            c0, c1 = corners(w0, y0), corners(w1, y1)

            glBegin(GL_LINES)
            for j in range(4):
                glVertex3f(*c0[j]); glVertex3f(*c1[j])
                glVertex3f(*c0[j]); glVertex3f(*c1[(j + 1) % 4])  # diagonal bracing
            glEnd()

            if w1 > 0.001:
                glBegin(GL_LINE_LOOP)
                for c in c1:
                    glVertex3f(*c)
                glEnd()

        glBegin(GL_LINES)
        glVertex3f(0, size * 2.0, 0)
        glVertex3f(0, size * 2.35, 0)
        glEnd()

    def _draw_skyscraper(self, size=1.4):
        """Tall rectangular tower with floor lines, a setback top section,
        and a spire — a stylized generic skyscraper silhouette."""
        hw = size * 0.42
        height = size * 2.2
        body_top = height * 0.8

        bottom = [(hw, 0, hw), (hw, 0, -hw), (-hw, 0, -hw), (-hw, 0, hw)]
        top = [(hw, body_top, hw), (hw, body_top, -hw), (-hw, body_top, -hw), (-hw, body_top, hw)]

        glBegin(GL_LINES)
        for j in range(4):
            glVertex3f(*bottom[j]); glVertex3f(*top[j])
        glEnd()
        for verts in (bottom, top):
            glBegin(GL_LINE_LOOP)
            for v in verts:
                glVertex3f(*v)
            glEnd()

        floors = 8
        for f in range(1, floors):
            fy = body_top * f / floors
            glBegin(GL_LINE_LOOP)
            glVertex3f(hw, fy, hw); glVertex3f(hw, fy, -hw)
            glVertex3f(-hw, fy, -hw); glVertex3f(-hw, fy, hw)
            glEnd()

        top2_hw = hw * 0.55
        top2 = [(top2_hw, height, top2_hw), (top2_hw, height, -top2_hw),
                (-top2_hw, height, -top2_hw), (-top2_hw, height, top2_hw)]
        glBegin(GL_LINES)
        for j in range(4):
            glVertex3f(*top[j]); glVertex3f(*top2[j])
        glEnd()
        glBegin(GL_LINE_LOOP)
        for v in top2:
            glVertex3f(*v)
        glEnd()

        glBegin(GL_LINES)
        glVertex3f(0, height, 0)
        glVertex3f(0, height * 1.15, 0)
        glEnd()

    def _draw_dna_helix(self, height=2.4, radius=0.5, turns=3, rungs=22):
        """Two intertwined helical strands with connecting rungs — a
        stylized double-helix, not a biologically accurate base-pair
        model, but immediately recognizable as DNA."""
        segments_per_turn = 20
        total_segments = int(turns * segments_per_turn)
        strand1, strand2 = [], []

        for i in range(total_segments + 1):
            frac = i / total_segments
            y = -height / 2 + frac * height
            angle = frac * turns * 2 * math.pi
            strand1.append((radius * math.cos(angle), y, radius * math.sin(angle)))
            strand2.append((radius * math.cos(angle + math.pi), y, radius * math.sin(angle + math.pi)))

        for strand in (strand1, strand2):
            glBegin(GL_LINE_STRIP)
            for p in strand:
                glVertex3f(*p)
            glEnd()

        step = max(1, total_segments // rungs)
        glBegin(GL_LINES)
        for i in range(0, total_segments + 1, step):
            glVertex3f(*strand1[i])
            glVertex3f(*strand2[i])
        glEnd()

    def _draw_shape(self, name):
        if name == "sphere":
            self._draw_wireframe_sphere(1.5)
        elif name == "cube":
            self._draw_wireframe_cube(1.1)
        elif name == "torus":
            self._draw_wireframe_torus(1.2, 0.5)
        elif name == "pyramid":
            self._draw_wireframe_pyramid(1.3)
        elif name == "cylinder":
            self._draw_wireframe_cylinder(1.0, 2.0)
        elif name == "eiffel tower":
            self._draw_eiffel_tower()
        elif name == "skyscraper":
            self._draw_skyscraper()
        elif name == "dna":
            self._draw_dna_helix()

    def _draw_node_fan(self, radius, segments=16):
        glBegin(GL_TRIANGLE_FAN)
        glVertex3f(0, 0, 0)
        for i in range(segments + 1):
            theta = 2.0 * math.pi * i / segments
            glVertex3f(radius * math.cos(theta), radius * math.sin(theta), 0.0)
        glEnd()

    def _draw_glow_fan(self, base_radius, r, g, b, passes=4):
        for i in range(passes, 0, -1):
            scale = 1.0 + i * 0.5
            alpha = 0.10 / i
            glColor4f(r, g, b, alpha)
            self._draw_node_fan(base_radius * scale)

    NUCLEON_RENDER_CAP = 40

    def _nucleon_positions(self, count, cluster_radius=0.22):
        """Fibonacci-sphere point distribution — an even, natural-looking
        packing of points on a sphere surface, used to arrange individual
        proton/neutron particles into a convincing little cluster."""
        positions = []
        if count <= 0:
            return positions
        golden_angle = math.pi * (3 - math.sqrt(5))
        for i in range(count):
            y = 1 - (i / max(1, count - 1)) * 2
            radius_at_y = math.sqrt(max(0.0, 1 - y * y))
            theta = golden_angle * i
            x = math.cos(theta) * radius_at_y
            z = math.sin(theta) * radius_at_y
            positions.append((x * cluster_radius, y * cluster_radius, z * cluster_radius))
        return positions

    def _draw_nucleus_particles(self, protons, neutrons, brightness):
        """Real proton/neutron particle cluster instead of a single dot —
        capped for performance (a gold nucleus is 197 particles; drawing
        that many every frame on integrated graphics would hurt FPS for
        no visual benefit past a certain density, so above the cap we
        render a representative subset at the same proton:neutron ratio)."""
        total = protons + neutrons
        if total <= 0:
            return

        render_total = min(total, self.NUCLEON_RENDER_CAP)
        if total > 0:
            render_protons = max(1, round(render_total * protons / total)) if protons > 0 else 0
            render_neutrons = render_total - render_protons
        else:
            render_protons = render_neutrons = 0

        # single shared glow halo behind the whole cluster (much cheaper
        # than glowing every individual particle)
        halo_radius = 0.16 + min(0.26, total * 0.0035)
        self._draw_glow_fan(halo_radius, 1.0 * brightness, 0.65 * brightness, 0.35 * brightness, passes=6)

        positions = self._nucleon_positions(render_total, cluster_radius=min(0.3, 0.16 + total * 0.0022))
        idx = 0
        for _ in range(render_protons):
            x, y, z = positions[idx]; idx += 1
            glPushMatrix()
            glTranslatef(x, y, z)
            glColor4f(1.0 * brightness, 0.3 * brightness, 0.25 * brightness, 1.0)
            self._draw_node_fan(0.065, segments=10)
            glPopMatrix()
        for _ in range(render_neutrons):
            x, y, z = positions[idx]; idx += 1
            glPushMatrix()
            glTranslatef(x, y, z)
            glColor4f(0.6 * brightness, 0.7 * brightness, 0.9 * brightness, 1.0)
            self._draw_node_fan(0.065, segments=10)
            glPopMatrix()

    def _draw_sweep_arc(self, radius, span_deg=50):
        glPushMatrix()
        glRotatef(self.sweep_phase, 0, 0, 1)
        glBegin(GL_LINE_STRIP)
        segments = 24
        for i in range(segments + 1):
            theta = math.radians(i * span_deg / segments)
            glVertex3f(radius * math.cos(theta), radius * math.sin(theta), 0.0)
        glEnd()
        glPopMatrix()

    def _draw_base_plate(self, base_color, brightness):
        glPushMatrix()
        glTranslatef(0, -1.9, 0)
        glColor4f(base_color[0] * brightness, base_color[1] * brightness, base_color[2] * brightness, 0.5)
        for radius in (1.8, 1.4, 1.0):
            glPushMatrix()
            glScalef(1.0, 0.28, 1.0)
            self._draw_ring(radius, segments=48)
            glPopMatrix()
        glPopMatrix()

    # ---- ortho (screen-space) helpers -----------------------------------------

    def _begin_ortho(self):
        glMatrixMode(GL_PROJECTION)
        glPushMatrix(); glLoadIdentity()
        glOrtho(0, self.width, self.height, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST)

    def _end_ortho(self):
        glEnable(GL_DEPTH_TEST)
        glMatrixMode(GL_PROJECTION); glPopMatrix()
        glMatrixMode(GL_MODELVIEW); glPopMatrix()

    def _draw_space_background(self):
        """Deep-space backdrop: faint grid, fixed twinkling starfield and a
        few thin constellation links between nearby stars."""
        cx, cy = self.width / 2, self.height / 2
        max_dist = math.hypot(cx, cy)
        glLineWidth(1.0)
        glBegin(GL_LINES)
        x = 0
        while x < self.width:
            dist = abs(x - cx) / max_dist
            glColor4f(0.16, 0.22, 0.38, max(0.015, 0.05 * (1 - dist)))
            glVertex2f(x, 0); glVertex2f(x, self.height)
            x += 90
        y = 0
        while y < self.height:
            dist = abs(y - cy) / max_dist
            glColor4f(0.16, 0.22, 0.38, max(0.015, 0.05 * (1 - dist)))
            glVertex2f(0, y); glVertex2f(self.width, y)
            y += 90
        glEnd()

        glBegin(GL_LINES)
        for a, b in self.star_links:
            ax, ay = self.stars[a][0] * self.width, self.stars[a][1] * self.height
            bx, by = self.stars[b][0] * self.width, self.stars[b][1] * self.height
            glColor4f(0.55, 0.65, 0.9, 0.05)
            glVertex2f(ax, ay); glVertex2f(bx, by)
        glEnd()

        for fx, fy, phase, size in self.stars:
            twinkle = 1.0 if self.reduced_motion else \
                0.35 + 0.65 * max(0.0, math.sin(self.elapsed * 1.2 + phase))
            glColor4f(0.82, 0.88, 1.0, 0.65 * twinkle)
            self._draw_circle_2d(fx * self.width, fy * self.height, size * 0.7)

    def _draw_rect(self, x, y, w, h, r, g, b, a, filled=True):
        glBegin(GL_QUADS if filled else GL_LINE_LOOP)
        glColor4f(r, g, b, a)
        glVertex2f(x, y); glVertex2f(x + w, y)
        glVertex2f(x + w, y + h); glVertex2f(x, y + h)
        glEnd()

    def _materialize_progress(self):
        t = min(1.0, (self.elapsed - self._materialize_start) / self._materialize_duration)
        return 1 - (1 - t) ** 3  # ease-out cubic

    def _draw_panel(self, x, y, w, h, accent, chamfer=14, fill_alpha=0.55, alpha_mult=1.0):
        """A dark backing plate with two chamfered (cut) corners and a
        thin accent border — gives text somewhere to sit instead of
        floating directly over the busy 3D scene, and reads as an
        intentional HUD panel rather than plain text. alpha_mult scales
        everything for fade-in transitions."""
        r, g, b = accent
        pts = [
            (x + chamfer, y), (x + w, y), (x + w, y + h - chamfer),
            (x + w - chamfer, y + h), (x, y + h), (x, y + chamfer),
        ]
        glColor4f(0.035, 0.027, 0.017, fill_alpha * alpha_mult)
        glBegin(GL_POLYGON)
        for px, py in pts:
            glVertex2f(px, py)
        glEnd()

        border_pulse = 0.28
        glColor4f(r, g, b, border_pulse * alpha_mult)
        glLineWidth(1.3)
        glBegin(GL_LINE_LOOP)
        for px, py in pts:
            glVertex2f(px, py)
        glEnd()

        # small bright accent tick at the chamfered corner
        glColor4f(r, g, b, 0.9 * alpha_mult)
        glLineWidth(2.0)
        glBegin(GL_LINES)
        glVertex2f(x, y + chamfer + 10); glVertex2f(x, y + chamfer)
        glVertex2f(x, y + chamfer); glVertex2f(x + chamfer, y)
        glEnd()

    def _draw_segmented_bar(self, x, y, w, h, frac, color, segments=18, gap=2):
        """Segmented tick-style HUD bar instead of a plain solid fill."""
        frac = max(0.0, min(1.0, frac))
        lit = round(frac * segments)
        seg_w = (w - gap * (segments - 1)) / segments
        for i in range(segments):
            sx = x + i * (seg_w + gap)
            if i < lit:
                glColor4f(color[0], color[1], color[2], 0.95)
            else:
                glColor4f(color[0], color[1], color[2], 0.15)
            glBegin(GL_QUADS)
            glVertex2f(sx, y); glVertex2f(sx + seg_w, y)
            glVertex2f(sx + seg_w, y + h); glVertex2f(sx, y + h)
            glEnd()

    def _truncate(self, text, max_chars):
        return text if len(text) <= max_chars else text[:max_chars - 1] + "…"

    def _blit_text(self, font, text, x, y, color=(235, 216, 182)):
        if not font:
            return 0
        try:
            # Cache CPU glyph pixels, not context-bound GL textures. Safe across F11.
            key = (id(font), str(text), tuple(color))
            pixels = self._text_cache.get(key)
            if pixels is None:
                surface = font.render(str(text), True, color)
                w, h = surface.get_size()
                pixels = (w, h, pygame.image.tostring(surface, "RGBA", True))
                self._text_cache[key] = pixels
                if len(self._text_cache) > 256:
                    self._text_cache.popitem(last=False)
            self._text_cache.move_to_end(key)
            w, h, data = pixels
            glRasterPos2f(x, y + h)
            glDrawPixels(w, h, GL_RGBA, GL_UNSIGNED_BYTE, data)
            return h
        except Exception:
            return 0

    def _fit_text(self, font, text, width):
        """Pixel-based clipping for labels, including long activity messages."""
        if not font:
            return text
        if font.size(text)[0] <= width:
            return text
        while text and font.size(text + "...")[0] > width:
            text = text[:-1]
        return text + "..." if text else ""

    def _center_text(self, font, text, cx, y, color=(215, 235, 245)):
        width = font.size(text)[0] if font else 0
        self._blit_text(font, text, cx - width / 2, y, color=color)

    def _draw_tab_chip(self, x, y, label, active, accent):
        """Small status tab like the reference's BUSINESS/PERSONAL chips.
        Visual only — the dashboard stays voice-operated."""
        text_w = self.font_small.size(label)[0] if self.font_small else len(label) * 7
        w, h = text_w + 30, 20
        r, g, b = accent
        self._draw_rect(x, y, w, h, r, g, b, 0.14 if active else 0.04)
        self._draw_rect(x, y, w, h, r, g, b, 0.55 if active else 0.16, filled=False)
        dot_a = 0.9 if active else 0.3
        pulse = 0.6 + 0.4 * math.sin(self.elapsed * 3) if active and not self.reduced_motion else 1.0
        glColor4f(r, g, b, dot_a * pulse)
        self._draw_circle_2d(x + 10, y + h / 2, 2.5, segments=10)
        self._blit_text(self.font_small, label, x + 18, y + 4,
                        color=(int(120 + 120 * r * (0.4 + 0.6 * active)),
                               int(120 + 120 * g * (0.4 + 0.6 * active)),
                               int(120 + 120 * b * (0.4 + 0.6 * active))))
        return w

    def _draw_top_bar(self, theme_color):
        """Reference-style status bar: mode tabs left, live briefing chip in
        the middle, real stats counters on the right."""
        # left: status tabs (visual only, voice remains the only input)
        x, y = 24, 24
        x += self._draw_tab_chip(x, y, "CORE", self.mode == "empty", (0.45, 1.0, 0.55)) + 8
        x += self._draw_tab_chip(x, y, "SYSTEM", self.show_diagnostics, (1.0, 0.78, 0.25)) + 8
        self._draw_rect(x, y, 20, 20, 0.55, 0.65, 0.85, 0.05)
        self._draw_rect(x, y, 20, 20, 0.55, 0.65, 0.85, 0.25, filled=False)
        self._blit_text(self.font_small, "+", x + 6, y + 3, color=(150, 165, 195))

        # center: live briefing chip
        stamp = time.strftime("%m / %d %I:%M %p").replace(" 0", " ").upper()
        briefing = f"BRIEFING - LIVE {stamp}"
        bw = self.font_small.size(briefing)[0] + 40 if self.font_small else len(briefing) * 8
        bx = self.width / 2 - bw / 2
        self._draw_rect(bx, 22, bw, 22, 1.0, 0.62, 0.12, 0.08)
        self._draw_rect(bx, 22, bw, 22, 1.0, 0.62, 0.12, 0.5, filled=False)
        pulse = 0.55 + 0.45 * math.sin(self.elapsed * 2.4) if not self.reduced_motion else 1.0
        glColor4f(0.6, 1.0, 0.4, pulse)
        self._draw_circle_2d(bx + 12, 33, 2.5, segments=10)
        self._blit_text(self.font_small, briefing, bx + 22, 27, color=(235, 170, 80))
        self._center_text(self.font_small, "A U R O R A", self.width / 2, 52, color=(140, 122, 92))

        # right: real counters (fps + psutil + uptime)
        if self.width >= 900:
            uptime = max(0, int(time.time() - self._start_time))
            up = f"+{uptime // 3600:02d}:{(uptime % 3600) // 60:02d}"
            stats = [("FPS", f"{self._fps:.0f}")]
            for label, frac in self._get_system_stats()[:2]:
                stats.append((label, "N/A" if frac is None else f"{int(frac * 100)}"))
            stats.append(("UPTIME", up))
            # two-row mini table, right aligned: dim label over bright value
            rx = self.width - 24
            for label, value in reversed(stats):
                vw = self.font_small.size(value)[0] if self.font_small else len(value) * 7
                lw = self.font_small.size(label)[0] if self.font_small else len(label) * 7
                rx -= max(vw, lw)
                self._blit_text(self.font_small, label, rx, 20, color=(95, 110, 135))
                self._blit_text(self.font_small, value, rx, 34, color=(215, 230, 250))
                rx -= 18
            label = "CORE / VOICE" if self.mode == "empty" else self.mode_label
            self._blit_text(self.font_small, self._fit_text(self.font_small, label, self.width / 2 - 235),
                            36, 52, color=(110, 100, 80))

    def _get_system_stats(self):
        """Real system stats via psutil. Falls back to a clearly-labeled
        'N/A' rather than fake numbers if psutil isn't available."""
        if not PSUTIL_AVAILABLE:
            return [("CPU", None), ("MEM", None), ("BATT", None)]

        if self.elapsed - self._stats_sample_time < 0.5 and self._stats_cache:
            return self._stats_cache
        self._stats_sample_time = self.elapsed
        cpu = psutil.cpu_percent(interval=None) / 100.0
        self.cpu_history.append(cpu)
        mem = psutil.virtual_memory().percent / 100.0

        battery = psutil.sensors_battery()
        if battery is not None:
            batt_frac = battery.percent / 100.0
            batt_label = "BATT" if not battery.power_plugged else "BATT (chg)"
        else:
            batt_frac = None
            batt_label = "BATT"

        self._stats_cache = [("CPU", cpu), ("MEM", mem), (batt_label, batt_frac)]
        return self._stats_cache

    def _draw_sparkline(self, x, y, w, h, values, color):
        if len(values) < 2:
            return
        glColor4f(color[0], color[1], color[2], 0.5)
        glLineWidth(1.3)
        glBegin(GL_LINE_STRIP)
        n = len(values)
        for i, v in enumerate(values):
            vx = x + (i / (n - 1)) * w
            vy = y + h - max(0.0, min(1.0, v)) * h
            glVertex2f(vx, vy)
        glEnd()
        # faint fill under the line for a proper "graph" look
        glColor4f(color[0], color[1], color[2], 0.12)
        glBegin(GL_TRIANGLE_STRIP)
        for i, v in enumerate(values):
            vx = x + (i / (n - 1)) * w
            vy = y + h - max(0.0, min(1.0, v)) * h
            glVertex2f(vx, y + h)
            glVertex2f(vx, vy)
        glEnd()

    def _draw_system_panel(self, theme_color):
        px, py, pw, ph = 20, 110, 220, 242
        self._draw_rect(px, py, 2, ph, *theme_color, 0.4)

        x, y, bar_w = px + 18, py + 18, 184
        self._blit_text(self.font_small, "SYSTEM / LIVE", x, y, color=(90, 130, 160))
        y += 24
        for label, frac in self._get_system_stats():
            if frac is None:
                self._blit_text(self.font_small, f"{label}", x, y, color=(150, 200, 230))
                self._blit_text(self.font_small, "N/A", x + bar_w - 30, y, color=(90, 110, 130))
                y += 16
                self._draw_segmented_bar(x, y, bar_w, 7, 0.0, (0.2, 0.3, 0.4))
                y += 24
                continue
            frac = max(0.0, min(1.0, frac))
            self._blit_text(self.font_small, label, x, y, color=(150, 200, 230))
            self._blit_text(self.font_small, f"{int(frac*100)}%", x + bar_w - 30, y, color=(200, 235, 255))
            y += 16
            self._draw_segmented_bar(x, y, bar_w, 7, frac, theme_color)
            y += 24

        # live CPU history graph — actual trend over the last ~20 seconds,
        # not just an instantaneous bar
        self._blit_text(self.font_small, "CPU HISTORY", x, y, color=(90, 130, 160))
        y += 16
        self._draw_sparkline(x, y, bar_w, 26, list(self.cpu_history), theme_color)

    def _draw_event_log(self):
        pw, ph = 260, 242
        px, py = self.width - pw - 20, 110
        self._draw_rect(px, py, 2, ph, 0.3, 0.85, 1.0, 0.25)

        x, y = px + 18, py + 16
        self._blit_text(self.font_small, "RECENT ACTIVITY", x, y, color=(90, 130, 160))
        y += 24
        with self._event_log_lock:
            log_snapshot = list(self.event_log)
        if not log_snapshot:
            self._blit_text(self.font_small, "Your session starts here.", x, y, color=(80, 110, 130))
        for i, line in enumerate(log_snapshot):
            fade = max(0.35, 1.0 - i * 0.16)
            base = (170, 220, 245) if i == 0 else (150, 190, 215)
            color = tuple(int(c * fade) for c in base)
            self._blit_text(self.font_small, self._fit_text(self.font_small, line, pw - 36), x, y, color=color)
            y += 27

    def _voice_color(self):
        # Preserve the reference's amber palette through voice states. States change
        # energy and warmth, rather than abruptly switching the entire core to cyan.
        color = self._current_theme_color()
        warmth = max(0, self.core_animation.energy - .24) * .21
        return tuple(min(1, channel + warmth) for channel in color)

    def _draw_bottom_bar(self, theme_color):
        # State-driven animation, deliberately not presented as microphone amplitude.
        color = self._voice_color()
        cx, y = self.width / 2, self.height - 108
        active = self.voice_available and self.state != "idle"
        for i in range(49):
            envelope = math.sin(math.pi * i / 48) ** 2
            wave = abs(math.sin(self.elapsed * (5 if active else 1.3) + i * 0.48))
            h = 3 + envelope * (24 if active else 6) * wave
            self._draw_rect(cx + (i - 24) * 5, y - h / 2, 2, h, *color, 0.45 + envelope * 0.5)
        label = ({"idle": 'SAY "AURORA" TO BEGIN', "listening": "LISTENING",
                  "thinking": "PROCESSING YOUR REQUEST", "speaking": "AURORA IS SPEAKING"}
                 .get(self.state, "STANDBY") if self.voice_available else "VOICE OFFLINE / CHECK MICROPHONE SETUP")
        self._center_text(self.font_small, label, cx, y + 20, color=(165, 140, 105))

        # reference-style command pill with the live transcript inside
        pw = min(430, self.width - 80)
        px, py, ph = cx - pw / 2, self.height - 58, 26
        self._draw_rect(px, py, pw, ph, 0.55, 0.62, 0.85, 0.05)
        self._draw_rect(px, py, pw, ph, 0.55, 0.62, 0.85, 0.28, filled=False)
        transcript = self.last_heard or "talk to aurora"
        shown = self._fit_text(self.font_small, transcript, pw - 56)
        self._blit_text(self.font_small, shown, px + 14, py + 7,
                        color=(185, 195, 215) if self.last_heard else (110, 120, 145))
        ix = px + pw - 24
        self._draw_rect(ix, py + 6, 14, 14, 1.0, 0.68, 0.2, 0.12)
        self._draw_rect(ix, py + 6, 14, 14, 1.0, 0.68, 0.2, 0.5, filled=False)
        glColor4f(1.0, 0.75, 0.3, 0.9 if active else 0.4)
        self._draw_circle_2d(ix + 7, py + 13, 2.0, segments=10)
        if self.width >= 1000:
            self._blit_text(self.font_small, "VOICE INTERFACE / " + ("READY" if self.voice_available else "OFFLINE"),
                            32, self.height - 30, color=(80, 90, 110))
            self._blit_text(self.font_small, "WINDOWED  /  ESC EXIT" if self.windowed_only else "F11 WINDOW  /  ESC EXIT",
                            self.width - 255, self.height - 30, color=(80, 90, 110))

    def _draw_telemetry(self):
        """The per-frame HUD lines main.py feeds (zoom/fps/voice/hands) were
        collected but never drawn — surface them as a dim readout so the
        tracking state is visible without a debug window."""
        if not self.hud_lines:
            return
        y = self.height - 96
        for line in self.hud_lines:
            y -= 16
            self._blit_text(self.font_small, self._fit_text(self.font_small, line, 260),
                            24, y, color=(96, 132, 160))

    def _overlay_frame(self, rect, theme_color, label, hint):
        x, y, w, h = rect.x, rect.y, rect.width, rect.height
        self._draw_panel(x, y, w, h, theme_color, chamfer=12, fill_alpha=.96)
        self._draw_rect(x + 24, y + 48, w - 48, 1, *theme_color, .18)
        self._blit_text(self.font_small, self._fit_text(self.font_small, label, w - 48),
                        x + 24, y + 20, color=(209, 163, 84))
        self._draw_rect(x + 24, y + h - 43, w - 48, 1, *theme_color, .12)
        self._blit_text(self.font_small, self._fit_text(self.font_small, hint, w - 48),
                        x + 24, y + h - 29, color=(153, 130, 94))

    def _draw_help(self, theme_color):
        self._draw_rect(0, 0, self.width, self.height, .006, .005, .004, .9)
        rect = overlay_rect(self.width, self.height, 'help')
        self._overlay_frame(rect, theme_color, "VOICE GUIDE / NO BUTTONS REQUIRED",
                            'Say "Aurora, close help"  /  ESC')
        rows = [("EXPLORE", '"Show me a carbon atom"'),
                ("WEATHER", '"Weather in Ghaziabad"'),
                ("PERSONALIZE", '"Change theme" / "Reduce motion"'),
                ("READ", '"Read more" / "Scroll up"'),
                ("RETURN", '"Show core" / "Close answer"')]
        available = rect.height - 116
        spacing = available / len(rows)
        for i, (label, phrase) in enumerate(rows):
            y = rect.y + 65 + i * spacing
            if spacing >= 38:
                self._blit_text(self.font_small, label, rect.x + 24, y, color=(183, 143, 78))
                self._blit_text(self.font, phrase, rect.x + 24, y + 20, color=(238, 220, 187))
            else:
                self._blit_text(self.font_small, self._fit_text(self.font_small, phrase, rect.width - 48),
                                rect.x + 24, y, color=(238, 220, 187))

    # ---- weather widget (docks the hologram left, shows this on the right) --

    def _draw_circle_2d(self, cx, cy, radius, segments=24, filled=True):
        glBegin(GL_TRIANGLE_FAN if filled else GL_LINE_LOOP)
        if filled:
            glVertex2f(cx, cy)
        for i in range(segments + 1):
            theta = 2.0 * math.pi * i / segments
            glVertex2f(cx + radius * math.cos(theta), cy + radius * math.sin(theta))
        glEnd()

    def _draw_cloud_icon(self, cx, cy, scale, color, alpha=0.9):
        glColor4f(color[0], color[1], color[2], alpha)
        for dx, dy, r in [(-0.5, 0.1, 0.55), (0.1, -0.15, 0.7), (0.7, 0.1, 0.5), (0, 0.3, 0.6)]:
            self._draw_circle_2d(cx + dx * scale, cy + dy * scale, r * scale, segments=20)

    def _draw_weather_icon(self, condition, cx, cy, scale, color):
        if condition == "sunny":
            glColor4f(1.0, 0.85, 0.3, 0.95)
            self._draw_circle_2d(cx, cy, 0.55 * scale, segments=28)
            glLineWidth(3.0)
            glBegin(GL_LINES)
            for i in range(8):
                a = math.radians(i * 45)
                x1, y1 = cx + math.cos(a) * 0.7 * scale, cy + math.sin(a) * 0.7 * scale
                x2, y2 = cx + math.cos(a) * 0.95 * scale, cy + math.sin(a) * 0.95 * scale
                glVertex2f(x1, y1); glVertex2f(x2, y2)
            glEnd()

        elif condition == "partly_cloudy":
            glColor4f(1.0, 0.85, 0.3, 0.9)
            self._draw_circle_2d(cx - 0.4 * scale, cy - 0.35 * scale, 0.4 * scale, segments=24)
            self._draw_cloud_icon(cx + 0.15 * scale, cy + 0.15 * scale, scale * 0.85, (0.75, 0.85, 0.95))

        elif condition == "cloudy":
            self._draw_cloud_icon(cx, cy, scale, (0.7, 0.8, 0.9))

        elif condition == "fog":
            glColor4f(0.7, 0.8, 0.9, 0.7)
            glLineWidth(3.0)
            for i, dy in enumerate([-0.4, -0.1, 0.2, 0.5]):
                glBegin(GL_LINE_STRIP)
                for j in range(9):
                    x = -1.0 * scale + j * 0.25 * scale
                    y = cy + dy * scale + math.sin(j + i) * 0.04 * scale
                    glVertex2f(cx + x, y)
                glEnd()

        elif condition == "rain":
            self._draw_cloud_icon(cx, cy - 0.25 * scale, scale * 0.85, (0.6, 0.7, 0.85))
            glColor4f(0.3, 0.65, 1.0, 0.9)
            glLineWidth(3.0)
            glBegin(GL_LINES)
            for dx in (-0.5, -0.1, 0.3, 0.7):
                glVertex2f(cx + dx * scale, cy + 0.35 * scale)
                glVertex2f(cx + dx * scale - 0.1 * scale, cy + 0.75 * scale)
            glEnd()

        elif condition == "snow":
            self._draw_cloud_icon(cx, cy - 0.25 * scale, scale * 0.85, (0.75, 0.8, 0.9))
            glColor4f(0.9, 0.95, 1.0, 0.95)
            glLineWidth(2.5)
            for dx in (-0.5, -0.1, 0.3, 0.7):
                sx, sy = cx + dx * scale, cy + 0.55 * scale
                glBegin(GL_LINES)
                for a in range(3):
                    ang = math.radians(a * 60)
                    glVertex2f(sx - math.cos(ang) * 0.1 * scale, sy - math.sin(ang) * 0.1 * scale)
                    glVertex2f(sx + math.cos(ang) * 0.1 * scale, sy + math.sin(ang) * 0.1 * scale)
                glEnd()

        elif condition == "storm":
            self._draw_cloud_icon(cx, cy - 0.3 * scale, scale * 0.85, (0.5, 0.55, 0.65))
            glColor4f(1.0, 0.9, 0.2, 1.0)
            glBegin(GL_LINE_STRIP)
            for x, y in [(-0.05, 0.3), (0.15, 0.35), (-0.02, 0.55), (0.15, 0.6), (-0.1, 0.9)]:
                glVertex2f(cx + x * scale, cy + y * scale)
            glEnd()

        else:
            self._draw_cloud_icon(cx, cy, scale, (0.7, 0.8, 0.9))

    def _draw_us_map(self, x, y, w, h, highlight_key, accent_color):
        for name, (nx, ny) in US_STATE_POSITIONS.items():
            if name == highlight_key:
                continue
            glColor4f(0.35, 0.5, 0.62, 0.5)
            self._draw_circle_2d(x + nx * w, y + ny * h, 2.5)

        if highlight_key and highlight_key in US_STATE_POSITIONS:
            nx, ny = US_STATE_POSITIONS[highlight_key]
            hx, hy = x + nx * w, y + ny * h
            pulse = 0.5 + 0.5 * math.sin(self.elapsed * 4)

            glColor4f(accent_color[0], accent_color[1], accent_color[2], 0.22 + 0.1 * pulse)
            self._draw_circle_2d(hx, hy, 16 + 5 * pulse)
            glColor4f(1.0, 1.0, 1.0, 1.0)
            self._draw_circle_2d(hx, hy, 6)

            label = US_STATE_ABBREV.get(highlight_key, highlight_key[:2].upper())
            self._blit_text(self.font_small, label, hx + 11, hy - 8, color=(230, 240, 255))

    def _draw_weather_panel(self, theme_color):
        data, status = self.weather, self.weather_status
        if not data and not status:
            return
        rect = overlay_rect(self.width, self.height, 'weather')
        x, y, w, h = rect.x, rect.y, rect.width, rect.height
        self._overlay_frame(rect, theme_color, "ATMOSPHERE / CURRENT CONDITIONS",
                            'Say "Aurora, close weather"')
        expanded = h >= 440
        if status:
            loading = status['status'] == 'loading'
            label = "Acquiring conditions" if loading else "Unable to update weather"
            self._blit_text(self.font, self._fit_text(self.font, label, w - 48), x + 24, y + 68,
                            color=(241, 221, 184))
            if loading:
                phase = 0 if self.reduced_motion else (self.elapsed * .35) % 1
                self._draw_rect(x + 24, y + 101, w - 48, 2, *theme_color, .15)
                self._draw_rect(x + 24 + phase * (w - 80), y + 100, 32, 3, *theme_color, .8)
            lines = self._wrap_text(self.font_small, status['message'], w - 48)
            _, _, visible = visible_page(lines, 0, h - 172, 19)
            for i, line in enumerate(visible):
                self._blit_text(self.font_small, line, x + 24, y + 118 + i * 19,
                                color=(197, 176, 142))
            return
        # A single clipped location keeps small overlays inside their bounds.
        self._blit_text(self.font, self._fit_text(self.font, data['location'], w - 48),
                        x + 24, y + 64, color=(241, 226, 202))
        if expanded:
            icon_x, icon_y = x + w - 78, y + 158
            if data['condition'] == 'clear_night':
                glColor4f(.96, .8, .48, .9)
                self._draw_circle_2d(icon_x, icon_y, 25, segments=48)
                glColor4f(.035, .027, .017, 1)
                self._draw_circle_2d(icon_x + 12, icon_y - 8, 23, segments=48)
            else:
                bob = 0 if self.reduced_motion else math.sin(self.elapsed * .7) * 2
                self._draw_weather_icon(data['condition'], icon_x, icon_y + bob, 34, theme_color)
        temp_y = y + (119 if expanded else 93)
        self._blit_text(self.font_huge if expanded else self.font_big,
                        f"{round(data['temp_c'])}°C", x + 24, temp_y, color=(255, 223, 157))
        description = self._fit_text(self.font_small, data['description'].upper(), w - 48)
        self._blit_text(self.font_small, description, x + 24, temp_y + (68 if expanded else 36),
                        color=(199, 170, 118))
        humidity = '--' if data['humidity'] is None else f"{round(data['humidity'])}%"
        wind = '--' if data['wind_kph'] is None else f"{round(data['wind_kph'])} km/h"
        if expanded:
            metrics_y = y + 238
            for i, (label, value) in enumerate((("HUMIDITY", humidity), ("WIND", wind))):
                mx = x + 24 + i * (w - 48) / 2
                self._draw_rect(mx, metrics_y, (w - 64) / 2, 72, *theme_color, .045)
                self._blit_text(self.font_small, label, mx + 12, metrics_y + 12, color=(156, 127, 81))
                self._blit_text(self.font_big, value, mx + 12, metrics_y + 32, color=(230, 213, 180))
            source_y = y + h - 95
            self._blit_text(self.font_small, self._fit_text(self.font_small,
                f"{data['source']} / {data['updated_at']}", w - 48), x + 24, source_y, color=(158, 137, 101))
            self._blit_text(self.font_small, self._fit_text(self.font_small,
                data.get('timezone', ''), w - 48), x + 24, source_y + 19, color=(126, 111, 86))
        else:
            stats = f"Humidity {humidity}  /  Wind {wind}"
            self._blit_text(self.font_small, stats, x + 24, y + 154, color=(197, 177, 144))
            # Source stays visible in compact view; timestamp is clipped by pixels.
            self._blit_text(self.font_small, self._fit_text(self.font_small,
                f"{data['source']} / {data['updated_at']}", w - 48),
                x + 24, y + h - 63, color=(148, 126, 91))

    def _wrap_text(self, font, text, max_width):
        if not font:
            return [text]
        lines, current = [], ""
        for word in text.split():
            candidate = (current + " " + word).strip()
            if font.size(candidate)[0] <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = ""
            # Break long URLs/tokens too; word-only wrapping lets these escape cards.
            for char in word:
                if current and font.size(current + char)[0] > max_width:
                    lines.append(current)
                    current = ""
                current += char
        if current:
            lines.append(current)
        return lines

    def _draw_info_card(self, theme_color):
        card = self.info_card
        if not card:
            return
        rect = overlay_rect(self.width, self.height, 'answer')
        x, y, w, h = rect.x, rect.y, rect.width, rect.height
        lines = self._wrap_text(self.font, card['answer'], w - 56)
        start_y = y + 128
        self.info_scroll, self._info_page_lines, visible = visible_page(
            lines, self.info_scroll, h - 190)
        page = self.info_scroll // self._info_page_lines + 1
        pages = max(1, math.ceil(len(lines) / self._info_page_lines))
        hint = '"Aurora, read more" / "Scroll up"' if pages > 1 else '"Aurora, close answer"'
        self._overlay_frame(rect, theme_color, f"AURORA / RESPONSE   {page:02d} / {pages:02d}", hint)
        question = self._wrap_text(self.font_small, card['question'], w - 56)
        for i, line in enumerate(question[:2]):
            self._blit_text(self.font_small, line, x + 28, y + 67 + i * 19, color=(175, 148, 108))
        for i, line in enumerate(visible):
            self._blit_text(self.font, line, x + 28, start_y + i * 23, color=(239, 224, 197))
        track_y = y + h - 53
        self._draw_rect(x + 24, track_y, w - 48, 2, *theme_color, .12)
        fraction = min(1, (self.info_scroll + len(visible)) / max(1, len(lines)))
        self._draw_rect(x + 24, track_y, (w - 48) * fraction, 2, *theme_color, .65)

    def _graph_positions(self):
        """Screen-space node layout: the constellation spans the whole
        window, with long connectors radiating through the core, like the
        reference dashboards."""
        positions = []
        for _label, fx, fy, _family, _gear in GRAPH_NODES:
            x = min(self.width - 180, max(24, fx * self.width))
            y = min(self.height - 150, max(80, fy * self.height))
            positions.append((x, y))
        return positions

    def _draw_system_graph(self, theme_color):
        """Constellation of live subsystem nodes around the voice core —
        glowing clusters, thin connector lines and boxed uppercase labels,
        in the style of the supplied reference dashboard."""
        docked = bool(self.weather or self.weather_status or self.info_card) and self.width >= 1000
        free = ParticleCore.layout(self.width, self.height, False)
        side = ParticleCore.layout(self.width, self.height, True)
        cx, cy, radius = tuple(a + (b - a) * self._core_dock for a, b in zip(free, side))
        fade = self._materialize_progress()
        positions = self._graph_positions()

        # soft multi-hue nebula haze behind the core (additive)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        for (r, g, b), dx, dy, scale in (((1.0, .3, .8), -.4, -.15, 1.2),
                                         ((.5, .35, 1.0), .4, -.3, 1.3),
                                         ((.2, .7, 1.0), .1, .4, 1.1)):
            glBegin(GL_TRIANGLE_FAN)
            glColor4f(r, g, b, 0.22 * fade)
            glVertex2f(cx, cy)
            glColor4f(r, g, b, 0)
            for i in range(49):
                angle = i * math.tau / 48
                glVertex2f(cx + dx * radius + math.cos(angle) * radius * scale,
                           cy + dy * radius + math.sin(angle) * radius * scale)
            glEnd()
        # white swirl arms sweeping around the nucleus, like the reference
        if not self.reduced_motion:
            glLineWidth(1.5)
            for arm in range(2):
                base = self.elapsed * 0.6 + arm * math.pi
                glBegin(GL_LINE_STRIP)
                for i in range(25):
                    t = i / 24
                    angle = base + t * 2.4
                    rr = radius * (0.10 + 0.32 * t)
                    glColor4f(0.9, 0.95, 1.0, (1 - t) * 0.45 * fade)
                    glVertex2f(cx + math.cos(angle) * rr, cy + math.sin(angle) * rr * 0.9)
                glEnd()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        # connector lines: hub spokes through gear nodes + chained links,
        # some drawn dashed like the reference's long constellation edges
        glLineWidth(1.0)
        glBegin(GL_LINES)
        for node, (x, y) in zip(GRAPH_NODES, positions):
            if node[4]:
                glColor4f(0.78, 0.84, 1.0, 0.12 * fade)
                glVertex2f(cx, cy); glVertex2f(x, y)
        for a, b in GRAPH_CROSS_LINKS:
            ax, ay = positions[a]
            bx, by = positions[b]
            glColor4f(0.78, 0.84, 1.0, 0.09 * fade)
            if (a + b) % 3 == 0:  # dashed variant
                length = math.hypot(bx - ax, by - ay) or 1
                steps = max(2, int(length / 14))
                for s in range(0, steps, 2):
                    t0, t1 = s / steps, min(1, (s + 1) / steps)
                    glVertex2f(ax + (bx - ax) * t0, ay + (by - ay) * t0)
                    glVertex2f(ax + (bx - ax) * t1, ay + (by - ay) * t1)
            else:
                glVertex2f(ax, ay); glVertex2f(bx, by)
        glEnd()

        for index, (node, (x, y)) in enumerate(zip(GRAPH_NODES, positions)):
            label, _fx, _fy, family, gear = node
            r, g, b = GRAPH_FAMILIES[family]
            if label == "VOICE LINK":
                status = 1.0 if self.voice_available else 0.35
            elif label == "WEATHER DOCK":
                status = 1.0 if self.weather else 0.5
            else:
                status = 0.75 + 0.25 * math.sin(self.elapsed * 1.7 + index)
            glow = status * fade
            if gear:
                # radial "flower" cluster: dotted ring + spokes
                glColor4f(r, g, b, 0.10 * glow)
                self._draw_circle_2d(x, y, 11, segments=16)
                glBegin(GL_LINES)
                for s in range(8):
                    a = s * math.tau / 8 + self.elapsed * (0 if self.reduced_motion else 0.15)
                    glColor4f(r, g, b, 0.5 * glow)
                    glVertex2f(x + math.cos(a) * 6, y + math.sin(a) * 6)
                    glVertex2f(x + math.cos(a) * 13, y + math.sin(a) * 13)
                glEnd()
                for d in range(14):
                    a = d * math.tau / 14
                    glColor4f(r, g, b, 0.55 * glow)
                    self._draw_circle_2d(x + math.cos(a) * 17, y + math.sin(a) * 17, 1.2, segments=6)
                glColor4f(min(1, r + .3), min(1, g + .3), min(1, b + .3), 0.95 * glow)
                self._draw_circle_2d(x, y, 2.6, segments=10)
            else:
                # tan terminal dot with a darker ring, like the reference
                glColor4f(r, g, b, 0.14 * glow)
                self._draw_circle_2d(x, y, 8, segments=12)
                glColor4f(0.16, 0.13, 0.10, 0.9 * fade)
                self._draw_circle_2d(x, y, 4.6, segments=12)
                glColor4f(min(1, r + .15), min(1, g + .15), min(1, b + .15), 0.9 * glow)
                self._draw_circle_2d(x, y, 3.4, segments=12)

            # boxed uppercase label chip
            text = label.upper()
            tw = self.font_small.size(text)[0] if self.font_small else len(text) * 7
            lx, ly = x + 12, y - 9
            if lx + tw + 12 > self.width - 8:
                lx = x - tw - 24
            self._draw_rect(lx, ly, tw + 12, 17, 0.02, 0.02, 0.03, 0.72 * fade)
            self._draw_rect(lx, ly, tw + 12, 17, r, g, b, 0.28 * fade, filled=False)
            self._blit_text(self.font_small, text, lx + 6, ly + 3,
                            color=(int(165 + 70 * r * status), int(165 + 70 * g * status),
                                   int(165 + 70 * b * status)))

    def _draw_idle_indicator(self, theme_color):
        """Multi-hue nebula particle sphere with orbital trails and a hot
        blue-white nucleus, in the style of the supplied reference."""
        docked = bool(self.weather or self.weather_status or self.info_card) and self.width >= 1000
        target = 1.0 if docked else 0.0
        previous = getattr(self, '_core_dock', target)
        self._core_dock = target if self.reduced_motion else previous + (target - previous) * .12
        free = ParticleCore.layout(self.width, self.height, False)
        side = ParticleCore.layout(self.width, self.height, True)
        cx, cy, radius = tuple(a + (b - a) * self._core_dock for a, b in zip(free, side))
        motion_time, energy = self.core_animation.advance(self.elapsed, self.state, self.reduced_motion)
        frame = self.particle_core.frame(motion_time, self.state,
                                         quality=self.particle_quality, energy=energy)
        color = self._voice_color()
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        try:
            batches, line_batch = build_buffers(frame, (cx, cy), radius, color, self.brightness,
                                                palette=NEBULA_PALETTE)
            # A handful of vertex-array draws replaces thousands of Python GL calls.
            glPushClientAttrib(GL_CLIENT_VERTEX_ARRAY_BIT)
            glPushAttrib(GL_POINT_BIT)
            try:
                glEnableClientState(GL_VERTEX_ARRAY)
                glEnableClientState(GL_COLOR_ARRAY)
                glEnable(GL_POINT_SMOOTH)
                def draw_batch(vertices, colors, primitive):
                    if not vertices:
                        return
                    glVertexPointer(2, GL_FLOAT, 0, ctypes.c_void_p(vertices.buffer_info()[0]))
                    glColorPointer(4, GL_FLOAT, 0, ctypes.c_void_p(colors.buffer_info()[0]))
                    glDrawArrays(primitive, 0, len(vertices) // 2)
                for size, (vertices, colors) in batches.items():
                    # Broad dim halo plus a sharp head: real moving points, no sprites/images.
                    halo = colors[:]
                    for index in range(3, len(halo), 4):
                        halo[index] *= .12
                    point_scale = max(.75, min(1.25, math.sqrt(radius / 200)))
                    if self.particle_quality != "performance" or size == 3:
                        glPointSize((size * 2.5 + 1) * point_scale)
                        draw_batch(vertices, halo, GL_POINTS)
                    if self.particle_quality == "cinematic":
                        for index in range(3, len(halo), 4):
                            halo[index] *= .45
                        glPointSize((size * 4 + 2) * point_scale)
                        draw_batch(vertices, halo, GL_POINTS)
                    glPointSize(size * point_scale)
                    draw_batch(vertices, colors, GL_POINTS)
                glLineWidth(1.2)
                draw_batch(*line_batch, GL_LINES)
            finally:
                glPopAttrib()
                glPopClientAttrib()
            # Smooth radial glow, not nested opaque disks; white-hot central seed.
            for scale, alpha in ((.4, .09), (.2, .22), (.08, .85)):
                glBegin(GL_TRIANGLE_FAN)
                glColor4f(.5 + .4 * frame.energy, .68 + .27 * frame.energy, 1.0, alpha)
                glVertex2f(cx, cy)
                glColor4f(.3, .45, 1.0, 0)
                for i in range(65):
                    angle = i * math.tau / 64
                    glVertex2f(cx + math.cos(angle) * radius * scale,
                               cy + math.sin(angle) * radius * scale)
                glEnd()
            glColor4f(.88, .94, 1.0, .95)
            self._draw_circle_2d(cx, cy, radius * .021, segments=32)
        finally:
            glPointSize(1)
            glLineWidth(1)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

    def _draw_dashboard(self, theme_color):
        self._begin_ortho()
        self._draw_space_background()
        overlay_active = bool(self.weather or self.weather_status or self.info_card)
        if self.mode in ("empty", "info") and (not overlay_active or self.width >= 1000):
            if self.mode == "empty":
                self._draw_system_graph(theme_color)
            self._draw_idle_indicator(theme_color)
        if self.mode == "info":
            self._draw_info_card(theme_color)
        self._draw_top_bar(theme_color)
        if self.show_diagnostics and self.width >= 1100 and self.height >= 720:
            self._draw_system_panel(theme_color)
            if not self.weather and not self.weather_status and self.mode != "info":
                self._draw_event_log()
        self._draw_weather_panel(theme_color)
        self._draw_bottom_bar(theme_color)
        self._draw_telemetry()
        if self.show_help:
            self._draw_help(theme_color)
        self._end_ortho()

    # ---- main render ------------------------------------------------------

    def render(self, pinch_amount=0.0):
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)


        base_color = {
            "listening": COLOR_LISTENING,
            "speaking": COLOR_SPEAKING,
            "thinking": COLOR_THINKING,
        }.get(self.state, self._current_theme_color())
        if self.mode == "solar_system":
            base_color = (1.0, 0.75, 0.2)  # sunlight tint regardless of theme

        pulse = 0.5 + 0.5 * math.sin(self.pulse_phase)
        brightness = (0.6 + 0.4 * pulse + pinch_amount * 0.3) * self.brightness

        t = self.flash_intensity
        color = tuple(base_color[i] * (1 - t) + self.flash_color[i] * t for i in range(3))

        if self.mode not in ("empty", "info"):
            self._draw_base_plate(color, brightness * 0.7)

        glPushMatrix()
        glTranslatef(self.translate_x + self._dock_shift, self.translate_y, 0.0)
        glRotatef(self.rotation_x, 1, 0, 0)
        glRotatef(self.rotation_y, 0, 1, 0)
        glRotatef(self.roll, 0, 0, 1)

        glLineWidth(2.0)
        materialize_ease = self._materialize_progress()
        scale = (1.0 + pinch_amount * 0.15) * self.zoom * materialize_ease

        if self.mode == "shape":
            glPushMatrix()
            glScalef(scale, scale, scale)
            # Bake in a fixed 3/4-view tilt so the shape never looks flat
            # from directly in front (a cube viewed perfectly face-on, or
            # a torus viewed edge-on, both look 2D otherwise) — hand
            # rotation still adds on top of this baseline.
            glRotatef(25, 1, 0, 0)
            glRotatef(35, 0, 1, 0)
            glColor4f(color[0] * brightness, color[1] * brightness, color[2] * brightness, 0.95)
            self._draw_shape(self.shape_name)
            glPopMatrix()
        else:
            for idx, orbit in enumerate(self.orbits):
                is_selected = (idx == self.selected_index)
                glPushMatrix()
                glRotatef(orbit["tilt"], 1, 1, 0)
                glScalef(scale, scale, scale)

                if self.mode == "atom" and "shell" in orbit:
                    shell_color = SHELL_COLORS[orbit["shell"] % len(SHELL_COLORS)]
                    ring_color = tuple(c * brightness for c in shell_color)
                else:
                    ring_color = (color[0] * brightness, color[1] * brightness, color[2] * brightness)

                path_alpha = 1.0 if is_selected else 0.6
                glColor4f(*ring_color, path_alpha)
                self._draw_ring(orbit["radius"])

                for phase in orbit["electrons"]:
                    angle = math.radians(phase)
                    nx = orbit["radius"] * math.cos(angle)
                    ny = orbit["radius"] * math.sin(angle)
                    glColor4f(*ring_color, path_alpha)
                    glBegin(GL_LINES)
                    glVertex3f(0, 0, 0)
                    glVertex3f(nx, ny, 0)
                    glEnd()

                    glPushMatrix()
                    glTranslatef(nx, ny, 0)
                    if is_selected:
                        self._draw_glow_fan(0.09, 1.0, 1.0, 1.0, passes=3)
                        glColor4f(1.0, 1.0, 1.0, 1.0)
                        self._draw_node_fan(0.09 + 0.02 * pulse)
                        glColor4f(1.0, 1.0, 1.0, 0.9)
                        glBegin(GL_LINE_LOOP)
                        for i in range(4):
                            a = math.radians(45 + i * 90)
                            glVertex3f(0.16 * math.cos(a), 0.16 * math.sin(a), 0)
                        glEnd()
                    else:
                        nc = tuple(min(1.0, c + 0.2) for c in ring_color)
                        self._draw_glow_fan(0.07, *nc, passes=2)
                        glColor4f(*nc, 1.0)
                        self._draw_node_fan(0.07)
                    glPopMatrix()

                glPopMatrix()

        if self.mode not in ("shape", "empty", "info"):
            glPushMatrix()
            glScalef(scale, scale, scale)
            self._draw_sweep_arc(1.9)
            core_color = (min(1.0, color[0] * brightness + 0.3),
                          min(1.0, color[1] * brightness + 0.3),
                          min(1.0, color[2] * brightness + 0.3))
            if self.mode == "atom":
                self._draw_nucleus_particles(self.protons, self.neutrons, brightness)
            else:
                core_radius = 0.22 if self.mode == "solar_system" else 0.14 + 0.05 * pulse
                self._draw_glow_fan(core_radius, *core_color, passes=5)
                glColor4f(*core_color, 1.0)
                self._draw_node_fan(core_radius)
            glPopMatrix()

        glPopMatrix()

        self._draw_dashboard(color)
        pygame.display.flip()

    def close(self):
        pygame.quit()
