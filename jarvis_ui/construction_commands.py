"""
Natural-language control of holographic construction models (Stage 4).

Voice and typed input both arrive through VoiceAssistant._handle_local_command, which calls handle_command() below
(registered with the other feature handlers in voice_assistant.py). Nothing here competes with that router:
handle_command returns False for anything it does not recognise, so every other Aurora command behaves as before.

Layers
  parse(text, models)      pure, deterministic regex parsing -> intent dict or None. No hologram, no AI, no eval.
  ConstructionCommands     resolves references (it / that / the whole model / a part name) against the live scene and
                           calls the Stage 3 ConstructionEditor and Stage 2 registry. It owns no models and no transforms.
Everything is plain Python state behind the editor's own lock; no OpenGL call is made here, so commands can arrive from
the voice thread, the network hub or a worker without touching the render thread. A re-entrant lock serialises commands.

Scope rules: commands only act while a construction model is displayed. Other hologram modes (atoms, orbits, labs,
presentations...) are never touched; their phrases fall through. Ambiguity is answered with a question, not a guess.
"""
import difflib
import math
import re
import threading
import time

from jarvis_ui import procedural_construction as pc

NUM = r"\d+(?:\.\d+)?"
PENDING_TTL = 60.0                       # seconds a clarification question stays open
MOVE_STEP = {"small": 0.1, "normal": 0.25, "large": 0.6}
MAX_MOVE_AMOUNT, DEFAULT_ANGLE, MAX_ANGLE = 10.0, 45.0, 720.0
GROW = {"small": 1.1, "normal": 1.25, "large": 1.6}
SHRINK = {"small": 0.9, "normal": 0.8, "large": 0.6}
MIN_FACTOR, MAX_FACTOR = 0.1, 10.0

_LEAD = re.compile(r"^(?:(?:aurora|please|hey|ok|okay|now|then|and|can you|could you|would you|will you|i want you to|"
                   r"i would like you to|id like you to|go ahead and)\b[, ]*)+")
# phrases that belong to other Aurora features; construction never claims them
_FOREIGN = re.compile(r"\b(?:orbits?|panels?|desktop|volume|wifi|phone|files?|folders?|notes?|timer|screen|workspace|e-?mail|window|"
                      r"songs?|music|skills?|decks?|flashcards?|calendar|weather|presentation|document|spreadsheet|brightness|theme|"
                      r"molecules?|atoms?|circuit|pendulum|projectile|spring)\b|\.[a-z]{2,4}\b")
_MODEL_ALIAS = {"ship": "spaceship", "starship": "spaceship", "spacecraft": "spaceship", "rocket": "spaceship",
                "quadcopter": "drone", "quadrocopter": "drone", "uav": "drone", "automobile": "car", "vehicle": "car",
                "bot": "robot", "android": "robot"}
_PART_SYN = {"rotor": "propeller", "prop": "propeller", "blade": "propeller", "propellor": "propeller", "tyre": "wheel",
             "tire": "wheel", "back": "rear", "hull": "fuselage", "thruster": "engine", "gun": "cannon"}
_QUERY_STOP = {"the", "an", "its", "their", "his", "her", "my", "of", "this", "that", "those", "these", "part", "component",
               "piece", "one", "please", "on", "in"}
_CLARIFY_STOP = {"the", "one", "part", "piece", "component", "please"}
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": -1}
_MOVE_DIRS = {"up": (0, 1, 0), "upward": (0, 1, 0), "upwards": (0, 1, 0), "higher": (0, 1, 0),
              "down": (0, -1, 0), "downward": (0, -1, 0), "downwards": (0, -1, 0),
              "left": (-1, 0, 0), "right": (1, 0, 0),
              "closer": (0, 0, 1), "nearer": (0, 0, 1), "forward": (0, 0, 1), "forwards": (0, 0, 1),
              "back": (0, 0, -1), "backward": (0, 0, -1), "backwards": (0, 0, -1), "away": (0, 0, -1),
              "farther": (0, 0, -1), "further": (0, 0, -1)}
_DIR_REPLY = {(0, 1, 0): "upward", (0, -1, 0): "downward", (-1, 0, 0): "left", (1, 0, 0): "right",
              (0, 0, 1): "closer", (0, 0, -1): "away"}
_ROT_DIRS = {"left": ("y", 1), "counterclockwise": ("y", 1), "right": ("y", -1), "clockwise": ("y", -1),
             "up": ("x", 1), "forward": ("x", 1), "down": ("x", -1), "backward": ("x", -1), "back": ("x", -1)}
_WHOLE_WORDS = {"", "thing", "model", "object", "hologram", "construction", "everything", "structure", "all"}
_PART_REFS = {"selected component", "selected part", "selected piece", "selected one", "selected", "this part", "that part",
              "this component", "that component", "this piece", "that piece", "part", "component", "piece"}
_LARGE = r"a lot|much|way|far|significantly|massively|even"
_SMALL = r"slightly|a bit|a little|a tad|somewhat"


# ============================================================================ parsing (pure)
def normalize(text):
    t = (text or "").lower().replace("\u00b0", " degrees ").replace("'", "")
    t = re.sub(r"counter[- ]clockwise|anti[- ]clockwise", "counterclockwise", t)
    t = re.sub(r"[,;:!?\"]", " ", t).strip(" .")
    t = _LEAD.sub("", re.sub(r"\s+", " ", t))
    return re.sub(r"\s+(?:please|for me|now)$", "", t).strip()


def _squash(s):
    return " ".join(s.split())


def _quality(s):
    for q, pat in (("large", _LARGE), ("small", _SMALL)):
        m = re.search(rf"\b(?:{pat})\b", s)
        if m:
            return _squash(s[:m.start()] + " " + s[m.end():]), q
    return _squash(s), "normal"


def resolve_model(phrase, models):
    """'a drone' / 'the ship' / 'quadcopter model' -> registered model name or None."""
    p = _squash(re.sub(r"\b(?:the|a|an|my|one|new|3d|model|hologram|construction|whole|entire)\b", " ", phrase or ""))
    p = _MODEL_ALIAS.get(p, p)
    return p if p in models else None


def _tokens(s, stop=()):
    out = []
    for w in re.findall(r"[a-z0-9]+", s.lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        w = _PART_SYN.get(w, w)
        if w not in stop:
            out.append(w)
    return out


def resolve_part(phrase, names):
    """-> ("part", name) | ("ask", [names]) | ("none", None). Matches by word set, so 'left arm' finds 'arm left'."""
    q = _tokens(phrase, _QUERY_STOP)
    for qs in dict.fromkeys((frozenset(q), frozenset(q) - {"a"})):
        if not qs:
            continue
        hits = [n for n in names if qs <= set(_tokens(n))]
        if not hits:
            continue
        exact = [n for n in hits if set(_tokens(n)) == qs]
        if len(exact) == 1:
            return "part", exact[0]
        return ("part", hits[0]) if len(hits) == 1 else ("ask", hits)
    if not q:
        return "none", None
    key = " ".join(sorted(q))                                           # typo tolerance
    scored = sorted(((difflib.SequenceMatcher(None, key, " ".join(sorted(_tokens(n)))).ratio(), n) for n in names), reverse=True)
    if scored and scored[0][0] >= 0.8 and (len(scored) < 2 or scored[1][0] < scored[0][0] - 0.08):
        return "part", scored[0][1]
    return "none", None


def _num(m, *groups):
    for g in groups:
        if m.group(g):
            return float(m.group(g))
    return None


def _p_list(t, models):
    if re.fullmatch(r"(?:show|list|display|tell)(?: me)?(?: all)?(?: of)?(?: the)?(?: available| registered)? models|"
                    r"(?:what|which) models (?:are available|do you have|are there)", t):
        return {"kind": "list"}


def _p_undo(t, models):
    if re.fullmatch(r"undo(?: (?:that|it|this|(?:my )?(?:the )?last (?:change|edit|action|step)))?", t):
        return {"kind": "undo"}


def _p_reset(t, models):
    m = re.fullmatch(r"reset(?: (.+))?", t)
    if not m:
        return None
    rest = _squash(re.sub(r"\b(?:the|this|that|my|its|whole|entire|complete)\b", " ", m.group(1) or ""))
    if re.fullmatch(r"(?:model )?(?:transform|transformation|position|rotation|size|scale|orientation)", rest):
        return {"kind": "reset_transform"}
    if rest in ("", "it", "model", "thing", "object", "construction", "everything", "changes", "edits") or resolve_model(rest, models):
        return {"kind": "reset", "target": rest}


def _p_deselect(t, models):
    if re.fullmatch(r"(?:deselect|unselect)(?: (?:it|that|this|the part|the component|everything|all))?|clear (?:the )?selection", t):
        return {"kind": "deselect"}


def _p_step(t, models):
    m = re.fullmatch(r"(?:select )?(?:the )?(next|previous|prior) (?:part|component|piece)", t)
    if m:
        return {"kind": "step", "dir": 1 if m.group(1) == "next" else -1}


def _p_switch(t, models):
    m = re.fullmatch(r"(?:switch|change|go|swap)(?: over)? to (?:the |a |an )?(.+?) (?:hologram|display|view|model)", t)
    if m:
        model = resolve_model(m.group(1), models)
        return {"kind": "show", "model": model} if model else {"kind": "switch", "target": m.group(1)}


def _p_show(t, models):
    m = re.fullmatch(r"(?:show|display|construct|build|assemble|create|make|generate|load|spawn|bring up|pull up)(?: me)?"
                     r"(?: an?| the| my| one)? ?(.+?)", t)
    model = resolve_model(m.group(1), models) if m else None
    return {"kind": "show", "model": model} if model else None


def _p_select(t, models):
    m = re.fullmatch(r"(?:select|pick|choose|highlight|focus on|focus|target)\s+(.+)", t)
    return {"kind": "select", "target": m.group(1)} if m else None


def _factor(word, q, n):
    if n is not None:
        return n if word == "bigger" else (1.0 / n if n > 0 else None)
    return (GROW if word == "bigger" else SHRINK)[q]


def _p_scale(t, models):
    t2, q = _quality(t)
    m = re.fullmatch(r"make (.+?) twice as (?:big|large)", t2)
    if m:
        return {"kind": "scale", "target": m.group(1), "factor": 2.0, "absolute": False, "weak": False}
    m = re.fullmatch(r"make (.+?) half (?:the size|as (?:big|large))", t2)
    if m:
        return {"kind": "scale", "target": m.group(1), "factor": 0.5, "absolute": False, "weak": False}
    m = re.fullmatch(rf"make (.+?)(?: ({NUM}) times)? (bigger|larger|smaller|tinier)", t2)
    word = None
    if m:
        target, n, word = m.group(1), _num(m, 2), "bigger" if m.group(3) in ("bigger", "larger") else "smaller"
    else:
        m = re.fullmatch(r"(?:(enlarge|expand|grow|inflate|magnify|upsize|scale up)|(shrink|reduce|deflate|downsize|minimi[sz]e|"
                         r"squash|compress|scale down))\b ?(.*)", t2)
        if m:
            word, rest = ("bigger" if m.group(1) else "smaller"), m.group(3)
            n = re.search(rf"\bby ({NUM})\b|\b({NUM}) ?(?:times|x)\b", rest)
            target = _squash((rest[:n.start()] + " " + rest[n.end():]) if n else rest)
            n = _num(n, 1, 2) if n else None
    if word:
        return {"kind": "scale", "target": target, "factor": _factor(word, q, n), "absolute": False, "weak": False}
    m = re.fullmatch(r"(scale|resize) ?(.*)", t2)
    if not m:
        return None
    rest, absolute, factor = m.group(2), False, None
    n = re.search(rf"\b(by|to) ({NUM})(%| ?times| ?x)?(?!\w)|\b({NUM}) ?(?:times|x)\b|\b({NUM})%", rest)
    if n:
        val = float(next(g for g in (n.group(2), n.group(4), n.group(5)) if g))
        factor = val / 100 if (n.group(3) == "%" or n.group(5)) else val
        absolute = n.group(1) == "to" and n.group(3) != "%"
        rest = rest[:n.start()] + " " + rest[n.end():]
    d = re.search(r"\b(up|down)\b", rest)
    if d and factor is None:
        factor, rest = _factor("bigger" if d.group(1) == "up" else "smaller", q, None), rest[:d.start()] + " " + rest[d.end():]
    return {"kind": "scale", "target": _squash(rest), "factor": factor, "absolute": absolute, "weak": False}


def _p_rotate(t, models):
    m = re.fullmatch(r"(rotate|spin|tilt|twist|roll|turn|pivot|swivel)\b ?(.*)", t)
    if not m:
        return None
    verb, rest = m.groups()
    if verb == "turn" and re.search(r"\b(?:up|down|on|off|out|in|to|into|over|back)\b", rest):
        return None
    rest, _ = _quality(rest)
    angle = None
    for pat, val in ((r"quarter (?:turn|rotation)", 90), (r"half (?:turn|circle|rotation)", 180),
                     (r"full (?:turn|circle|rotation|revolution)", 360)):
        k = re.search(pat, rest)
        if k:
            angle, rest = float(val), rest[:k.start()] + " " + rest[k.end():]
    if angle is None:
        k = re.search(rf"(?:by )?(-?{NUM}) ?(?:degrees?|deg)\b|\bby (-?{NUM})\b|(?:^| )(-?{NUM})$", rest)
        if k:
            angle, rest = float(next(g for g in k.groups() if g)), rest[:k.start()] + " " + rest[k.end():]
    ax = re.search(r"\b(?:around|about|on|along|over)(?: the)? ([xyz])(?:[- ]axis)?\b|\b([xyz])[- ]axis\b", rest)
    axis = (ax.group(1) or ax.group(2)) if ax else None
    if ax:
        rest = rest[:ax.start()] + " " + rest[ax.end():]
    if angle is None and re.search(r"\baround\b", rest):
        angle, rest = 180.0, re.sub(r"\baround\b", " ", rest)
    words = _squash(rest).split()
    dirword = words.pop() if words and words[-1] in _ROT_DIRS else None
    sign = _ROT_DIRS[dirword][1] if dirword else 1
    if axis is None:
        if verb == "roll":
            axis = "z"
        elif verb == "tilt":
            axis = "z" if dirword in ("left", "right", "clockwise", "counterclockwise") else "x"
        else:
            axis = _ROT_DIRS[dirword][0] if dirword else "y"
    target = _squash(re.sub(r"\bby\b", " ", " ".join(words)))
    return {"kind": "rotate", "target": target, "axis": axis, "angle": sign * (DEFAULT_ANGLE if angle is None else angle),
            "weak": verb == "turn"}


def _p_move(t, models):
    m = re.fullmatch(r"(move|shift|push|nudge|slide|lift|raise|lower|pull|bring|drag)\b ?(.*)", t)
    if not m:
        return None
    verb, rest = m.groups()
    rest, q = _quality(re.sub(r"\baway from me\b", "away", re.sub(r"\b(?:toward|towards|to) me\b", "closer", m.group(2))))
    rest = re.sub(r"\bto the\b", " ", rest)
    if re.search(r"\b(?:in|into|from|onto|on|at|with|to)\b", rest):
        return None
    amount = None
    k = re.search(rf"(?:by )?({NUM}) (?:units?|steps?|meters?|metres?)\b|\bby ({NUM})\b", rest)
    if k:
        amount, rest = float(k.group(1) or k.group(2)), rest[:k.start()] + " " + rest[k.end():]
    words = _squash(rest).split()
    dirword = words.pop() if words and words[-1] in _MOVE_DIRS else {"lift": "up", "raise": "up", "lower": "down"}.get(verb)
    return {"kind": "move", "target": _squash(re.sub(r"\bby\b", " ", " ".join(words))), "dir": _MOVE_DIRS.get(dirword),
            "amount": amount, "quality": q, "weak": True}


_PARSERS = (_p_list, _p_undo, _p_reset, _p_deselect, _p_step, _p_switch, _p_show, _p_select, _p_scale, _p_rotate, _p_move)


def parse(text, models=()):
    """Spoken or typed text -> intent dict, or None when the phrase is not a construction command."""
    t = normalize(text)
    if not t or _FOREIGN.search(t):
        return None
    for fn in _PARSERS:
        intent = fn(t, models)
        if intent:
            return intent
    return None


# ============================================================================ execution
def _label(name):
    return name[:1].upper() + name[1:]


def _or_join(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + (", or " if len(items) > 2 else " or ") + items[-1]


def _fail_text(err):
    err = err or "that didn't work"
    if err == "no change":
        return "Nothing changed. It's at its limit."
    return "That would make the model too large." if "extends" in err else err[:1].upper() + err[1:] + "."


class ConstructionCommands:
    def __init__(self, hologram, voice=None):
        self.h, self.v = hologram, voice
        self._lock = threading.RLock()
        self.pending = None                       # open clarification: {"cands", "followup", "model", "expires"}

    @property
    def editor(self):
        ed = getattr(self.h, "construction", None)
        return ed if isinstance(ed, pc.ConstructionEditor) else None

    def displayed(self):
        """Name of the construction model on screen, else None (other hologram modes are never ours)."""
        name = getattr(self.h, "shape_name", None)
        ok = self.editor is not None and getattr(self.h, "mode", None) == "shape" and isinstance(name, str) and name in pc.MODELS
        return name if ok else None

    def route(self, text):
        """Reply string ('' = already spoken) or None when the phrase is not for construction. Never raises."""
        try:
            with self._lock:
                name, models = self.displayed(), set(pc.list_models())
                if self.pending:
                    if time.time() > self.pending["expires"] or name != self.pending["model"]:
                        self.pending = None
                    elif parse(text, models) is None:
                        return self._clarify(normalize(text), name)
                intent = parse(text, models)
                return self._execute(intent, name) if intent else None
        except Exception as e:                    # a bad command must never break the voice loop
            return f"Sorry, that construction command failed: {type(e).__name__}."

    # ---- execution ---------------------------------------------------------------------------
    def _execute(self, it, name):
        k, ed = it["kind"], self.editor
        if k == "list":
            return "Available models: " + _or_join(pc.list_models()) + "."
        if k == "show":
            return self._show(it["model"])
        if k == "switch":
            return self._switch(it["target"])
        if k == "select" and name is None:
            m = resolve_model(it["target"], set(pc.list_models()))
            return f"The {m} isn't displayed. Say show me a {m}." if m else None
        if name is None or ed is None:
            return None
        if k == "undo":
            return "Undone." if ed.undo() else "Nothing to undo."
        if k == "reset":
            other = resolve_model(it["target"], set(pc.list_models()))
            if other and other != name:
                return f"The {other} isn't displayed."
            return "Model reset." if ed.reset() else "Already original."
        if k == "reset_transform":
            return "Transform reset." if ed.reset_transform() else "Already at the default transform."
        if k == "deselect":
            ed.deselect_part()
            return "Selection cleared."
        if k == "step":
            part = ed.select_next_part() if it["dir"] > 0 else ed.select_previous_part()
            return f"{_label(part)} selected." if part else "Selection cleared."
        if k == "select":
            return self._select(it["target"], name)
        return self._transform(it, name)

    def _show(self, model):
        load = getattr(self.h, "load_construction", None)
        if load is None:
            return "Construction models aren't available."
        self.pending = None
        return f"{_label(model)} constructed." if load(model) else "I couldn't load that model."

    def _switch(self, target):
        model = resolve_model(target, set(pc.list_models()))
        if model:
            return self._show(model)
        if self.v is not None and self.v._handle_local_command(f"show me the {target}"):
            return ""                              # the existing display command already answered
        return f"I don't have a hologram called {target}."

    def _resolve_target(self, phrase, name):
        """-> (kind, value, implicit): kind is model | part | ask | error | none."""
        ed, models = self.editor, set(pc.list_models())
        p = _squash(re.sub(r"\b(?:the|a|an|its|their|his|her|my|of)\b", " ", phrase))
        sel = ed.selected_part
        if p in ("", "it", "that", "this", "them", "one"):
            return ("part", sel, True) if sel else ("model", None, True)
        if p in _PART_REFS:
            return ("part", sel, True) if sel else ("error", "No part is selected.", False)
        w = _squash(" ".join(x for x in p.split() if x not in ("whole", "entire", "complete", "full", "this", "that", "these", "those")))
        if w in _WHOLE_WORDS:
            return "model", None, False
        other = resolve_model(w, models)
        if other:
            return ("model", None, False) if other == name else ("error", f"The {other} isn't displayed.", False)
        kind, val = resolve_part(p, ed.part_names())
        return {"part": ("part", val, False), "ask": ("ask", val, False)}.get(kind, ("none", None, False))

    def _select(self, target, name):
        kind, val, _ = self._resolve_target(target, name)
        if kind == "model":
            self.editor.deselect_part()
            return f"{_label(name)} selected."
        if kind == "part":
            return f"{_label(val)} selected." if self.editor.select_part(val) else _fail_text(self.editor.last_error)
        if kind == "ask":
            return self._ask(val, None, name)
        return val if kind == "error" else "I couldn't identify that component."

    def _ask(self, cands, followup, name):
        self.pending = {"cands": cands[:8], "followup": followup, "model": name, "expires": time.time() + PENDING_TTL}
        return "Which one: " + _or_join([_label(c) for c in cands[:8]]) + "?"

    def _clarify(self, t, name):
        p = self.pending
        if re.fullmatch(r"cancel|never ?mind|none|no|stop", t):
            self.pending = None
            return "Okay."
        words = [w for w in t.split() if w != "the"]
        picked = None
        if words and words[0] in _ORDINALS and len(p["cands"]) > 1:
            picked = [p["cands"][_ORDINALS[words[0]]]] if abs(_ORDINALS[words[0]]) <= len(p["cands"]) else []
        else:
            q = set(_tokens(" ".join(words), _CLARIFY_STOP))
            picked = [c for c in p["cands"] if q and q <= set(_tokens(c))]
        if not picked:
            if len(words) > 4:
                self.pending = None
                return None
            return "I couldn't match that. Which one: " + _or_join([_label(c) for c in p["cands"]]) + "?"
        if len(picked) > 1:
            p["cands"], p["expires"] = picked, time.time() + PENDING_TTL
            return "Which one: " + _or_join([_label(c) for c in picked]) + "?"
        self.pending = None
        if p["followup"] is None:
            return f"{_label(picked[0])} selected." if self.editor.select_part(picked[0]) else _fail_text(self.editor.last_error)
        return self._transform(dict(p["followup"], target=picked[0]), name)

    def _transform(self, it, name):
        ed = self.editor
        kind, val, implicit = self._resolve_target(it["target"], name)
        if kind == "none":
            return None if it["weak"] else "I couldn't identify that component."
        if kind == "error":
            return val
        if kind == "ask":
            return self._ask(val, it, name)
        part = val if kind == "part" else None
        label = ("selected component" if implicit else f"the {part}") if part else "the model"
        k, ok, text = it["kind"], False, ""
        if k == "move":
            if it["dir"] is None:
                return "Which direction?"
            amt = it["amount"] if it["amount"] is not None else MOVE_STEP[it["quality"]]
            if not 0 < amt <= MAX_MOVE_AMOUNT:
                return "That distance is out of range."
            vec = tuple(c * amt for c in it["dir"])
            text = f"Moving {label} {_DIR_REPLY[it['dir']]}."
            ok = self._with_part(part, lambda: ed.move_part(*vec)) if part else ed.move_model(*vec)
        elif k == "rotate":
            ang = it["angle"]
            if not math.isfinite(ang) or ang == 0 or abs(ang) > MAX_ANGLE:
                return f"Angles must be between 1 and {MAX_ANGLE:g} degrees."
            vec = tuple(ang if a == it["axis"] else 0.0 for a in "xyz")
            text = f"Rotating {label} {abs(ang):g} degrees."
            ok = self._with_part(part, lambda: ed.rotate_part(*vec)) if part else ed.rotate_model(*vec)
        else:
            f = it["factor"]
            if f is None:
                return "By how much?"
            if not math.isfinite(f) or not MIN_FACTOR <= f <= MAX_FACTOR:
                return f"Scale must be between {MIN_FACTOR:g} and {MAX_FACTOR:g}."
            text = f"Making {label} {'bigger' if f > 1 else 'smaller'}."
            ok = self._with_part(part, lambda: ed.resize_part(f)) if part else ed.scale_model(f, absolute=it["absolute"])
        return text if ok else _fail_text(ed.last_error)

    def _with_part(self, part, action):
        """Explicitly named parts become the selection (so 'it' refers to them next); implicit ones already are."""
        if self.editor.selected_part != part and not self.editor.select_part(part):
            return False
        return action()


def handle_command(voice, text):
    """Voice/typed entry point for voice_assistant.py. Returns True if handled."""
    h = voice.hologram
    cmds = getattr(voice, "_construction_commands", None)
    if cmds is None or cmds.h is not h:
        pc.install(h)                              # idempotent; main.py has normally done this already
        cmds = voice._construction_commands = ConstructionCommands(h, voice)
    reply = cmds.route(text)
    if reply is None:
        return False
    if reply:
        voice._speak(reply)
    return True
