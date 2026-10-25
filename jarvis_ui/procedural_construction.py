"""
Procedural construction: validated multi-part 3D models drawn with Aurora's existing wireframe primitives.

Contains the part schema and validator, a model registry with a small built-in library (robot, drone, car,
spaceship), staged or instant assembly drawing, and the hook into Hologram's shape renderer. Voice, AI generation and
gesture selection are not included yet.

Model API (all validation goes through build_model / Part.from_dict; nothing else validates):
  define_model(name, parts, stagger=None, replace=False)   validate + register a model from part dicts
  model_from_dict({"name":..., "parts":[...], "stagger":0.07})   validate a model dict without registering
  register_model(model, replace=False) / register_model_dict(spec, replace=False)
  get_model(name) / list_models() / unregister_model(name)
  load_construction(hologram, name, assemble=True)          show a model; assemble=False builds it instantly
Registering a different model under an existing name raises unless replace=True; built-ins can never be replaced.

Hook (same pattern as addons.install for the heart): Hologram._draw_shape is wrapped, so the existing shape layers
(chromatic ghosts, bloom, crisp outline), scan band, stripes, fog, materialize wave and the rotate/zoom/pan/roll
gestures all apply unchanged. Part colour is deliberately not part of the schema: the active layer sets it.
Model names are NOT added to hologram.SHAPES, so the generic "show <shape>" voice matcher cannot trigger them.

Threading: only draw_construction / draw_model issue OpenGL calls, and they run from Hologram.render().
load_construction and install only change Python state and are safe from other threads (like Hologram.load_shape).

Part fields (units match the other shape models, roughly +-1.5 across; y is up):
  name   unique, letters/digits/space/_/-, up to 32 characters
  prim   box | sphere | cylinder | pyramid
  pos    (x, y, z) centre, each within +-2.0
  size   full extents (x, y, z) in 0.02..2.5, or one number for a uniform size
  rot    optional (x, y, z) degrees, applied z then y then x, wrapped to -180..180
  delay  optional seconds after assembly starts, 0..10
"""
import itertools
import math
import numbers
import re
import threading
from dataclasses import dataclass, field

from OpenGL.GL import glCallList, glDeleteLists, glPopMatrix, glPushMatrix, glRotatef, glScalef, glTranslatef

PRIMITIVES = ("box", "sphere", "cylinder", "pyramid")
MAX_PARTS = 64
MAX_USER_MODELS = 32         # registered models beyond the built-ins
MAX_EXTENT = 2.4             # whole-model limit per axis from the origin (fog and zoom headroom)
MAX_POS, MIN_SIZE, MAX_SIZE, MAX_ROT, MAX_DELAY = 2.0, 0.02, 2.5, 720.0, 10.0
GROW_SECONDS = 0.35          # time for one part to grow to full size
ASSEMBLY_DELAY = 0.25        # lets the global materialize scale-in start before parts appear
MIN_GROWTH = 0.02            # smaller scales are skipped: a near-zero scale matrix draws nothing useful
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,31}")
_KEYS = {"name", "prim", "pos", "size", "rot", "delay"}


class PartError(ValueError):
    """A part or model description failed validation."""


def _num(value, label, lo, hi):
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise PartError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value) or not lo <= value <= hi:
        raise PartError(f"{label} must be between {lo:g} and {hi:g}")
    return value


def _vec(value, label, lo, hi):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PartError(f"{label} must be three numbers")
    return tuple(_num(v, f"{label}[{i}]", lo, hi) for i, v in enumerate(value))


@dataclass(frozen=True)
class Part:
    name: str
    prim: str
    pos: tuple
    size: tuple
    rot: tuple = (0.0, 0.0, 0.0)
    delay: float = 0.0

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise PartError("a part must be a dict")
        extra, missing = set(raw) - _KEYS, {"name", "prim", "pos", "size"} - set(raw)
        if extra or missing:
            raise PartError(f"part fields: unknown {sorted(extra)}, missing {sorted(missing)}")
        name = raw["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise PartError(f"bad part name {name!r}")
        if raw["prim"] not in PRIMITIVES:
            raise PartError(f"{name}: primitive must be one of {', '.join(PRIMITIVES)}")
        size = raw["size"]
        size = (_num(size, f"{name}.size", MIN_SIZE, MAX_SIZE),) * 3 if isinstance(size, numbers.Real) \
            else _vec(size, f"{name}.size", MIN_SIZE, MAX_SIZE)
        rot = tuple(((r + 180.0) % 360.0) - 180.0 for r in _vec(raw.get("rot", (0, 0, 0)), f"{name}.rot", -MAX_ROT, MAX_ROT))
        return cls(name, raw["prim"], _vec(raw["pos"], f"{name}.pos", -MAX_POS, MAX_POS), size, rot,
                   _num(raw.get("delay", 0.0), f"{name}.delay", 0.0, MAX_DELAY))


    def as_dict(self):
        return {"name": self.name, "prim": self.prim, "pos": self.pos, "size": self.size, "rot": self.rot, "delay": self.delay}


_UID = itertools.count(1)


@dataclass(frozen=True)
class Model:
    name: str
    parts: tuple
    uid: int = field(default_factory=lambda: next(_UID), compare=False, repr=False)   # display-list cache id

    @property
    def duration(self):
        """Seconds until every part has finished growing."""
        return max(p.delay for p in self.parts) + GROW_SECONDS


def _rotate(v, rot):
    """Same order as draw_model: x rotation first, then y, then z (right-handed, degrees)."""
    x, y, z = v
    rx, ry, rz = (math.radians(a) for a in rot)
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return x, y, z


def model_bounds(model):
    """((min x, y, z), (max x, y, z)). Conservative: uses each part's rotated bounding box."""
    pts = []
    for p in model.parts:
        for sx in (-0.5, 0.5):
            for sy in (-0.5, 0.5):
                for sz in (-0.5, 0.5):
                    r = _rotate((sx * p.size[0], sy * p.size[1], sz * p.size[2]), p.rot)
                    pts.append(tuple(r[i] + p.pos[i] for i in range(3)))
    return tuple(min(q[i] for q in pts) for i in range(3)), tuple(max(q[i] for q in pts) for i in range(3))


def build_model(name, raw_parts, stagger=None):
    """Validates a list of part dicts. Raises PartError; never returns a partly valid model.
    stagger (seconds) gives every part that has no explicit delay a delay of index * stagger (sequential assembly)."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise PartError(f"bad model name {name!r}")
    if not isinstance(raw_parts, (list, tuple)) or not 1 <= len(raw_parts) <= MAX_PARTS:
        raise PartError(f"a model needs 1 to {MAX_PARTS} parts")
    if stagger is not None:
        step = _num(stagger, "stagger", 0.0, 1.0)
        raw_parts = [dict(p, delay=i * step) if isinstance(p, dict) and "delay" not in p else p for i, p in enumerate(raw_parts)]
    parts = tuple(Part.from_dict(p) for p in raw_parts)
    if len({p.name.lower() for p in parts}) != len(parts):
        raise PartError("part names must be unique")
    model = Model(name.lower(), parts)
    reach = max(abs(v) for corner in model_bounds(model) for v in corner)
    if reach > MAX_EXTENT:
        raise PartError(f"model extends {reach:.2f} units from the origin (limit {MAX_EXTENT:g})")
    return model


# ---- demo model: bottom-up build order, centred near the origin (y -1.32 .. 1.30) ----------------------------------
def _p(name, prim, pos, size, delay, rot=(0, 0, 0)):
    return {"name": name, "prim": prim, "pos": pos, "size": size, "delay": delay, "rot": rot}


_ROBOT_PARTS = [
    _p("foot left", "box", (-0.2, -1.25, 0.08), (0.34, 0.14, 0.5), 0.00),
    _p("foot right", "box", (0.2, -1.25, 0.08), (0.34, 0.14, 0.5), 0.00),
    _p("leg left", "box", (-0.2, -0.85, 0.0), (0.28, 0.7, 0.3), 0.15),
    _p("leg right", "box", (0.2, -0.85, 0.0), (0.28, 0.7, 0.3), 0.15),
    _p("torso", "box", (0.0, -0.05, 0.0), (0.8, 0.9, 0.5), 0.30),
    _p("core", "sphere", (0.0, 0.05, 0.27), (0.26, 0.26, 0.1), 0.45),
    _p("shoulder left", "sphere", (-0.55, 0.3, 0.0), 0.28, 0.50),
    _p("shoulder right", "sphere", (0.55, 0.3, 0.0), 0.28, 0.50),
    _p("arm left", "cylinder", (-0.6, -0.15, 0.0), (0.2, 0.8, 0.2), 0.60, (0, 0, -6)),
    _p("arm right", "cylinder", (0.6, -0.15, 0.0), (0.2, 0.8, 0.2), 0.60, (0, 0, 6)),
    _p("hand left", "sphere", (-0.64, -0.65, 0.0), 0.2, 0.70),
    _p("hand right", "sphere", (0.64, -0.65, 0.0), 0.2, 0.70),
    _p("neck", "cylinder", (0.0, 0.47, 0.0), (0.2, 0.12, 0.2), 0.75),
    _p("head", "box", (0.0, 0.7, 0.0), (0.5, 0.42, 0.42), 0.80),
    _p("eye left", "sphere", (-0.12, 0.74, 0.22), (0.1, 0.1, 0.06), 0.95),
    _p("eye right", "sphere", (0.12, 0.74, 0.22), (0.1, 0.1, 0.06), 0.95),
    _p("antenna", "cylinder", (0.0, 1.05, 0.0), (0.04, 0.28, 0.04), 1.00),
    _p("antenna tip", "sphere", (0.0, 1.25, 0.0), 0.1, 1.10),
]


def _q(name, prim, pos, size, rot=(0, 0, 0)):
    """Part without a delay: build_model(..., stagger=) assigns delays in list order."""
    return {"name": name, "prim": prim, "pos": pos, "size": size, "rot": rot}


# Axis conventions for the library (the viewer sees the model rotated 25/35 degrees): y is up; robot faces +z;
# drone front is +z; car and spaceship noses point along +x. Cylinders are y-axis, so wheels use rot x 90 and
# spaceship hulls use rot z 90; a pyramid's apex is +y, so rot z -90 points it along +x and +90 along -x.
_DRONE_PARTS = [
    _q("body", "box", (0, 0, 0), (0.7, 0.22, 0.7)),
    _q("battery", "box", (0, 0.17, 0), (0.44, 0.12, 0.44)),
    _q("arm a", "box", (0, 0.03, 0), (2.4, 0.07, 0.12), (0, -45, 0)),
    _q("arm b", "box", (0, 0.03, 0), (2.4, 0.07, 0.12), (0, 45, 0)),
    _q("motor front left", "cylinder", (-0.85, 0.06, 0.85), (0.26, 0.16, 0.26)),
    _q("motor front right", "cylinder", (0.85, 0.06, 0.85), (0.26, 0.16, 0.26)),
    _q("motor rear left", "cylinder", (-0.85, 0.06, -0.85), (0.26, 0.16, 0.26)),
    _q("motor rear right", "cylinder", (0.85, 0.06, -0.85), (0.26, 0.16, 0.26)),
    _q("propeller front left", "cylinder", (-0.85, 0.17, 0.85), (0.85, 0.02, 0.85)),
    _q("propeller front right", "cylinder", (0.85, 0.17, 0.85), (0.85, 0.02, 0.85)),
    _q("propeller rear left", "cylinder", (-0.85, 0.17, -0.85), (0.85, 0.02, 0.85)),
    _q("propeller rear right", "cylinder", (0.85, 0.17, -0.85), (0.85, 0.02, 0.85)),
    _q("camera gimbal", "sphere", (0, -0.2, 0.34), 0.28),
    _q("skid left", "box", (0, -0.45, -0.3), (1.1, 0.05, 0.07)),
    _q("skid right", "box", (0, -0.45, 0.3), (1.1, 0.05, 0.07)),
    _q("strut front left", "box", (-0.4, -0.27, 0.3), (0.05, 0.32, 0.05)),
    _q("strut front right", "box", (0.4, -0.27, 0.3), (0.05, 0.32, 0.05)),
    _q("strut rear left", "box", (-0.4, -0.27, -0.3), (0.05, 0.32, 0.05)),
    _q("strut rear right", "box", (0.4, -0.27, -0.3), (0.05, 0.32, 0.05)),
]
_CAR_PARTS = [
    _q("chassis", "box", (0, -0.1, 0), (2.3, 0.35, 1.0)),
    _q("wheel front left", "cylinder", (0.72, -0.3, -0.52), (0.5, 0.2, 0.5), (90, 0, 0)),
    _q("wheel front right", "cylinder", (0.72, -0.3, 0.52), (0.5, 0.2, 0.5), (90, 0, 0)),
    _q("wheel rear left", "cylinder", (-0.72, -0.3, -0.52), (0.5, 0.2, 0.5), (90, 0, 0)),
    _q("wheel rear right", "cylinder", (-0.72, -0.3, 0.52), (0.5, 0.2, 0.5), (90, 0, 0)),
    _q("front bumper", "box", (1.18, -0.2, 0), (0.08, 0.12, 1.0)),
    _q("rear bumper", "box", (-1.18, -0.2, 0), (0.08, 0.12, 1.0)),
    _q("hood", "box", (0.75, 0.1, 0), (0.7, 0.05, 0.94)),
    _q("trunk", "box", (-0.85, 0.1, 0), (0.5, 0.05, 0.94)),
    _q("cabin", "box", (-0.1, 0.22, 0), (1.2, 0.32, 0.86)),
    _q("mirror left", "box", (0.35, 0.28, -0.5), (0.1, 0.08, 0.12)),
    _q("mirror right", "box", (0.35, 0.28, 0.5), (0.1, 0.08, 0.12)),
    _q("headlight left", "sphere", (1.15, -0.02, -0.3), (0.12, 0.12, 0.14)),
    _q("headlight right", "sphere", (1.15, -0.02, 0.3), (0.12, 0.12, 0.14)),
    _q("taillight left", "box", (-1.15, 0.0, -0.34), (0.04, 0.08, 0.2)),
    _q("taillight right", "box", (-1.15, 0.0, 0.34), (0.04, 0.08, 0.2)),
    _q("spoiler", "box", (-1.0, 0.3, 0), (0.25, 0.04, 0.9)),
    _q("spoiler support left", "box", (-1.0, 0.2, -0.3), (0.05, 0.18, 0.05)),
    _q("spoiler support right", "box", (-1.0, 0.2, 0.3), (0.05, 0.18, 0.05)),
]
_SPACESHIP_PARTS = [
    _q("fuselage", "cylinder", (-0.1, 0, 0), (0.5, 1.8, 0.5), (0, 0, 90)),
    _q("nose cone", "pyramid", (1.15, 0, 0), (0.5, 0.7, 0.5), (0, 0, -90)),
    _q("cockpit", "sphere", (0.55, 0.2, 0), (0.5, 0.28, 0.32)),
    _q("ventral keel", "box", (0, -0.22, 0), (1.0, 0.12, 0.2)),
    _q("wing left", "box", (-0.35, -0.02, -0.65), (0.8, 0.05, 0.9), (0, 20, 0)),
    _q("wing right", "box", (-0.35, -0.02, 0.65), (0.8, 0.05, 0.9), (0, -20, 0)),
    _q("wing cannon left", "cylinder", (-0.35, -0.02, -1.0), (0.07, 0.6, 0.07), (0, 0, 90)),
    _q("wing cannon right", "cylinder", (-0.35, -0.02, 1.0), (0.07, 0.6, 0.07), (0, 0, 90)),
    _q("tail fin", "box", (-0.85, 0.35, 0), (0.45, 0.5, 0.05)),
    _q("stabilizer left", "box", (-0.9, 0.0, -0.4), (0.35, 0.04, 0.45)),
    _q("stabilizer right", "box", (-0.9, 0.0, 0.4), (0.35, 0.04, 0.45)),
    _q("engine left", "cylinder", (-1.15, 0, -0.22), (0.28, 0.4, 0.28), (0, 0, 90)),
    _q("engine right", "cylinder", (-1.15, 0, 0.22), (0.28, 0.4, 0.28), (0, 0, 90)),
    _q("exhaust left", "pyramid", (-1.55, 0, -0.22), (0.2, 0.4, 0.2), (0, 0, 90)),
    _q("exhaust right", "pyramid", (-1.55, 0, 0.22), (0.2, 0.4, 0.2), (0, 0, 90)),
]

MODELS = {"robot": build_model("robot", _ROBOT_PARTS),
          "drone": build_model("drone", _DRONE_PARTS, stagger=0.07),
          "car": build_model("car", _CAR_PARTS, stagger=0.07),
          "spaceship": build_model("spaceship", _SPACESHIP_PARTS, stagger=0.07)}
BUILTIN_MODELS = frozenset(MODELS)
_LOCK = threading.Lock()


# ---- registry API (pure Python state: safe from voice/background threads, never touches OpenGL) -----------------------
def _key(name):
    return name.strip().lower() if isinstance(name, str) else None


def register_model(model, replace=False):
    """Adds a Model. Returns the registered (validated) model. Re-registering identical content is a no-op.
    A different model under an existing name raises PartError unless replace=True; built-ins are never replaced."""
    if not isinstance(model, Model):
        raise PartError("register_model needs a Model (use define_model for part dicts)")
    canonical = build_model(model.name, [p.as_dict() for p in model.parts])        # re-validates hand-built Models
    with _LOCK:
        existing = MODELS.get(canonical.name)
        if existing is not None:
            if existing == canonical:
                return existing
            if canonical.name in BUILTIN_MODELS:
                raise PartError(f"{canonical.name!r} is a built-in model and cannot be replaced")
            if not replace:
                raise PartError(f"model {canonical.name!r} already exists with different parts (pass replace=True)")
        elif len(MODELS) - len(BUILTIN_MODELS) >= MAX_USER_MODELS:
            raise PartError(f"at most {MAX_USER_MODELS} custom models can be registered")
        MODELS[canonical.name] = canonical
    return canonical


def define_model(name, raw_parts, stagger=None, replace=False):
    """Validates part dicts and registers the model."""
    return register_model(build_model(name, raw_parts, stagger), replace)


def model_from_dict(spec):
    """Validates {"name": str, "parts": [part dicts], "stagger": optional seconds} without registering it."""
    if not isinstance(spec, dict) or set(spec) - {"name", "parts", "stagger"} or not {"name", "parts"} <= set(spec):
        raise PartError('a model dict needs "name" and "parts" (optional "stagger") and nothing else')
    return build_model(spec["name"], spec["parts"], spec.get("stagger"))


def register_model_dict(spec, replace=False):
    return register_model(model_from_dict(spec), replace)


def get_model(name):
    model = MODELS.get(_key(name))
    if model is None:
        raise PartError(f"unknown model {name!r}; available: {', '.join(list_models())}")
    return model


def list_models():
    return sorted(MODELS)


def unregister_model(name):
    """Removes a custom model. Returns False if it doesn't exist; built-ins raise PartError."""
    key = _key(name)
    if key in BUILTIN_MODELS:
        raise PartError(f"{key!r} is a built-in model")
    with _LOCK:
        return MODELS.pop(key, None) is not None


# ---- drawing (render thread only) -------------------------------------------------------------------------------------
def _ease(x):
    x = min(1.0, max(0.0, x))
    return 1.0 - (1.0 - x) ** 3


def _primitives(h):
    """Unit-sized primitives from Hologram. Each spans -1..1 on every axis, so scaling by size/2 gives the full extent."""
    return {"box": lambda: h._draw_wireframe_cube(1.0),
            "sphere": lambda: h._draw_wireframe_sphere(1.0, lat_count=3, lon_count=4, segments=24),
            "cylinder": lambda: h._draw_wireframe_cylinder(1.0, 2.0, 16),
            "pyramid": lambda: h._draw_wireframe_pyramid(1.0)}


def draw_model(h, model, t=None):
    """Draws a model with the current colour. t = seconds since assembly started; None = fully built."""
    prims = _primitives(h)
    for p in model.parts:
        growth = 1.0 if t is None else _ease((t - p.delay) / GROW_SECONDS)
        if growth < MIN_GROWTH:
            continue
        glPushMatrix()
        try:
            glTranslatef(*p.pos)
            for angle, axis in ((p.rot[2], (0, 0, 1)), (p.rot[1], (0, 1, 0)), (p.rot[0], (1, 0, 0))):
                if angle:
                    glRotatef(angle, *axis)
            glScalef(*(0.5 * s * growth for s in p.size))
            prims[p.prim]()
        finally:
            glPopMatrix()


def _evict_stale_lists(h, name, keep):
    """Deletes display lists compiled for an older version of model `name` (render thread only)."""
    lists = getattr(h, "_gl_lists", None)
    if isinstance(lists, dict):
        for k in [k for k in lists if isinstance(k, tuple) and len(k) == 3 and k[0] == "construction" and k[1] == name and k != keep]:
            glDeleteLists(lists.pop(k), 1)


def draw_construction(h, name):
    """Called by the wrapped Hologram._draw_shape, up to 5 times per frame (layers), so it must be a pure function of
    h.elapsed. While assembling it draws directly; once complete (or when built instantly) it replays a cached display
    list keyed by the model's uid, so replacing a model compiles a new list and frees the old one."""
    model = MODELS.get(name)
    if model is None:
        return
    t = h.elapsed - getattr(h, "_materialize_start", 0.0) - ASSEMBLY_DELAY
    if getattr(h, "_construction_assemble", True) is not False and t < model.duration:
        draw_model(h, model, t)
        return
    cached = getattr(h, "_cached_list", None)
    if cached is None:
        draw_model(h, model)
        return
    key = ("construction", name, model.uid)
    fresh = key not in getattr(h, "_gl_lists", {key})
    list_id = cached(key, lambda: draw_model(h, model))
    if fresh:
        _evict_stale_lists(h, name, key)
    glCallList(list_id)


# ---- Hologram integration ---------------------------------------------------------------------------------------------
def load_construction(h, name="robot", assemble=True):
    """Shows a model through the normal shape mode (mirrors Hologram.load_shape). Returns False for unknown names.
    assemble=False skips the part-by-part build and shows the finished model (it still materializes in)."""
    name = _key(name)
    if name not in MODELS:
        return False
    h.hide_weather()
    h.mode = "shape"
    h.shape_name = name
    h._construction_assemble = bool(assemble)
    h.mode_label = f"{name.upper()} CONSTRUCTION"
    h.selected_index = None
    h._trigger_materialize()
    return True


def install(hologram):
    """Wraps hologram._draw_shape and adds hologram.load_construction(name, assemble=True). Idempotent."""
    if getattr(hologram, "_construction_installed", False) is True:
        return hologram
    original = hologram._draw_shape
    hologram._draw_shape = lambda name: draw_construction(hologram, name) if name in MODELS else original(name)
    hologram.load_construction = lambda name="robot", assemble=True: load_construction(hologram, name, assemble)
    hologram._construction_installed = True
    return hologram
