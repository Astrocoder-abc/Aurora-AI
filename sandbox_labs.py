"""
Aurora Sandbox Labs - Physics Sandbox, Molecule Builder, Circuit Sandbox.
One file, no changes to hologram.py needed (it hooks itself in on first use).

PHYSICS (voice):   "create a projectile with 20 m/s velocity" (also: at 30 degrees,
                   from 10 meters, on the moon) / "drop a ball from 15 meters" /
                   "create a pendulum with length 2 meters" / "create a spring with
                   constant 40" / "clear physics"
MOLECULES:         "build a molecule" / "add carbon" / "add 4 hydrogens" / "fill hydrogens"
                   "double bond between 1 and 2" / "select atom 2" / "remove atom 3"
                   "build a water molecule" / "stop spinning" / "molecule info"
                   Mouse: drag = rotate in 3D, click atom = select. Hand movement rotates too.
CIRCUITS:          "circuit sandbox" / "add a 220 ohm resistor" / "add a green LED" /
                   "add a battery" / "add an arduino pin" / "add a switch" /
                   "add a 1000 microfarad capacitor" / "wire it up" / "toggle the switch"
                   "build an LED circuit" / "build a blink circuit" / "build an RC circuit"
                   "measure the circuit" / "undo" / "clear circuit"
                   Mouse: click a dot then another dot = wire, click switch/arduino = toggle,
                   click a part = select (say "delete selected"), right-click cancels wire.
                   Sim is real (nodal analysis): LEDs burn out without a resistor.
Close with: "close the physics / molecule / circuit sandbox".

WIRING (voice_assistant.py, inside _handle_local_command right after the "stop" block):
    from jarvis_ui import sandbox_labs        # (top of file)
    if sandbox_labs.handle_command(self.hologram, t, self._speak):
        return True
"""

import math
import re
import threading
import time
from collections import Counter

import numpy as np
import pygame
from OpenGL.GL import *

from jarvis_ui.hologram import MOLECULES, ATOM_COLORS, ATOM_RADII

PALETTE = [(0.3, 0.85, 1.0), (1.0, 0.6, 0.25), (0.5, 1.0, 0.5), (1.0, 0.4, 0.7), (0.9, 0.9, 0.3)]
G = 30  # circuit grid size in px
NUMW = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
        "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _num(pattern, t, default=None):
    m = re.search(pattern, t)
    return float(m.group(1)) if m else default


def _int(word):
    word = (word or "").strip()
    return int(word) if word.isdigit() else NUMW.get(word)


def _nice(v):
    if v <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if m * mag >= v:
            return m * mag
    return 10 * mag


def _line(pts, col, a=1.0, wd=2.0, mode=GL_LINE_STRIP):
    glLineWidth(wd)
    glColor4f(col[0], col[1], col[2], a)
    glBegin(mode)
    for p in pts:
        glVertex2f(p[0], p[1])
    glEnd()


def _eng(v):
    return f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"


# ============================================================================
# PHYSICS
# ============================================================================

GRAVITY = {"moon": 1.62, "mars": 3.71, "jupiter": 24.79, "venus": 8.87, "earth": 9.81}
_VEL = (r"(\d+(?:\.\d+)?)\s*(?:m/s|m per s(?:ec(?:ond)?)?|meters? per second|metres? per second|"
        r"meters? a second|metres? a second|mps)")
_VERB = r"\b(?:create|make|launch|fire|shoot|throw|kick|simulate|add|build|drop|start)\b"


def _physics_kind(t):
    if not re.search(_VERB, t):
        return None
    if "pendulum" in t:
        return "pendulum"
    if re.search(r"\bspring\b", t):
        return "spring"
    noun = re.search(r"projectile|cannon|\bball\b|rocket|arrow|\bstone\b|\brock\b", t)
    if re.search(r"free ?fall", t) or (noun and re.search(r"\bdrop(?:ped)?\b", t)):
        return "drop"
    return "proj" if noun else None


def _proj_state(s):
    tt = min(s["t"], s["T"])
    vx, vy0 = s["v"] * math.cos(s["a"]), s["v"] * math.sin(s["a"])
    y = max(0.0, s["h0"] + vy0 * tt - 0.5 * s["g"] * tt * tt)
    return vx * tt, y, vx, vy0 - s["g"] * tt


class Physics:
    def __init__(self):
        self.sims = []

    def create(self, t):
        g, body = 9.81, "earth"
        for name, val in GRAVITY.items():
            if re.search(rf"\b{name}\b", t):
                g, body = val, name
        ang = _num(r"(\d+(?:\.\d+)?)\s*(?:degrees?|deg|°)", t)
        kind = _physics_kind(t)

        if kind == "pendulum":
            L = (_num(r"length\s*(?:of\s*)?(\d+(?:\.\d+)?)", t)
                 or _num(r"(\d+(?:\.\d+)?)\s*(?:m\b|meters?|metres?)", t) or 1.5)
            a = ang if ang is not None else 45.0
            self.sims = [{"kind": "pendulum", "L": L, "g": g, "th": math.radians(a), "w": 0.0, "trail": []}]
            return (f"Pendulum with a {L:g} meter string released at {a:g} degrees on {body}. "
                    f"Period is about {2 * math.pi * math.sqrt(L / g):.1f} seconds.")

        if kind == "spring":
            k = _num(r"(?:constant|stiffness|k)\s*(?:of\s*|is\s*)?(\d+(?:\.\d+)?)", t) or 20.0
            m = _num(r"(\d+(?:\.\d+)?)\s*(?:kg|kilograms?|kilos?)", t) or 1.0
            self.sims = [{"kind": "spring", "k": k, "m": m, "x": 0.5, "v": 0.0, "A": 0.5}]
            return (f"Mass-spring system: {m:g} kilograms on a {k:g} newton per meter spring. "
                    f"Period is {2 * math.pi * math.sqrt(m / k):.2f} seconds.")

        vm = re.search(_VEL, t)
        v = float(vm.group(1)) if vm else _num(r"(?:velocity|speed)\s*(?:of\s*)?(\d+(?:\.\d+)?)", t)
        t2 = re.sub(_VEL, "", t)
        h0 = (_num(r"(\d+(?:\.\d+)?)\s*(?:m|meters?|metres?)\s*(?:high|tall|cliff|tower|building|above)", t2)
              or _num(r"(?:from|off)\s*(?:a\s*|an\s*|the\s*)?(?:height\s*of\s*|top\s*of\s*)?"
                      r"(\d+(?:\.\d+)?)\s*(?:m\b|meters?|metres?)", t2) or 0.0)
        if kind == "drop":
            v, ang, h0 = 0.0, 0.0, h0 or 10.0
        else:
            v = v if v is not None else 20.0
            ang = ang if ang is not None else 45.0
        a = math.radians(ang)
        vy0 = v * math.sin(a)
        T = (vy0 + math.sqrt(vy0 * vy0 + 2 * g * h0)) / g
        R = v * math.cos(a) * T
        H = h0 + (vy0 * vy0 / (2 * g) if vy0 > 0 else 0.0)
        sim = {"kind": "proj", "v": v, "a": a, "deg": ang, "h0": h0, "g": g, "t": 0.0, "hold": 0.0,
               "T": T, "R": R, "H": H, "trail": []}
        if self.sims and self.sims[0]["kind"] != "proj":
            self.sims = []
        self.sims = (self.sims + [sim])[-4:]
        if kind == "drop":
            return f"Dropping a ball from {h0:g} meters on {body}. It lands in {T:.2f} seconds."
        return (f"Projectile at {v:g} meters per second, {ang:g} degrees on {body}. "
                f"Range {R:.1f} meters, peak height {H:.1f} meters, flight time {T:.2f} seconds.")

    def step(self, dt):
        for s in self.sims:
            k = s["kind"]
            if k == "proj":
                if s["t"] < s["T"]:
                    s["t"] += dt
                    x, y, _, _ = _proj_state(s)
                    s["trail"].append((x, y))
                    if len(s["trail"]) > 500:
                        s["trail"].pop(0)
                else:
                    s["hold"] += dt
                    if s["hold"] > 2.5:
                        s["t"], s["hold"], s["trail"] = 0.0, 0.0, []
            elif k == "pendulum":
                for _ in range(8):
                    ds = dt / 8
                    s["w"] += (-(s["g"] / s["L"]) * math.sin(s["th"]) - 0.03 * s["w"]) * ds
                    s["th"] += s["w"] * ds
                s["trail"].append((s["L"] * math.sin(s["th"]), s["L"] * math.cos(s["th"])))
                if len(s["trail"]) > 120:
                    s["trail"].pop(0)
            else:
                for _ in range(8):
                    ds = dt / 8
                    s["v"] += (-s["k"] * s["x"] - 0.15 * s["v"]) / s["m"] * ds
                    s["x"] += s["v"] * ds


# ============================================================================
# MOLECULE BUILDER
# ============================================================================

VALENCE = {"H": 1, "C": 4, "N": 3, "O": 2, "S": 2, "Cl": 1, "P": 3, "F": 1, "Br": 1}
MASS = {"H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999, "S": 32.06, "Cl": 35.45,
        "P": 30.974, "F": 18.998, "Br": 79.904}
COLORS = dict(ATOM_COLORS)
COLORS.update({"F": (0.5, 1.0, 0.5), "Br": (0.65, 0.2, 0.1)})
RADII = dict(ATOM_RADII)
RADII.update({"F": 0.12, "Br": 0.17})
EL_WORDS = {"hydrogen": "H", "carbon": "C", "oxygen": "O", "nitrogen": "N", "sulfur": "S", "sulphur": "S",
            "chlorine": "Cl", "phosphorus": "P", "fluorine": "F", "bromine": "Br"}
BOND_ORDER = {"carbon dioxide": 2, "oxygen": 2, "nitrogen": 3}
FORMULA_NAMES = {"H2O": "water", "CH4": "methane", "CO2": "carbon dioxide", "H3N": "ammonia", "O2": "oxygen",
                 "N2": "nitrogen", "H2": "hydrogen gas", "C6H6": "benzene", "C2H6": "ethane",
                 "C2H4": "ethene", "C2H2": "ethyne", "CH2O": "formaldehyde", "CH4O": "methanol",
                 "C2H6O": "ethanol", "ClH": "hydrogen chloride", "H2S": "hydrogen sulfide"}


def _rotate(p, rx, ry, rz):
    x, y, z = p
    c, s = math.cos(ry), math.sin(ry)
    x, z = x * c + z * s, -x * s + z * c
    c, s = math.cos(rx), math.sin(rx)
    y, z = y * c - z * s, y * s + z * c
    c, s = math.cos(rz), math.sin(rz)
    x, y = x * c - y * s, x * s + y * c
    return x, y, z


class Molecule:
    def __init__(self):
        self.atoms, self.bonds, self.sel = [], [], None
        self.rx, self.ry, self.spin, self.dragging = 0.35, 0.6, True, False
        self.screen = []  # (sx, sy, radius, z) per atom from the last draw

    def clear(self):
        self.atoms, self.bonds, self.sel = [], [], None

    def _valence_used(self, i):
        return sum(o for a, b, o in self.bonds if i in (a, b))

    def add(self, el, parent=None, select=True):
        if not self.atoms:
            self.atoms.append([el, 0.0, 0.0, 0.0])
            self.sel = 0
            return 0
        p = parent if parent is not None else (self.sel if self.sel is not None and self.sel < len(self.atoms) else 0)
        px, py, pz = self.atoms[p][1:]
        bl = 1.0 if "H" in (el, self.atoms[p][0]) else 1.4
        best, bs = None, -1.0
        for k in range(48):  # pick the free direction farthest from every existing atom
            y = 1 - 2 * (k + 0.5) / 48
            r = math.sqrt(1 - y * y)
            th = 2.399963 * k
            cand = (px + bl * r * math.cos(th), py + bl * y, pz + bl * r * math.sin(th))
            sc = min(math.dist(cand, a[1:]) for a in self.atoms)
            if sc > bs:
                bs, best = sc, cand
        self.atoms.append([el, *best])
        n = len(self.atoms) - 1
        self.bonds.append((p, n, 1))
        if select and el != "H":
            self.sel = n
        return n

    def fill_hydrogens(self):
        added = 0
        for i in range(len(self.atoms)):
            el = self.atoms[i][0]
            if el == "H":
                continue
            for _ in range(max(0, VALENCE.get(el, 0) - self._valence_used(i))):
                self.add("H", parent=i, select=False)
                added += 1
        return added

    def set_bond(self, i, j, order):
        self.bonds = [b for b in self.bonds if set(b[:2]) != {i, j}] + [(i, j, order)]

    def remove(self, i):
        if not 0 <= i < len(self.atoms):
            return False
        del self.atoms[i]
        self.bonds = [(a - (a > i), b - (b > i), o) for a, b, o in self.bonds if i not in (a, b)]
        self.sel = None if self.sel == i or self.sel is None else self.sel - (self.sel > i)
        return True

    def load_preset(self, name):
        d = MOLECULES[name]
        self.atoms = [[e, x, y, z] for e, x, y, z in d["atoms"]]
        self.bonds = [(i, j, BOND_ORDER.get(name, 1)) for i, j in d["bonds"]]
        self.sel = None

    def formula(self):
        c = Counter(a[0] for a in self.atoms)
        order = (["C", "H"] + sorted(k for k in c if k not in "CH")) if "C" in c else sorted(c)
        return "".join(f"{k}{c[k] if c[k] > 1 else ''}" for k in order if k in c)

    def mass(self):
        return sum(MASS.get(a[0], 0) for a in self.atoms)

    def name(self):
        return FORMULA_NAMES.get(self.formula(), "")

    def pick(self, mx, my):
        hits = [(z, i) for i, (sx, sy, r, z) in enumerate(self.screen) if math.hypot(mx - sx, my - sy) <= r + 6]
        return min(hits)[1] if hits else None


# ============================================================================
# CIRCUIT SANDBOX (real nodal analysis, transient caps, nonlinear LEDs)
# ============================================================================

SOURCES = ("battery", "ard")
LED_RGB = {"red": (1.0, 0.15, 0.1), "green": (0.2, 1.0, 0.3), "blue": (0.25, 0.45, 1.0),
           "yellow": (1.0, 0.9, 0.2), "white": (1.0, 1.0, 1.0)}
LED_VF = {"red": 1.9, "yellow": 2.1, "green": 2.2, "blue": 3.0, "white": 3.2}
SIM_DT = 0.005


class Circuit:
    def __init__(self):
        self.comps, self.wires, self.history = [], [], []
        self.nid, self.sel, self.wire_start = 1, None, None
        self.t, self.paused = 0.0, False
        self.cols, self.rows = 20, 8

    # ---- editing ----
    @staticmethod
    def term(c):
        p = (c["gx"], c["gy"])
        return p if c["kind"] == "gnd" else ((c["gx"] - 1, c["gy"]), (c["gx"] + 1, c["gy"]))

    def _slot(self):
        per = max(1, (self.cols - 2) // 4)
        occ = {(c["gx"], c["gy"]) for c in self.comps}
        k = 0
        while True:
            s = (3 + 4 * (k % per), 3 + 6 * (k // per))
            if s not in occ:
                return s
            k += 1

    def add(self, kind, val=None, color="red"):
        c = {"id": self.nid, "kind": kind, "closed": True, "blink": True, "color": color,
             "vf": LED_VF.get(color, 2.0), "vc": 0.0, "burnt": False, "on": False, "v": 0.0, "i": 0.0}
        c["val"] = val if val is not None else {"battery": 5.0, "ard": 5.0, "resistor": 220.0, "capacitor": 1000.0,
                                                "bulb": 30.0}.get(kind, 0.0)
        c["gx"], c["gy"] = self._slot()
        self.nid += 1
        self.comps.append(c)
        self.history.append(("c", c))
        return c

    def _route(self, p, q):
        if p[0] == q[0] or p[1] == q[1]:
            return [p, q]
        return [p, (q[0], p[1]), q]

    def add_wire(self, pts, auto=False):
        w = {"pts": pts, "auto": auto}
        self.wires.append(w)
        if not auto:
            self.history.append(("w", w))

    def auto_wire(self):
        chain = [c for c in self.comps if c["kind"] != "gnd"]
        if len(chain) < 2:
            return False
        self.wires = [w for w in self.wires if not w["auto"]]
        for i, c in enumerate(chain):
            p = self.term(c)[1]
            nxt = chain[(i + 1) % len(chain)]
            q = self.term(nxt)[0]
            if i == len(chain) - 1 and p[1] == q[1]:
                self.add_wire([p, (p[0], p[1] + 2), (q[0], q[1] + 2), q], auto=True)
            else:
                self.add_wire(self._route(p, q), auto=True)
        return True

    def undo(self):
        if not self.history:
            return False
        k, o = self.history.pop()
        lst = self.comps if k == "c" else self.wires
        if o in lst:
            lst.remove(o)
        return True

    def delete_selected(self):
        c = next((c for c in self.comps if c["id"] == self.sel), None)
        if not c:
            return False
        self.comps.remove(c)
        self.sel = None
        return True

    def clear(self):
        self.comps, self.wires, self.history, self.sel, self.wire_start = [], [], [], None, None
        self.t = 0.0

    def preset(self, name):
        self.clear()
        if name == "blink":
            self.add("ard"); self.add("resistor", 220); self.add("led")
        elif name == "rc":
            self.add("battery", 5); sw = self.add("switch"); sw["closed"] = False
            self.add("resistor", 1000); self.add("capacitor", 1000)
        else:
            self.add("battery", 5); self.add("resistor", 220); self.add("led")
        self.auto_wire()

    def toggle_switches(self, closed=None):
        n = 0
        for c in self.comps:
            if c["kind"] == "switch":
                c["closed"] = (not c["closed"]) if closed is None else closed
                n += 1
        return n

    def summary(self):
        names = {"ard": "arduino pin", "cap": "capacitor", "capacitor": "capacitor"}
        parts = [f"{names.get(c['kind'], c['kind'])}: {abs(c['v']):.1f} volts, {abs(c['i']) * 1000:.0f} milliamps"
                 + (", burnt out" if c["burnt"] else "")
                 for c in self.comps if c["kind"] != "gnd"]
        return "; ".join(parts[:6]) or "The circuit is empty."

    # ---- simulation ----
    def _src_v(self, s):
        if s["kind"] == "ard" and s["blink"] and int(self.t) % 2 == 1:
            return 0.0
        return s["val"]

    def frame(self, dt):
        if self.paused or not self.comps:
            return
        for _ in range(min(8, max(1, int(dt / SIM_DT)))):
            self._step(SIM_DT)

    def _step(self, dt):
        comps = self.comps
        parent = {}

        def find(p):
            parent.setdefault(p, p)
            while parent[p] != p:
                parent[p] = parent[parent[p]]
                p = parent[p]
            return p

        def union(a, b):
            parent[find(a)] = find(b)

        two = [c for c in comps if c["kind"] != "gnd"]
        grounds = [(c["gx"], c["gy"]) for c in comps if c["kind"] == "gnd"]
        for c in two:
            find(self.term(c)[0]); find(self.term(c)[1])
        for g in grounds[1:]:
            union(g, grounds[0])
        for w in self.wires:
            union(w["pts"][0], w["pts"][-1])
        if not two:
            return
        if grounds:
            ref = find(grounds[0])
        else:
            src = next((c for c in two if c["kind"] in SOURCES), two[0])
            ref = find(self.term(src)[0])
        ids = {}
        for c in two:
            for pt in self.term(c):
                r = find(pt)
                if r != ref and r not in ids:
                    ids[r] = len(ids)
        srcs = [c for c in two if c["kind"] in SOURCES]
        n = len(ids)
        N = n + len(srcs)
        if N == 0:
            return

        def ix(pt):
            return ids.get(find(pt))

        x = np.zeros(N)
        for _ in range(10):
            A = np.zeros((N, N))
            z = np.zeros(N)
            for i in range(n):
                A[i, i] += 1e-9
            for c in two:
                k = c["kind"]
                if k in SOURCES:
                    continue
                a, b = ix(self.term(c)[0]), ix(self.term(c)[1])
                vf = 0.0
                if k in ("resistor", "bulb"):
                    g = 1.0 / max(c["val"], 1e-3)
                elif k == "switch":
                    g = 1e3 if c["closed"] else 1e-9
                elif k == "led":
                    g, vf = (0.2, c["vf"]) if (c["on"] and not c["burnt"]) else (1e-9, 0.0)
                else:  # capacitor, backward-Euler companion model
                    g, vf = c["val"] * 1e-6 / dt, c["vc"]
                c["_g"], c["_vf"] = g, vf
                if a is not None:
                    A[a, a] += g; z[a] += g * vf
                if b is not None:
                    A[b, b] += g; z[b] -= g * vf
                if a is not None and b is not None:
                    A[a, b] -= g; A[b, a] -= g
            for j, s in enumerate(srcs):
                p, q, r = ix(self.term(s)[1]), ix(self.term(s)[0]), n + j
                if p is not None:
                    A[p, r] += 1; A[r, p] += 1
                if q is not None:
                    A[q, r] -= 1; A[r, q] -= 1
                z[r] = self._src_v(s)
            try:
                x = np.linalg.solve(A, z)
            except np.linalg.LinAlgError:
                x = np.linalg.lstsq(A, z, rcond=None)[0]
            changed = False
            for c in two:
                if c["kind"] == "led":
                    i0, i1 = ix(self.term(c)[0]), ix(self.term(c)[1])
                    v = (0.0 if i0 is None else x[i0]) - (0.0 if i1 is None else x[i1])
                    new = v > c["vf"] and not c["burnt"]
                    if new != c["on"]:
                        c["on"], changed = new, True
            if not changed:
                break

        def V(pt):
            i = ix(pt)
            return 0.0 if i is None else float(x[i])

        for j, s in enumerate(srcs):
            s["v"] = V(self.term(s)[1]) - V(self.term(s)[0])
            s["i"] = -float(x[n + j])
        for c in two:
            if c["kind"] in SOURCES:
                continue
            v = V(self.term(c)[0]) - V(self.term(c)[1])
            c["v"] = v
            c["i"] = max(0.0, c["_g"] * (v - c["_vf"])) if c["kind"] == "led" and c["on"] else \
                (0.0 if c["kind"] == "led" else c["_g"] * (v - c["_vf"]))
            if c["kind"] == "capacitor":
                c["vc"] = v
            if c["kind"] == "led" and c["i"] > 0.06:
                c["burnt"], c["on"], c["i"] = True, False, 0.0
        self.t += dt


# ============================================================================
# LABS: state holder, drawing, hologram hook
# ============================================================================

class Labs:
    def __init__(self, holo):
        self.h = holo
        self.active = None
        self.lock = threading.RLock()
        self.phys, self.mol, self.circ = Physics(), Molecule(), Circuit()
        self._last = time.time()
        self._btn = (False, False, False)
        self._mpos = (0, 0)
        self.dt = 0.016
        self.mx = self.my = 0
        self.dm = (0, 0)
        self.inside = self.down = self.edge_l = self.edge_r = False

    def open(self, mode):
        h = self.h
        with self.lock:
            h.hide_weather()
            h.info_card = None
            h.orbits = []
            h.selected_index = None
            h.mode = "info"  # 'info' mode suppresses the 3D core/plate; we draw our own panel
            h.mode_label = {"physics": "PHYSICS SANDBOX", "molecule": "MOLECULE BUILDER",
                            "circuit": "CIRCUIT SANDBOX"}[mode]
            self.active = mode
            self._last = time.time()

    def close(self):
        with self.lock:
            if self.active:
                self.active = None
                self.h.mode = "empty"
                self.h.mode_label = 'STANDBY — SAY "AURORA"'

    def rect(self):
        h = self.h
        return 262, 92, max(320, h.width - 262 - 330), max(240, h.height - 92 - 100)

    def _t(self, s, x, y, col=(200, 225, 245), big=False):
        h = self.h
        h._blit_text(h.font if big else h.font_small, s, x, y, color=col)

    def _circle(self, cx, cy, r, col, a=1.0, filled=True):
        glColor4f(col[0], col[1], col[2], a)
        self.h._draw_circle_2d(cx, cy, r, segments=24, filled=filled)

    # ---- main entry, called every frame from the wrapped dashboard ----
    def draw(self, theme):
        h = self.h
        with self.lock:
            if not self.active:
                return
            if h.mode != "info" or h.info_card is not None:  # something else took over the display
                self.active = None
                return
            now = time.time()
            self.dt = min(0.05, max(1e-3, now - self._last))
            self._last = now
            x, y, w, hh = self.rect()
            mx, my = pygame.mouse.get_pos()
            b = pygame.mouse.get_pressed()
            self.edge_l, self.edge_r = b[0] and not self._btn[0], b[2] and not self._btn[2]
            self.dm = (mx - self._mpos[0], my - self._mpos[1])
            self._btn, self._mpos, self.mx, self.my, self.down = b, (mx, my), mx, my, b[0]
            self.inside = x <= mx <= x + w and y <= my <= y + hh
            h._draw_panel(x, y, w, hh, theme, chamfer=18, fill_alpha=0.66)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            {"physics": self._draw_physics, "molecule": self._draw_molecule,
             "circuit": self._draw_circuit}[self.active](x, y, w, hh)

    # ---- physics drawing ----
    def _draw_physics(self, x, y, w, hh):
        ph = self.phys
        self._t("PHYSICS SANDBOX", x + 18, y + 12, big=True)
        if not ph.sims:
            self._t('Say: "create a projectile with 20 m/s velocity"   |   "create a pendulum with length 2 meters"',
                    x + 18, y + 50)
            self._t('Also: "drop a ball from 15 meters on the moon"   |   "create a spring with constant 40"',
                    x + 18, y + 70)
            return
        ph.step(self.dt)
        kind = ph.sims[0]["kind"]
        {"proj": self._phys_proj, "pendulum": self._phys_pend, "spring": self._phys_spring}[kind](x, y, w, hh)

    def _phys_proj(self, x, y, w, hh):
        sims = self.phys.sims
        xmax = max(max(s["R"] for s in sims), 0.4 * max(s["H"] for s in sims), 1.0)
        ymax = max(max(s["H"] for s in sims), 1.0)
        top = y + 50 + 18 * len(sims) + 10
        x0, y0, W = x + 55, y + hh - 45, w - 100
        Hh = max(60, y0 - top)
        S = min(W / (xmax * 1.05), Hh / (ymax * 1.15))
        step = _nice(xmax / 6)
        k = 0
        while k * step <= xmax * 1.05:
            px = x0 + k * step * S
            _line([(px, y0), (px, y0 + 5)], (0.5, 0.7, 0.9), 0.6, 1.0)
            self._t(f"{k * step:g}m", px - 8, y0 + 8, (110, 150, 180))
            k += 1
        ystep = _nice(ymax / 4)
        k = 1
        while k * ystep <= ymax * 1.1:
            py = y0 - k * ystep * S
            _line([(x0, py), (x0 + W, py)], (0.4, 0.6, 0.8), 0.12, 1.0)
            self._t(f"{k * ystep:g}m", x0 - 45, py - 8, (110, 150, 180))
            k += 1
        _line([(x0, y0), (x0 + W, y0)], (0.6, 0.85, 1.0), 0.9, 2.5)
        h0 = max(s["h0"] for s in sims)
        if h0 > 0:
            _line([(x0 - 14, y0 - h0 * S), (x0, y0 - h0 * S), (x0, y0)], (0.6, 0.85, 1.0), 0.7, 2.5)
        for i, s in enumerate(sims):
            col = PALETTE[i % len(PALETTE)]
            ghost = []
            for j in range(41):
                st = dict(s, t=s["T"] * j / 40)
                gx, gy, _, _ = _proj_state(st)
                ghost.append((x0 + gx * S, y0 - gy * S))
            _line(ghost, col, 0.18, 1.0)
            if len(s["trail"]) > 1:
                _line([(x0 + tx * S, y0 - ty * S) for tx, ty in s["trail"]], col, 0.85, 2.0)
            bx, by, vx, vy = _proj_state(s)
            self._circle(x0 + bx * S, y0 - by * S, 12, col, 0.2)
            self._circle(x0 + bx * S, y0 - by * S, 6, col, 1.0)
            line = (f"#{i + 1} v={s['v']:g}m/s ang={s['deg']:g} g={s['g']:g} | range {s['R']:.1f}m  max h {s['H']:.1f}m  "
                    f"flight {s['T']:.2f}s | t={min(s['t'], s['T']):.2f}  x={bx:.1f}  y={by:.1f}  vy={vy:.1f}")
            self._t(self.h._truncate(line, int(w / 7)), x + 18, y + 48 + 18 * i, tuple(int(c * 255) for c in col))

    def _phys_pend(self, x, y, w, hh):
        s = self.phys.sims[0]
        L = s["L"]
        px, py = x + w / 2, y + 80
        S = min((hh - 160) / L, (w / 2 - 40) / L)
        bx, by = px + L * S * math.sin(s["th"]), py + L * S * math.cos(s["th"])
        _line([(px + tx * S, py + ty * S) for tx, ty in s["trail"]], PALETTE[0], 0.4, 1.5)
        _line([(px - 40, py), (px + 40, py)], (0.6, 0.85, 1.0), 0.9, 4.0)
        _line([(px, py), (bx, by)], (0.8, 0.9, 1.0), 0.9, 2.5)
        self._circle(bx, by, 22, PALETTE[1], 0.2)
        self._circle(bx, by, 12, PALETTE[1], 1.0)
        T0 = 2 * math.pi * math.sqrt(L / s["g"])
        self._t("PHYSICS SANDBOX - PENDULUM", x + 18, y + 12, big=True)
        self._t(f"length {L:g} m  g {s['g']:g}  angle {math.degrees(s['th']):.1f} deg  "
                f"omega {s['w']:.2f} rad/s  period ~{T0:.2f}s", x + 18, y + 44)

    def _phys_spring(self, x, y, w, hh):
        s = self.phys.sims[0]
        cx, top = x + w / 2, y + 70
        eq = top + (hh - 150) * 0.55
        S = min(((hh - 150) * 0.4) / max(s["A"], 0.1), 300)
        my = eq + s["x"] * S
        _line([(cx - 50, top), (cx + 50, top)], (0.6, 0.85, 1.0), 0.9, 4.0)
        _line([(cx - 60, eq), (cx + 60, eq)], (0.5, 0.7, 0.9), 0.25, 1.0)
        pts = [(cx, top)]
        for i in range(1, 25):
            pts.append((cx + (14 if i % 2 else -14), top + (my - 18 - top) * i / 25))
        pts.append((cx, my - 18))
        _line(pts, PALETTE[0], 0.95, 2.0)
        _line([(cx - 30, my - 18), (cx + 30, my - 18), (cx + 30, my + 18), (cx - 30, my + 18), (cx - 30, my - 18)],
              PALETTE[1], 1.0, 3.0)
        om = math.sqrt(s["k"] / s["m"])
        E = 0.5 * s["k"] * s["x"] ** 2 + 0.5 * s["m"] * s["v"] ** 2
        self._t("PHYSICS SANDBOX - SPRING", x + 18, y + 12, big=True)
        self._t(f"k {s['k']:g} N/m  m {s['m']:g} kg  x {s['x']:.2f} m  v {s['v']:.2f} m/s  "
                f"period {2 * math.pi / om:.2f}s  energy {E:.2f} J", x + 18, y + 44)

    # ---- molecule drawing ----
    def _draw_molecule(self, x, y, w, hh):
        h, m = self.h, self.mol
        self._t("MOLECULE BUILDER", x + 18, y + 12, big=True)
        if self.down and self.inside:
            if self.edge_l:
                hit = m.pick(self.mx, self.my)
                if hit is not None:
                    m.sel, m.dragging = hit, False
                else:
                    m.dragging = True
            if m.dragging:
                m.ry += self.dm[0] * 0.01
                m.rx += self.dm[1] * 0.01
        else:
            m.dragging = False
        if m.spin and not m.dragging:
            m.ry += 0.5 * self.dt
        if not m.atoms:
            m.screen = []
            self._t('Say: "add carbon", then "add 4 hydrogens"   |   "build a water molecule"', x + 18, y + 50)
            self._t("Drag with the mouse (or move your hand) to rotate in 3D.", x + 18, y + 70)
            return
        n = len(m.atoms)
        c0 = [sum(a[k] for a in m.atoms) / n for k in (1, 2, 3)]
        pts = [(a[1] - c0[0], a[2] - c0[1], a[3] - c0[2]) for a in m.atoms]
        maxr = max(math.sqrt(sum(c * c for c in p)) for p in pts) + 0.9
        rx = m.rx + h.rotation_x * math.pi / 180
        ry = m.ry + h.rotation_y * math.pi / 180
        rz = h.roll * math.pi / 180
        rot = [_rotate(p, rx, ry, rz) for p in pts]
        d = max(4.0, 3.5 * maxr + 2)
        S = min(120.0, 0.5 * min(w - 60, hh - 170) / maxr)
        cx, cy = x + w / 2, y + hh / 2 - 10
        scr = []
        for (px, py, pz), a in zip(rot, m.atoms):
            f = d / max(0.5, d + pz)
            scr.append((cx + px * S * f, cy - py * S * f, (0.10 + RADII.get(a[0], 0.12)) * S * f, pz))
        m.screen = scr
        items = [((scr[a][3] + scr[b][3]) / 2, 0, (a, b, o)) for a, b, o in m.bonds]
        items += [(scr[i][3], 1, i) for i in range(n)]
        items.sort(key=lambda it: -it[0])
        for _, kind, obj in items:
            if kind == 0:
                a, b, o = obj
                (ax, ay), (bx, by) = scr[a][:2], scr[b][:2]
                dx, dy = bx - ax, by - ay
                ln = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / ln * 4.5, dx / ln * 4.5
                for off in {1: (0,), 2: (-1, 1), 3: (-1.6, 0, 1.6)}.get(o, (0,)):
                    _line([(ax + nx * off, ay + ny * off), (bx + nx * off, by + ny * off)],
                          (0.85, 0.9, 1.0), 0.85, 3.0, GL_LINES)
            else:
                i = obj
                el = m.atoms[i][0]
                col = COLORS.get(el, (0.7, 0.7, 0.7))
                sx, sy, r, _ = scr[i]
                self._circle(sx, sy, r * 1.8, col, 0.16)
                self._circle(sx, sy, r, tuple(c * 0.85 for c in col), 1.0)
                self._circle(sx - r * 0.3, sy - r * 0.3, r * 0.35, (1, 1, 1), 0.45)
                if i == m.sel:
                    glLineWidth(2.5)
                    self._circle(sx, sy, r + 5, (1, 1, 1), 0.95, filled=False)
                self._t(f"{el}{i + 1}", sx + r + 2, sy - r - 8, (200, 225, 245))
        f = m.formula()
        self._t(f"Formula {f}" + (f"  ({m.name()})" if m.name() else ""), x + 18, y + 44)
        self._t(f"Atoms {n}   Bonds {len(m.bonds)}   Mass {m.mass():.2f} g/mol   "
                f"{'(spinning)' if m.spin else ''}", x + 18, y + hh - 44, (140, 180, 210))
        self._t('"add 4 hydrogens" | "double bond between 1 and 2" | "select atom 2" | "fill hydrogens" | drag to rotate',
                x + 18, y + hh - 24, (110, 150, 180))

    # ---- circuit drawing / interaction ----
    def _comp_label(self, c):
        k = c["kind"]
        return {"resistor": f"{_eng(c['val'])} ohm", "battery": f"{c['val']:g} V",
                "ard": f"D13 {c['val']:g}V" + (" blink" if c["blink"] else " high"),
                "led": f"{c['color']} LED", "switch": "closed" if c["closed"] else "open",
                "capacitor": f"{c['val']:g} uF", "bulb": f"{c['val']:g} ohm bulb", "gnd": "GND"}.get(k, k)

    def _draw_circuit(self, x, y, w, hh):
        ci = self.circ
        ci.cols, ci.rows = int((w - 30) // G), int((hh - 100) // G)
        ox, oy = x + (w - ci.cols * G) / 2, y + 55
        self._t("CIRCUIT SANDBOX", x + 18, y + 12, big=True)
        self._t("sim running" if not ci.paused else "sim paused", x + w - 110, y + 16, (140, 180, 210))
        # mouse
        if self.inside and self.edge_r:
            ci.wire_start = None
            ci.sel = None
        if self.inside and self.edge_l:
            gx, gy = round((self.mx - ox) / G), round((self.my - oy) / G)
            gx, gy = max(0, min(ci.cols, gx)), max(0, min(ci.rows, gy))
            comp = next((c for c in ci.comps if (c["gx"], c["gy"]) == (gx, gy)), None)
            if comp and ci.wire_start is None:
                ci.sel = comp["id"]
                if comp["kind"] == "switch":
                    comp["closed"] = not comp["closed"]
                elif comp["kind"] == "ard":
                    comp["blink"] = not comp["blink"]
            else:
                if comp and ci.wire_start is not None:  # snap to the terminal nearest the wire start
                    ts = ci.term(comp)
                    ts = [ts] if comp["kind"] == "gnd" else list(ts)
                    gx, gy = min(ts, key=lambda p: abs(p[0] - ci.wire_start[0]) + abs(p[1] - ci.wire_start[1]))
                if ci.wire_start is None:
                    ci.wire_start = (gx, gy)
                elif (gx, gy) == ci.wire_start:
                    ci.wire_start = None
                else:
                    ci.add_wire(ci._route(ci.wire_start, (gx, gy)))
                    ci.wire_start = None
        ci.frame(self.dt)
        # grid
        glPointSize(2.0)
        glColor4f(0.5, 0.7, 0.9, 0.16)
        glBegin(GL_POINTS)
        for gx in range(ci.cols + 1):
            for gy in range(ci.rows + 1):
                glVertex2f(ox + gx * G, oy + gy * G)
        glEnd()
        P = lambda p: (ox + p[0] * G, oy + p[1] * G)
        for wr in ci.wires:
            _line([P(p) for p in wr["pts"]], (0.35, 0.9, 0.5), 0.9, 2.5)
            for p in (wr["pts"][0], wr["pts"][-1]):
                self._circle(*P(p), 3.5, (0.35, 0.9, 0.5), 0.9)
        if ci.wire_start:
            sx, sy = P(ci.wire_start)
            self._circle(sx, sy, 6, (1, 1, 1), 0.9)
            _line([(sx, sy), (self.mx, self.my)], (1, 1, 1), 0.4, 1.5)
        for c in ci.comps:
            self._draw_comp(c, ox, oy, c["id"] == ci.sel)
        self._t('mouse: click dot then dot = wire | click switch/arduino = toggle | right-click cancels | '
                'voice: "add a 220 ohm resistor", "wire it up"', x + 18, y + hh - 24, (110, 150, 180))
        if not ci.comps:
            self._t('Say: "build an LED circuit"  |  "build a blink circuit"  |  "build an RC circuit"  |  '
                    '"add a battery"', x + 18, y + 40)

    def _draw_comp(self, c, ox, oy, selected):
        k = c["kind"]
        cx, cy = ox + c["gx"] * G, oy + c["gy"] * G
        col = (0.75, 0.9, 1.0)
        if selected:
            _line([(cx - G, cy - 17), (cx + G, cy - 17), (cx + G, cy + 17), (cx - G, cy + 17), (cx - G, cy - 17)],
                  (1, 1, 1), 0.5, 1.5)
        if k == "gnd":
            _line([(cx, cy), (cx, cy + 10)], col, 1.0, 2.0)
            for dy, hw in ((10, 12), (15, 8), (20, 4)):
                _line([(cx - hw, cy + dy), (cx + hw, cy + dy)], col, 1.0, 2.0)
            self._t("GND", cx - 12, cy + 24, (150, 190, 220))
            return
        half = {"battery": 5, "capacitor": 5}.get(k, 14)
        _line([(cx - G, cy), (cx - half, cy)], col, 1.0, 2.0)
        _line([(cx + half, cy), (cx + G, cy)], col, 1.0, 2.0)
        for tx in (cx - G, cx + G):
            self._circle(tx, cy, 3.5, (1, 1, 1), 0.85)
        a_glow = 0.0
        if k == "resistor":
            _line([(cx - 14, cy), (cx - 10, cy - 8), (cx - 4, cy + 8), (cx + 2, cy - 8), (cx + 8, cy + 8),
                   (cx + 14, cy)], col, 1.0, 2.0)
        elif k in SOURCES:
            if k == "battery":
                _line([(cx + 5, cy - 13), (cx + 5, cy + 13)], (1, 0.5, 0.4), 1.0, 3.0)
                _line([(cx - 5, cy - 7), (cx - 5, cy + 7)], (0.5, 0.7, 1.0), 1.0, 3.0)
            else:
                lit = c["v"] > 2.5
                _line([(cx - 14, cy - 10), (cx + 14, cy - 10), (cx + 14, cy + 10), (cx - 14, cy + 10),
                       (cx - 14, cy - 10)], (0.3, 1.0, 0.6) if lit else col, 1.0, 2.5)
            self._t("-", cx - G + 4, cy - 20, (150, 190, 255))
            self._t("+", cx + G - 12, cy - 20, (255, 160, 150))
        elif k == "led":
            rgb = LED_RGB.get(c["color"], (1, 0.2, 0.2))
            a_glow = min(1.0, c["i"] / 0.012) if c["on"] else 0.0
            if a_glow > 0:
                self._circle(cx, cy, 26, rgb, 0.10 + 0.3 * a_glow)
                self._circle(cx, cy, 12, rgb, 0.35 + 0.6 * a_glow)
            _line([(cx - 8, cy - 9), (cx - 8, cy + 9), (cx + 8, cy), (cx - 8, cy - 9)], rgb, 1.0, 2.5)
            _line([(cx + 8, cy - 9), (cx + 8, cy + 9)], rgb, 1.0, 3.0)
            if c["burnt"]:
                _line([(cx - 10, cy - 10), (cx + 10, cy + 10)], (1, 0.2, 0.2), 1.0, 3.0, GL_LINES)
        elif k == "switch":
            self._circle(cx - 14, cy, 3, col)
            self._circle(cx + 14, cy, 3, col)
            _line([(cx - 14, cy), (cx + 14, cy)] if c["closed"] else [(cx - 14, cy), (cx + 10, cy - 14)],
                  (0.4, 1.0, 0.5) if c["closed"] else (1.0, 0.5, 0.4), 1.0, 3.0)
        elif k == "capacitor":
            ch = min(1.0, abs(c["vc"]) / 5.0)
            _line([(cx - 4, cy - 12), (cx - 4, cy + 12)], (0.5 + 0.5 * ch, 0.8, 1.0), 1.0, 3.0)
            _line([(cx + 4, cy - 12), (cx + 4, cy + 12)], (0.5 + 0.5 * ch, 0.8, 1.0), 1.0, 3.0)
        elif k == "bulb":
            a_glow = min(1.0, abs(c["v"] * c["i"]) / 0.5)
            if a_glow > 0.02:
                self._circle(cx, cy, 24, (1.0, 0.9, 0.4), 0.10 + 0.4 * a_glow)
            glLineWidth(2.0)
            self._circle(cx, cy, 12, (1.0, 0.9, 0.5), 1.0, filled=False)
            _line([(cx - 8, cy - 8), (cx + 8, cy + 8)], (1.0, 0.9, 0.5), 1.0, 2.0, GL_LINES)
            _line([(cx - 8, cy + 8), (cx + 8, cy - 8)], (1.0, 0.9, 0.5), 1.0, 2.0, GL_LINES)
        self._t(f"{abs(c['v']):.2f}V {abs(c['i']) * 1000:.1f}mA" + (" BURNT" if c["burnt"] else ""),
                cx - 32, cy - 34, (255, 140, 120) if c["burnt"] else (170, 210, 235))
        self._t(self._comp_label(c), cx - 28, cy + 16, (150, 190, 220))


# ============================================================================
# VOICE ROUTER + HOLOGRAM HOOK
# ============================================================================

def install(hologram):
    """Attach Labs to the hologram and draw it on top of the dashboard (idempotent)."""
    if getattr(hologram, "labs", None) is not None:
        return hologram.labs
    labs = Labs(hologram)
    hologram.labs = labs
    orig = hologram._draw_dashboard

    def wrapped(theme_color):
        orig(theme_color)
        if labs.active:
            hologram._begin_ortho()
            try:
                labs.draw(theme_color)
            finally:
                hologram._end_ortho()

    hologram._draw_dashboard = wrapped
    return labs


_EL_RE = "|".join(EL_WORDS)
_CKIND = r"(battery|resistor|led|switch|capacitor|bulb|lamp|arduino|ground)"


def _route(labs, t):
    """Returns a reply string if handled (may be ''), or None if not for us."""
    ph, mol, ci = labs.phys, labs.mol, labs.circ

    if labs.active and re.search(r"\b(?:close|exit|hide|leave|quit)\s+(?:the\s+|my\s+)?(?:physics|molecule|circuit)\b", t):
        labs.close()
        return "Closing the sandbox."

    # ---- physics ----
    if re.search(r"\b(?:clear|reset)\s+(?:the\s+)?(?:physics|simulation)\b", t):
        ph.sims = []
        return "Physics cleared."
    if _physics_kind(t):
        labs.open("physics")
        return ph.create(t)
    if re.search(r"physics (?:sandbox|lab)", t):
        labs.open("physics")
        return "Physics sandbox ready. Try: create a projectile with 20 meters per second velocity."

    # ---- molecule builder ----
    mol_active = labs.active == "molecule"
    preset = next((n for n in MOLECULES if n in t), None) or \
        {"co2": "carbon dioxide", "h2o": "water", "ch4": "methane", "nh3": "ammonia", "c6h6": "benzene"}.get(
            next((k for k in ("co2", "h2o", "ch4", "nh3", "c6h6") if re.search(rf"\b{k}\b", t)), ""))
    if preset and re.search(r"\b(?:build|builder|load)\b", t) and ("molecule" in t or mol_active):
        labs.open("molecule")
        mol.load_preset(preset)
        return f"{preset.title()} loaded. Drag to rotate it, or edit it by voice."
    if re.search(r"molecule (?:builder|sandbox)|(?:build|make|create|start)\s+(?:a\s+|my\s+)?(?:new\s+)?molecule\b|new molecule", t):
        labs.open("molecule")
        if not mol_active or "new" in t or "build" in t:
            mol.clear()
        return "Molecule builder ready. Say add carbon, then add 4 hydrogens."
    if mol_active or "molecule" in t:
        if not mol_active and "molecule" in t and not re.search(r"\b(add|bond|select|remove|delete|fill)\b", t):
            pass
        elif re.search(r"\b(?:fill|saturate)\b|add (?:all |the )?(?:missing )?hydrogens\b(?! ?atoms? to)", t) and \
                re.search(r"fill|saturate|all|missing", t):
            labs.open("molecule") if not mol_active else None
            n = mol.fill_hydrogens()
            return f"Added {n} hydrogen{'s' if n != 1 else ''}. {mol.formula()}" + \
                   (f", {mol.name()}." if mol.name() else ".")
        m = re.search(rf"\b(?:add|attach|put)\s+(\d+|an?|one|two|three|four|five|six)?\s*({_EL_RE})s?\b", t)
        if m and mol_active:
            cnt = _int(m.group(1)) or 1
            el = EL_WORDS[m.group(2)]
            for _ in range(min(cnt, 12)):
                mol.add(el)
            over = mol.sel is not None and mol.sel < len(mol.atoms) and \
                mol._valence_used(mol.sel) > VALENCE.get(mol.atoms[mol.sel][0], 9)
            f = mol.formula()
            return (f"Added {cnt} {m.group(2)}. Formula {f}" + (f", {mol.name()}" if mol.name() else "") + "." +
                    (" That atom is over its usual valence." if over else ""))
        m = re.search(r"(?:(single|double|triple)\s+)?bond\s+(?:between\s+)?(?:atoms?\s+)?(\w+)\s+(?:and|to|with)\s+(?:atom\s+)?(\w+)", t)
        if m and mol_active:
            i, j = _int(m.group(2)), _int(m.group(3))
            n = len(mol.atoms)
            if i and j and i != j and 1 <= i <= n and 1 <= j <= n:
                order = {"double": 2, "triple": 3}.get(m.group(1), 1)
                mol.set_bond(i - 1, j - 1, order)
                return f"{['single', 'double', 'triple'][order - 1].title()} bond set between atom {i} and atom {j}."
            return "I couldn't find those atoms."
        m = re.search(r"select atom (\w+)", t)
        if m and mol_active:
            i = _int(m.group(1))
            if i and 1 <= i <= len(mol.atoms):
                mol.sel = i - 1
                return f"Atom {i} selected."
            return "There's no such atom."
        m = re.search(r"(?:remove|delete) atom (\w+)", t)
        if m and mol_active:
            i = _int(m.group(1))
            return f"Removed atom {i}." if i and mol.remove(i - 1) else "There's no such atom."
        if mol_active and re.search(r"(?:clear|reset|empty) (?:the |my )?molecule|start over", t):
            mol.clear()
            return "Molecule cleared."
        if mol_active and re.search(r"stop (?:the )?(?:spinning|rotating)|(?:stop|freeze) (?:the )?molecule", t):
            mol.spin = False
            return "Stopped spinning."
        if mol_active and re.search(r"(?:start|resume) (?:spinning|rotating)|(?:spin|rotate) (?:the )?molecule", t):
            mol.spin = True
            return "Spinning."
        if mol_active and re.search(r"molecule info|what molecule|what is this molecule|formula", t):
            if not mol.atoms:
                return "The builder is empty."
            return (f"Formula {mol.formula()}" + (f", that's {mol.name()}" if mol.name() else "") +
                    f". {len(mol.atoms)} atoms, mass {mol.mass():.1f} grams per mole.")

    # ---- circuit sandbox ----
    cir_active = labs.active == "circuit"
    if re.search(r"\bcircuit\b", t) and re.search(r"\b(?:build|create|make|load|show)\b", t) and \
            not re.search(r"circuit (?:sandbox|builder|lab)", t):
        name = "rc" if re.search(r"\brc\b|capacitor", t) else "blink" if re.search(r"blink|arduino", t) else \
            "led" if "led" in t else None
        if name:
            labs.open("circuit")
            ci.preset(name)
            return {"led": "LED circuit built: battery, resistor, LED. Try removing the resistor with undo to see it burn out.",
                    "blink": "Arduino blink circuit built: pin thirteen, resistor, LED.",
                    "rc": "RC circuit built. Say close the switch to charge the capacitor."}[name]
    if re.search(r"circuit (?:sandbox|builder|lab)|(?:open|start|new)\s+(?:a\s+)?circuit\b", t):
        labs.open("circuit")
        if "new" in t:
            ci.clear()
        return "Circuit sandbox ready. Add parts by voice, or build an LED circuit."
    if cir_active or "circuit" in t:
        m = re.search(rf"\b(?:add|place|put|insert|create)\s+(?:a\s+|an\s+|the\s+)?(?:\S+\s+){{0,4}}?{_CKIND}\b", t)
        if m:
            labs.open("circuit") if not cir_active else None
            kind = {"lamp": "bulb", "arduino": "ard", "ground": "gnd"}.get(m.group(1), m.group(1))
            kind = "capacitor" if kind == "capacitor" else kind
            val = None
            if kind == "battery":
                val = _num(r"(\d+(?:\.\d+)?)\s*(?:volts?|v\b)", t)
            elif kind in ("resistor", "bulb"):
                val = _num(r"(\d+(?:\.\d+)?)\s*(?:k|kilo)\s*(?:ohms?)?", t)
                val = val * 1000 if val else _num(r"(\d+(?:\.\d+)?)\s*(?:ohms?|ω)", t)
            elif kind == "capacitor":
                val = _num(r"(\d+(?:\.\d+)?)\s*(?:micro\s*farads?|uf|µf)", t)
            color = next((cn for cn in LED_RGB if cn in t), "red")
            ci.add(kind, val, color)
            return f"Added a {m.group(1)}. Say wire it up to connect everything in a loop."
        if re.search(r"wire (?:it |them )?(?:up|in series)|auto ?wire|connect (?:everything|them|all)|close the loop", t):
            return "Wired in a series loop." if ci.auto_wire() else "I need at least two parts to wire."
        m = re.search(r"(toggle|flip|press|close|open|turn on|turn off)\s+(?:the\s+)?switch", t)
        if m:
            verb = m.group(1)
            closed = None if verb in ("toggle", "flip", "press") else verb in ("close", "turn on")
            return "Switch toggled." if ci.toggle_switches(closed) else "There's no switch in the circuit."
        if re.search(r"measure|what'?s the current|circuit status|readings", t) and (cir_active or "circuit" in t):
            return ci.summary()
        if re.search(r"(?:pause|freeze) (?:the )?simulation", t):
            ci.paused = True
            return "Simulation paused."
        if re.search(r"(?:resume|unpause|continue) (?:the )?simulation", t):
            ci.paused = False
            return "Simulation running."
        if re.search(r"(?:clear|reset|empty) (?:the |my )?circuit", t):
            ci.clear()
            return "Circuit cleared."
    if cir_active:
        if re.search(r"\bundo\b", t):
            return "Undone." if ci.undo() else "Nothing to undo."
        if re.search(r"(?:delete|remove) (?:the )?selected|delete (?:it|this)", t):
            return "Deleted." if ci.delete_selected() else "Nothing is selected. Click a part first."
    return None


def handle_command(hologram, text, speak):
    """Single entry point for voice_assistant.py. Returns True if handled."""
    labs = install(hologram)
    with labs.lock:
        reply = _route(labs, text.lower().strip())
    if reply is None:
        return False
    if reply:
        speak(reply)
    return True
