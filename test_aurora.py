"""
Aurora self-test. Tells you which features work.

    python test_aurora.py              logic + routing tests (no camera/mic/GPU/API needed)
    python test_aurora.py --hardware   also: camera, microphone, ADB, volume, hand tracker
    python test_aurora.py --gui        also: opens the hologram window and renders every display mode
    python test_aurora.py --live       also: real Groq API, tool calling, neural TTS, weather (needs internet + api_key.txt)
    python test_aurora.py --all        everything

Statuses: PASS = works | FAIL = broken (reason shown) | SKIP = not testable here (missing optional part)
Tests use temp folders; your notes, vault, projects and sandbox are never touched.
"""
import importlib
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
ARGS = set(sys.argv[1:])
ALL = "--all" in ARGS
HARDWARE, GUI, LIVE = ALL or "--hardware" in ARGS, ALL or "--gui" in ARGS, ALL or "--live" in ARGS

RESULTS = []


class Skip(Exception):
    def __init__(self, msg="", status="SKIP"):
        super().__init__(msg)
        self.status = status


def skip(msg, status="SKIP"):
    raise Skip(msg, status)


def requires(kind, msg):
    """kind: API, PERMISSION or HARDWARE"""
    raise Skip(msg, f"REQUIRES {kind}")


def check(group, name, fn):
    try:
        detail = fn()
        RESULTS.append((group, name, "PASS", detail if isinstance(detail, str) else ""))
    except Skip as e:
        RESULTS.append((group, name, e.status, str(e)))
    except AssertionError as e:
        RESULTS.append((group, name, "FAIL", f"assertion: {e}" if str(e) else "assertion failed"))
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)[-1]
        RESULTS.append((group, name, "FAIL", f"{type(e).__name__}: {str(e)[:110]} ({os.path.basename(tb.filename)}:{tb.lineno})"))


def mod(name):
    try:
        return importlib.import_module(f"jarvis_ui.{name}")
    except Exception as e:
        raise Skip(f"module {name} failed to import: {type(e).__name__}: {str(e)[:80]}")


def eq(a, b):
    assert a == b, f"expected {b!r}, got {a!r}"


# ============================================================================ dependencies
REQUIRED = ["cv2", "mediapipe", "OpenGL", "pygame", "numpy", "groq", "speech_recognition", "pyaudio", "edge_tts",
            "pyttsx3", "psutil", "PIL", "serial", "pypdf"]
WINDOWS_ONLY = ["pycaw", "comtypes"]
OPTIONAL = ["pocketsphinx"]


def dep(name, optional=False):
    def run():
        try:
            importlib.import_module(name)
        except ImportError as e:
            if optional or (name in WINDOWS_ONLY and os.name != "nt"):
                skip(f"not installed ({'optional' if optional else 'Windows only'})")
            raise AssertionError(f"not installed: pip install {name} ({e})")
    return run


for d in REQUIRED + WINDOWS_ONLY + OPTIONAL:
    check("Dependencies", d, dep(d, d in OPTIONAL))

# ============================================================================ modules import
for m in ["hologram", "hand_tracker", "face_id", "system_control", "telemetry", "brain", "voice_assistant",
          "addons", "aurora_plus", "sandbox_labs", "dashboard_modes", "system_monitor"]:
    check("Modules import", f"jarvis_ui.{m}", (lambda m=m: importlib.import_module(f"jarvis_ui.{m}") and None))
check("Modules import", "main.py", lambda: importlib.import_module("main") and None)


# ============================================================================ brain
def t_brain_search():
    b = mod("brain")
    assert b.needs_search("what's the latest news") and b.needs_search("who is the ceo of nvidia") and b.needs_search("iphone vs pixel")
    assert not b.needs_search("show me a carbon atom")


def t_brain_misc():
    b = mod("brain")
    eq(b.strip_code_fences("```python\nprint(1)\n```"), "print(1)\n")
    assert b.fuzzy_hit("oughrora set timer", ["aurora"]) or b.fuzzy_hit("aurera hi", ["aurora"])
    names = {t["function"]["name"] for t in b.TOOLS}
    for need in ("show_display", "get_weather", "set_timer", "pc_control", "phone_control", "edit_atom", "take_note",
                 "read_notes", "get_time", "write_code", "search_files"):
        assert need in names, f"tool {need} missing"


def t_brain_memory():
    b = mod("brain")
    m = b.RollingMemory(None)
    for i in range(20):
        m.add("user", f"hi {i}")
    m.maybe_compress()                      # no client -> must not crash
    eq(len(m.get_messages()), 20)
    m.clear_but_last()
    eq(len(m.get_messages()), 1)


check("AI brain", "needs_search heuristic", t_brain_search)
check("AI brain", "code-fence stripping, fuzzy match, tool schema", t_brain_misc)
check("AI brain", "rolling conversation memory", t_brain_memory)


# ============================================================================ voice helpers
def t_wake():
    v = mod("voice_assistant")
    eq(v.find_wake_word("aurora what time is it"), (True, "what time is it"))
    assert v.find_wake_word("arora hello")[0], "fuzzy wake word"
    assert not v.find_wake_word("just talking")[0]


def t_math():
    v = mod("voice_assistant")
    eq(v.try_calculate("47 times 12")[0], 564)
    eq(v.try_calculate("15 percent of 200")[0], 30)
    assert v.try_calculate("hello there") is None


def t_chunker_clean():
    v = mod("voice_assistant")
    c = v.Chunker()
    out = c.feed("Hello there my friend, how are you doing today? I am fine. ") + c.flush()
    assert len(out) >= 1 and "".join(out).replace(" ", "").startswith("Hellothere")
    eq(v.clean_for_speech("**bold** `code` # head"), "bold code head")


def t_speech_engine_queue():
    v = mod("voice_assistant")
    e = v.SpeechEngine(lambda m: None)
    e.use_edge = lambda: False
    with mock.patch.object(e, "_speak_offline", lambda text: time.sleep(0.05)):
        e.say("one two three")
        e.say("four five six")
        e.wait(5)
    assert not e.busy, "speech queue never drained"
    e.say("x y z")
    e.stop()
    time.sleep(0.2)
    assert not e.busy, "stop() left speech pending"


check("Voice pipeline", "wake word (fuzzy)", t_wake)
check("Voice pipeline", "spoken math + percent", t_math)
check("Voice pipeline", "sentence chunker + markdown cleaner", t_chunker_clean)
check("Voice pipeline", "speech queue drains and stop() cancels", t_speech_engine_queue)


# ============================================================================ voice command routing (stubbed hologram)
def make_voice():
    va = mod("voice_assistant")
    tel = mod("telemetry")
    H = mock.MagicMock(name="Hologram")
    H.load_atom.return_value = ("C", "carbon")
    H.load_star_system.return_value = ("Trappist-1", 7, False)
    H.next_element.return_value = ("He", "helium", 2)
    H.select_orbit.return_value = True
    H.protons, H.neutrons, H.electron_count, H.mode_label = 7, 7, 7, "NITROGEN"
    v = object.__new__(va.VoiceAssistant)
    said = []
    v.hologram, v.face_id, v.client, v.brain, v.plus, v.drawer = H, mock.MagicMock(), None, None, None, None
    v._capture = v._capture_tid = None
    v._turn_display = False
    v.said = said
    v.speech = mock.MagicMock()
    v.speech.say.side_effect = lambda text: said.append(va.clean_for_speech(text))
    v._log = v._on_log = lambda t: None
    v.active_timers, v.last_reply, v.latest_frame, v.last_weather_error = [], "", None, None
    v.telemetry, v.game_session, v.experiment = tel.TelemetryReader(), tel.GameSessionReader(), tel.ExperimentRecorder()
    v.active_reader, v.pending_enrollment_name = v.telemetry, None
    v._cancel = threading.Event()
    v._state = "idle"
    return v, H, said


def route(phrase, expect_said=None, expect=None, patches=(), setup=None):
    def run():
        v, H, said = make_voice()
        if setup:
            setup(v, H)
        stack = [p for p in patches]
        for p in stack:
            p.start()
        try:
            handled = v._handle_local_command(phrase)
        finally:
            for p in stack:
                p.stop()
        assert handled, f"'{phrase}' was not handled locally (would go to the AI)"
        if expect_said:
            assert any(expect_said.lower() in s.lower() for s in said), f"reply {said} lacks '{expect_said}'"
        if expect:
            expect(v, H)
    return run


sc_mod = lambda: mod("system_control")
tmpdir = tempfile.mkdtemp(prefix="aurora_test_")
ph = lambda target, **kw: mock.patch.object(sc_mod(), target, **kw)

ROUTES = [
    ("Time", "what time is it", "It's", None, ()),
    ("Math", "what's 47 times 12", "564", None, ()),
    ("Timers", "set a timer for 5 minutes", "Timer set", lambda v, H: eq(len(v.active_timers), 1), ()),
    ("Atoms", "show me a carbon atom", "carbon", lambda v, H: H.load_atom.assert_called_with("carbon"), ()),
    ("Atoms", "add 2 protons", "protons", lambda v, H: H.add_protons.assert_called_with(2), ()),
    ("Atoms", "next element", "helium", None, ()),
    ("Displays", "show me the solar system", "solar system", lambda v, H: H.load_solar_system.assert_called(), ()),
    ("Displays", "show me a cube", "cube", lambda v, H: H.load_shape.assert_called_with("cube"), ()),
    ("Displays", "show the eiffel tower", "eiffel", lambda v, H: H.load_shape.assert_called_with("eiffel tower"), ()),
    ("Displays", "show a water molecule", "water", lambda v, H: H.load_molecule.assert_called_with("water"), ()),
    ("Displays", "show orion", "Orion", lambda v, H: H.load_constellation.assert_called_with("orion"), ()),
    ("Displays", "show me trappist-1", "Trappist", lambda v, H: H.load_star_system.assert_called(), ()),
    ("Displays", "show a pendulum simulation", "pendulum", lambda v, H: H.load_physics_sim.assert_called_with("pendulum"), ()),
    ("Displays", "show my systems", "systems", lambda v, H: H.load_network.assert_called(), ()),
    ("Displays", "show the human heart", "heart", lambda v, H: H.load_shape.assert_called_with("heart"), ()),
    ("Displays", "reset the display", "Resetting", lambda v, H: H.load_demo.assert_called(), ()),
    ("Graphs", "plot x squared", "Plotting", None, ()),
    ("Graphs", "graph sine of x", "Plotting", None, ()),
    ("Notes", "take a note: buy milk", "Noted", None, (ph("add_note"),)),
    ("Notes", "read my notes", "notes", None, (ph("read_notes", return_value=["buy milk"]),)),
    ("Weather", "weather in tokyo", "Tokyo", lambda v, H: H.show_weather.assert_called(), ()),
    ("PC control", "open notepad", "Opening notepad", None, (ph("launch_app", return_value=True),)),
    ("PC control", "volume to 40", "40", None, (ph("set_volume_percent", return_value=True),)),
    ("PC control", "volume up", "Volume at", None, (ph("adjust_volume_percent", return_value=60),)),
    ("PC control", "play music", "Done", None, (ph("media_play_pause"),)),
    ("PC control", "take a screenshot", "Screenshot", None, (ph("take_screenshot", return_value=True),)),
    ("PC control", "system status", "CPU", None, (ph("get_system_status", return_value="CPU at 5 percent."),)),
    ("Phone", "call mom", "Calling mom", None,
     (ph("load_contacts", return_value={"mom": "123"}), ph("is_device_connected", return_value=(True, "")),
      ph("call_number", return_value=(True, "")))),
    ("Phone", "open instagram on my phone", "Opening instagram", None,
     (ph("is_device_connected", return_value=(True, "")), ph("open_app", return_value=True))),
    ("Phone", "call mom on whatsapp", "WhatsApp call", None,
     (ph("is_device_connected", return_value=(True, "")), ph("whatsapp_call", return_value=(True, "Mom")))),
    ("Phone", "turn off wifi on my phone", "wifi turned off", None,
     (ph("is_device_connected", return_value=(True, "")), ph("wifi_set", return_value=True))),
    ("Code", "create arduino sketch blinky", "blinky", None,
     (ph("create_arduino_sketch", return_value="x.ino"), ph("open_in_editor", return_value=(True, "")))),
    ("Code", "new python script called demo", "demo", None,
     (ph("create_code_file", return_value="demo.py"), ph("open_in_editor", return_value=(True, "")))),
    ("Vault", "add folder /tmp to my vault", "Added", None, (ph("vault_add_folder", return_value=(True, "/tmp")),)),
    ("Vault", "rebuild my vault", "Indexed", None, (ph("vault_build", return_value=(3, 9)),)),
    ("Face", "who do you know", "enrolled", None, ()),
    ("Face", "remember my face as sam", "Sam", lambda v, H: eq(v.pending_enrollment_name, "Sam"), ()),
    ("IoT", "show my mars station telemetry", "mars station", lambda v, H: H.show_telemetry.assert_called_with("mars station"), ()),
    ("Game companion", "game companion", "recording", lambda v, H: H.show_telemetry.assert_called(), ()),
    ("Experiment", "start experiment called volcano", "volcano", None, ()),
    ("Control", "repeat that", "haven't said", None, ()),
    ("Control", "stop", None, lambda v, H: v.speech.stop.assert_called(), ()),
]


def setup_graph(v, H):
    mod("hologram")
    H._eval_graph_expr.side_effect = lambda e, x: mod("hologram").Hologram._eval_graph_expr(H, e, x)


def setup_weather(v, H):
    v._fetch_weather_data = lambda loc: {"location": "Tokyo, Japan", "temp_c": 21.4, "condition": "sunny", "description": "clear sky",
                                          "humidity": 40, "wind_kph": 5, "updated_at": "12:00"}


def setup_experiment(v, H):
    tel = mod("telemetry")
    v.experiment = tel.ExperimentRecorder()


for group, phrase, said_frag, expect, patches in ROUTES:
    setup = setup_graph if group == "Graphs" else setup_weather if group == "Weather" else None
    if group == "Experiment":
        def exp_run(phrase=phrase, said_frag=said_frag):
            def run():
                tel = mod("telemetry")
                with mock.patch.object(tel, "EXPERIMENTS_DIR", tmpdir):
                    route(phrase, said_frag)()
            return run
        check(f"Voice commands: {group}", f'"{phrase}"', exp_run())
    else:
        check(f"Voice commands: {group}", f'"{phrase}"', route(phrase, said_frag, expect, patches, setup))


def t_falls_through():
    v, H, said = make_voice()
    assert not v._handle_local_command("tell me a joke about cats"), "general chat must go to the AI"


check("Voice commands: Control", "general questions fall through to the AI", t_falls_through)


def t_llm_tools():
    b = mod("brain")
    v, H, said = make_voice()
    assert "It's" in v._exec_tool("get_time", {})
    v._exec_tool("show_display", {"kind": "atom", "name": "carbon"})
    H.load_atom.assert_called_with("carbon")
    assert "Timer set" in v._exec_tool("set_timer", {"seconds": 30})
    assert v._exec_tool("show_display", {"kind": "nonsense"}) == "Unsupported request."
    assert {t["function"]["name"] for t in b.TOOLS} >= {"pc_control", "phone_control"}


check("AI tool calling", "LLM tools drive real Aurora actions", t_llm_tools)


# ============================================================================ system layer
def t_notes():
    sc = sc_mod()
    with mock.patch.object(sc, "NOTES_FILE", os.path.join(tmpdir, "notes.txt")):
        sc.add_note("alpha")
        sc.add_note("beta")
        eq(sc.read_notes(5), ["alpha", "beta"])
        sc.clear_notes()
        eq(sc.read_notes(5), [])


def t_code_files():
    sc = sc_mod()
    with mock.patch.object(sc, "PROJECTS_ROOT", os.path.join(tmpdir, "projects")), \
            mock.patch.object(sc, "ARDUINO_ROOT", os.path.join(tmpdir, "ino")), mock.patch.object(sc, "BASE", tmpdir):
        p = sc.create_code_file("my demo", "python")
        assert p.endswith("my_demo.py") and os.path.exists(p)
        eq(sc.resolve_project_path("my demo"), p)
        ino = sc.create_arduino_sketch("blink test")
        assert os.path.exists(ino) and sc.find_arduino_sketch("blink test") == ino
        sc.write_file_content(p, "print(1)\n")
        sc.write_file_content(p, "print(2)\n")
        assert os.path.exists(p + ".bak"), "backup missing"
        assert "1 line added" in sc.summarize_diff("a\n", "a\nb\n")


def t_vault():
    sc = sc_mod()
    d = os.path.join(tmpdir, "vault_src")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "servo.txt"), "w") as f:
        f.write("my arduino servo robot arm project notes about torque")
    with mock.patch.object(sc, "BASE", tmpdir):
        ok, _ = sc.vault_add_folder(d)
        assert ok
        files, chunks = sc.vault_build()
        eq(files, 1)
        hits = sc.vault_search("what did I write about servo")
        assert hits and hits[0]["path"].endswith("servo.txt")


def t_apps_regex():
    sc = sc_mod()
    assert sc.APP_RE.search("open vs code").group(1) == "vs code"


def t_volume():
    sc = sc_mod()
    if not sc.PYCAW_AVAILABLE:
        skip("pycaw not available (Windows only)")
    v = sc.get_volume_percent()
    assert v is not None and 0 <= v <= 100


check("System control", "notes", t_notes)
check("System control", "code files, backups, diff summary", t_code_files)
check("System control", "keyword vault (add folder, build, search)", t_vault)
check("System control", "app launcher name matching", t_apps_regex)
check("System control", "master volume read (pycaw)", t_volume)


# ============================================================================ hand + face (no camera)
def t_filters():
    ht = mod("hand_tracker")
    f = ht.OneEuro()
    for i in range(10):
        f(i * 0.1, i * 0.033)
    d = ht.GestureDebouncer()
    for _ in range(5):
        c, _ = d.update("fist")
    eq(c, "fist")
    eq(round(ht._angle((1, 0, 0), (0, 0, 0), (0, 1, 0))), 90)


def t_face():
    f = mod("face_id")
    fid = f.FaceID()
    assert fid.detection_available, "Haar face detector failed to load (OpenCV install conflict)"
    assert fid.available, "cv2.face missing: pip uninstall opencv-python opencv-python-headless; pip install opencv-contrib-python"
    import numpy as np
    eq(fid.detect_faces(np.zeros((240, 320), np.uint8)), [])


check("Hand tracking", "smoothing filter, debouncer, joint angles", t_filters)
check("Face recognition", "detector + LBPH recogniser available", t_face)


# ============================================================================ addons
def circle(n=60):
    return [(0.5 + 0.2 * math.cos(2 * math.pi * i / n), 0.5 + 0.2 * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def poly(pts, per=15):
    out = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        out += [(a[0] + (b[0] - a[0]) * k / per, a[1] + (b[1] - a[1]) * k / per) for k in range(per)]
    return out + [out[0]]


SHAPES_IN = {
    "circle": circle(),
    "square": poly([(0.3, 0.3), (0.7, 0.3), (0.7, 0.7), (0.3, 0.7)]),
    "triangle": poly([(0.5, 0.25), (0.75, 0.7), (0.25, 0.7)]),
    "line": [(0.2 + 0.6 * i / 30, 0.5) for i in range(31)],
    "wave": [(0.1 + 0.8 * i / 80, 0.5 + 0.15 * math.sin(i / 80 * 3 * 2 * math.pi)) for i in range(81)],
    "spiral": [(0.5 + (0.05 + 0.25 * i / 100) * math.cos(i / 100 * 3 * 2 * math.pi),
                0.5 + (0.05 + 0.25 * i / 100) * math.sin(i / 100 * 3 * 2 * math.pi)) for i in range(101)],
}
for shp, pts in SHAPES_IN.items():
    check("Gesture drawing", f"recognises {shp}", (lambda shp=shp, pts=pts: eq(mod("addons").recognize(pts), shp)))
check("Gesture drawing", "rejects a tiny scribble", lambda: eq(mod("addons").recognize([(0.5, 0.5)] * 20), None))


def t_drawer():
    ad = mod("addons")
    H = mock.MagicMock()
    d = ad.GestureDrawer(H)
    d.set_active(True)
    for x, y in SHAPES_IN["circle"]:
        d.update(SimpleNamespace(raw_gesture="point", tip=(x, y)))
    d._last_point -= 1.0
    eq(d.update(None), "circle")
    H.load_shape.assert_called_with("sphere")


check("Gesture drawing", "drawer: stroke -> hologram sphere", t_drawer)


def t_expr():
    ad = mod("addons")
    for spoken, want in [("x squared", "x**2"), ("sine of x", "sin(x)"), ("2 x plus 1", "2*x + 1"),
                         ("x cubed minus 3 x", "x**3 - 3*x"), ("square root of x", "sqrt(x)"), ("e to the x", "exp(x)"),
                         ("x to the power of 4", "x**4")]:
        eq(ad.speech_to_expr(spoken), want)
    assert ad.speech_to_expr("delete everything") is None


check("Math plotter", "speech -> expression parsing", t_expr)


def t_spatial():
    ad = mod("addons")
    d = ad.SpatialDesktop()
    for phrase, want in [("move system panel behind me", "Moved"), ("bring the system panel forward", "Bringing"),
                         ("move notes panel left", "Moved"), ("push notes panel back", "Pushed"),
                         ("open browser panel", "Opened"), ("close the browser panel", "Closed"),
                         ("show my desktop", "system")]:
        ok, reply = ad.handle_spatial_command(d, phrase)
        assert ok and want.lower() in reply.lower(), f"{phrase!r} -> {reply!r}"
    eq(d.panels["system"].angle, 0.0)


def t_sandbox():
    ad = mod("addons")
    with mock.patch.object(ad, "SANDBOX_ROOT", os.path.join(tmpdir, "sandbox")):
        _, ok, out, err = ad.sandbox_write_and_run("hello", "print(2+2)")
        assert ok and out.strip() == "4", err
        _, ok, _, err = ad.sandbox_write_and_run("loop", "while True: pass", timeout=1)
        assert not ok and "Timed out" in err
        p = ad.sandbox_write("../../evil", "x")
        assert os.path.realpath(p).startswith(os.path.realpath(os.path.join(tmpdir, "sandbox"))), "path escaped sandbox"
        ad.sandbox_reset()
        eq(ad.sandbox_list(), [])


def t_addon_voice():
    ad = mod("addons")
    v, H, said = make_voice()
    v.drawer = ad.GestureDrawer(H)
    assert ad.handle_command(v, "draw mode") and v.drawer.active
    assert ad.handle_command(v, "stop drawing") and not v.drawer.active
    assert ad.handle_command(v, "move system panel behind me")
    assert not ad.handle_command(v, "tell me a joke")


check("Spatial desktop", "panel voice commands", t_spatial)
check("Code sandbox", "write/run, timeout kill, path jail, reset", t_sandbox)
check("Add-ons router", "draw mode + panels via handle_command", t_addon_voice)


# ============================================================================ sandbox labs
def t_physics():
    sl = mod("sandbox_labs")
    p = sl.Physics()
    r = p.create("create a projectile with 20 m/s velocity at 30 degrees")
    assert "Range" in r and p.sims[0]["kind"] == "proj"
    p.step(0.5)
    assert "Pendulum" in p.create("create a pendulum with length 2 meters")
    assert "spring" in p.create("create a spring with constant 40").lower()
    assert "Dropping" in p.create("drop a ball from 15 meters on the moon")


def t_molecule():
    sl = mod("sandbox_labs")
    m = sl.Molecule()
    m.add("C")
    m.fill_hydrogens()
    eq(m.formula(), "CH4")
    eq(m.name(), "methane")
    m.clear()
    m.add("O")
    m.fill_hydrogens()
    eq(m.formula(), "H2O")


def t_circuit():
    sl = mod("sandbox_labs")
    c = sl.Circuit()
    c.preset("led")
    for _ in range(40):
        c.frame(0.04)
    led = next(x for x in c.comps if x["kind"] == "led")
    assert led["on"] and not led["burnt"] and 0.005 < led["i"] < 0.03, f"LED i={led['i']}"
    c.clear()
    c.add("battery", 5)
    c.add("led")
    c.auto_wire()
    for _ in range(40):
        c.frame(0.04)
    assert next(x for x in c.comps if x["kind"] == "led")["burnt"], "LED without resistor should burn out"


def t_labs_voice():
    sl = mod("sandbox_labs")
    H = mock.MagicMock()
    H.labs = None      # a MagicMock auto-creates .labs, so install() would reuse a fake instead of the real Labs
    said = []
    assert sl.handle_command(H, "circuit sandbox", said.append) and "Circuit" in said[0]
    assert sl.handle_command(H, "add a 220 ohm resistor", said.append)
    assert sl.handle_command(H, "create a projectile with 20 m/s velocity", said.append)
    assert not sl.handle_command(H, "what is the capital of france", said.append)


check("Sandbox labs", "physics simulations", t_physics)
check("Sandbox labs", "molecule builder (valence + formula)", t_molecule)
check("Sandbox labs", "circuit simulator (LED on / burnout)", t_circuit)
check("Sandbox labs", "voice commands", t_labs_voice)


# ============================================================================ aurora plus
def make_plus(client=None):
    ap = mod("aurora_plus")
    voice = SimpleNamespace(client=client, _on_log=lambda m: None, state="idle", last_reply="", speak_now=lambda t: None,
                            _speak=lambda t: None, run_text=lambda t: f"echo:{t}")
    return ap, ap.Plus(voice, mock.MagicMock())


def t_guard():
    ap = mod("aurora_plus")
    G = ap.Guard
    assert not G.text_ok("api_key = gsk_abcdefghijklmnopqrstuvwxyz")[0]
    assert not G.text_ok("write a keylogger")[0]
    assert G.text_ok("build a prime number checker")[0]
    assert not G.python_ok("import subprocess", set())[0]
    assert not G.python_ok("import os\nos.system('x')", set())[0]
    assert not G.python_ok("eval('1')", set())[0]
    assert not G.python_ok("open('/etc/passwd')", set())[0]
    assert not G.python_ok("import requests", set())[0]
    assert G.python_ok("import math\nprint(math.pi)", set())[0]
    assert not G.path_ok("../x.py")[0] and not G.path_ok("a.exe")[0] and G.path_ok("calc.py")[0]
    assert G.command_ok(ap.TEST_CMD)[0] and not G.command_ok(["rm", "-rf", "/"])[0]


def t_cowork_pipeline():
    ap, plus = make_plus(client=object())
    plus.guard.judge = lambda k, d: (True, "")
    root = tempfile.mkdtemp(dir=tmpdir)
    reply = ("### FILE: calc.py\ndef add(a, b):\n    return a + b\n### END\n"
             "### FILE: test_calc.py\nimport unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n### END\n")
    files = ap.parse_files(reply)
    eq(sorted(files), ["calc.py", "test_calc.py"])
    ok, why = plus.cowork._apply(root, files)
    assert ok, why
    ok, out = plus.cowork._test(root)
    assert ok, out[-300:]
    ok, why = plus.cowork._apply(root, {"evil.py": "import subprocess\nsubprocess.run(['ls'])\n"})
    assert not ok, "malicious file must be rejected"
    ok, why = plus.cowork._apply(root, {"x.py": "print(1)\n"})
    plus.guard.judge = lambda k, d: (False, "no")
    ok, why = plus.cowork._apply(root, {"y.py": "print(1)\n"})
    assert not ok and "safeguard" in why, "safeguard must fail closed"


def t_semantic():
    ap, plus = make_plus()
    sc = sc_mod()
    d = os.path.join(tmpdir, "sem_src")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "budget.txt"), "w") as f:
        f.write("monthly budget: rent and groceries expenses total")
    with open(os.path.join(d, "rocket.txt"), "w") as f:
        f.write("model rocket launch notes about the estes engine")
    with mock.patch.object(sc, "BASE", tmpdir), mock.patch.object(ap, "INDEX_FILE", os.path.join(tmpdir, "sem.json")):
        sc.vault_add_folder(d)
        plus.search = ap.SemanticSearch(plus)
        nf, nc = plus.search.build()
        assert nf >= 2
        hits = plus.search.query("money and payments")
        assert hits and hits[0]["path"].endswith("budget.txt"), hits


def t_offline():
    ap, plus = make_plus()
    assert "offline" in plus.offline.answer("hello").lower()
    assert "Today is" in plus.offline.answer("what day is it")
    plus.offline.force = True
    assert not plus.offline.online()
    b = ap.OfflineAwareBrain(plus, None)
    got = []
    r = b.ask("hello", got.append, threading.Event())
    assert r and got, "offline brain must answer without an API"


def t_plus_routes():
    ap, plus = make_plus()
    said = []
    plus.voice._speak = said.append
    assert plus.route("offline mode on") and plus.offline.force
    assert plus.route("offline mode off") and not plus.offline.force
    assert plus.route("cowork status") and "idle" in said[-1]
    assert plus.route("cowork build a thing") and "offline" in said[-1].lower()     # no client
    assert not plus.route("what's the weather")


def t_hub():
    ap, plus = make_plus()
    with mock.patch.object(ap, "TOKEN_FILE", os.path.join(tmpdir, "tok.txt")):
        plus.hub.port = 18765
        ok, url = plus.hub.start()
        assert ok, url
        token = plus.hub.token
        try:
            def call(path, body=None, tok=token):
                req = urllib.request.Request(f"http://127.0.0.1:18765{path}", data=json.dumps(body).encode() if body else None,
                                             headers={"X-Token": tok, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.loads(r.read().decode())
                except urllib.error.HTTPError as e:
                    return e.code, {}
            eq(call("/cmd", {"text": "hello"}, tok="wrong")[0], 401)
            code, data = call("/cmd", {"text": "aurora hello"})
            assert code == 200 and data["reply"] == "echo:hello", data
            assert "isn't allowed" in call("/cmd", {"text": "unlock my phone"})[1]["reply"]
            eq(call("/status")[0], 200)
        finally:
            plus.hub.stop()
        assert plus.hub.server is None


def t_install():
    ap = mod("aurora_plus")
    va = mod("voice_assistant")
    v, H, said = make_voice()
    v.recognizer = None
    v.speech = va.SpeechEngine(lambda m: None)
    v._speak = said.append   # the mock speech engine that fills `said` was just replaced
    plus = ap.install(v, H)
    assert v.plus is plus and v.speech.use_edge == plus.offline.online
    assert v._handle_local_command("offline mode status") and any("currently" in s for s in said)
    assert v.brain.ask("hello there", lambda d: None, threading.Event())


check("Aurora Plus", "safety guard (secrets, malware, path, command)", t_guard)
check("Aurora Plus", "Cowork write/validate/test pipeline + safeguard fail-closed", t_cowork_pipeline)
check("Aurora Plus", "semantic file search", t_semantic)
check("Aurora Plus", "offline mode answers", t_offline)
check("Aurora Plus", "voice routes (offline, cowork)", t_plus_routes)
check("Aurora Plus", "network hub (auth, LAN-only, blocked commands)", t_hub)
check("Aurora Plus", "install() wires into VoiceAssistant", t_install)


# ============================================================================ hardware (--hardware)
def hw(name, fn):
    check("Hardware", name, fn if HARDWARE else (lambda: skip("run with --hardware")))


def t_camera():
    import cv2
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(0)
    ok, frame = cap.read()
    cap.release()
    assert ok and frame is not None, "camera 0 gave no frame (close Zoom/Teams/browser tabs using it)"
    return f"{frame.shape[1]}x{frame.shape[0]}"


def t_mic():
    import speech_recognition as sr
    names = sr.Microphone.list_microphone_names()
    assert names, "no audio input devices (create mic_index.txt after installing pyaudio)"
    return f"{len(names)} input device(s)"


def t_adb():
    ok, out = sc_mod().is_device_connected()
    if not ok and "not found" in out:
        skip("adb not installed / not on PATH")
    assert ok, "adb found but no phone authorised (enable USB debugging)"


def t_tracker():
    ad = mod("addons")
    tr = ad.TipHandTracker(0)
    try:
        for _ in range(5):
            result, frame = tr.read()
        assert frame is not None, "no frame from tracker"
        return "hand detected" if result.detected else "tracker running (no hand in view)"
    finally:
        tr.close()


hw("camera opens and returns frames", t_camera)
hw("microphone present", t_mic)
hw("Android phone over ADB", t_adb)
hw("MediaPipe hand tracker runs", t_tracker)


# ============================================================================ GUI (--gui)
def gui(name, fn):
    check("Hologram (GUI)", name, fn if GUI else (lambda: skip("run with --gui")))


_holo = {}


def get_holo():
    if "h" not in _holo:
        _holo["h"] = mod("hologram").Hologram(fullscreen=False, windowed_size=(1100, 720))
        mod("addons").install(_holo["h"], mod("addons").GestureDrawer(_holo["h"]))
    return _holo["h"]


def render_case(label, loader):
    def run():
        h = get_holo()
        loader(h)
        for _ in range(3):
            h.process_events()
            h.update(0.1, 0.1, 0.0, target_dt=1 / 30)
            h.render()
    gui(label, run)


CASES = [
    ("idle network view", lambda h: h.load_network()),
    ("demo orbits", lambda h: h.load_demo()),
    ("carbon atom", lambda h: h.load_atom("carbon")),
    ("solar system", lambda h: h.load_solar_system()),
    ("star system (TRAPPIST-1)", lambda h: h.load_star_system("trappist-1")),
    ("shapes: sphere/cube/torus/pyramid/cylinder/eiffel/skyscraper/dna",
     lambda h: [h.load_shape(s) or h.render() for s in ("sphere", "cube", "torus", "pyramid", "cylinder", "eiffel tower", "skyscraper", "dna")]),
    ("heart model", lambda h: h.load_shape("heart")),
    ("molecule", lambda h: h.load_molecule("water")),
    ("math graph", lambda h: h.load_math_graph("sin(x)")),
    ("physics sim", lambda h: h.load_physics_sim("pendulum")),
    ("satellite", lambda h: h.load_satellite("ISS")),
    ("constellation", lambda h: h.load_constellation("orion")),
    ("telemetry panel", lambda h: (h.show_telemetry("test"), h.update_telemetry({"temp": 21.5, "hum": 40}, True))),
    ("info card", lambda h: h.show_info_card("q", "an answer " * 20)),
    ("weather panel", lambda h: h.show_weather({"location": "Tokyo", "temp_c": 20, "condition": "rain", "description": "rain",
                                                "humidity": 80, "wind_kph": 10, "updated_at": "12:00"})),
    ("spatial panels overlay", lambda h: setattr(h, "spatial", mod("addons").SpatialDesktop())),
    ("circuit sandbox panel", lambda h: (mod("sandbox_labs").install(h), h.labs.open("circuit"), h.labs.circ.preset("led"))),
    ("molecule builder panel", lambda h: (mod("sandbox_labs").install(h), h.labs.open("molecule"), h.labs.mol.load_preset("methane"))),
    ("physics sandbox panel", lambda h: (mod("sandbox_labs").install(h), h.labs.open("physics"), h.labs.phys.create("create a projectile with 20 m/s velocity"))),
]
for label, loader in CASES:
    render_case(label, loader)


def t_gui_close():
    if _holo.get("h"):
        _holo["h"].close()


gui("window closes cleanly", t_gui_close)


# ============================================================================ live (--live)
def live(name, fn):
    check("Live services", name, fn if LIVE else (lambda: skip("run with --live")))


def get_client():
    va = mod("voice_assistant")
    key = va.load_api_key()
    if not key or va.Groq is None:
        requires("API", "no api_key.txt / groq package")
    return va.Groq(api_key=key)


def t_groq():
    b = mod("brain")
    out = b._complete(get_client(), "Reply with the single word OK.", "ping", 400, effort="low")
    assert out, "empty reply"
    return out[:30]


def t_groq_tools():
    b = mod("brain")
    calls = []
    br = b.Brain(get_client(), lambda m: None, lambda n, a: calls.append(n) or "Timer set")
    reply = br.ask("Set a timer for 10 seconds", lambda d: None, threading.Event())
    assert "set_timer" in calls, f"model did not call set_timer (calls={calls}, reply={reply!r})"


def t_groq_code():
    b = mod("brain")
    code = b.generate_code(get_client(), "print the numbers 1 to 3", "python")
    assert "print" in code and "```" not in code


def t_tts():
    import asyncio
    va = mod("voice_assistant")
    if not va.EDGE_TTS_AVAILABLE:
        skip("edge-tts not installed")
    p = os.path.join(tmpdir, "tts.mp3")
    asyncio.run(va._edge_save("Testing Aurora.", p))
    assert os.path.getsize(p) > 1000


def t_weather():
    v, H, said = make_voice()
    d = v._fetch_weather_data("Tokyo")
    assert d and d["temp_c"] is not None, f"weather failed ({v.last_weather_error})"
    return f"{d['location']} {d['temp_c']}C"


live("Groq chat (gpt-oss-120b)", t_groq)
live("Groq tool calling", t_groq_tools)
live("Groq code generation", t_groq_code)
live("Edge neural TTS", t_tts)
live("Open-Meteo weather", t_weather)


# ============================================================================ productivity features
import zipfile
from datetime import datetime, timedelta


# Legacy module names (before everything was merged) resolve to aurora_utilities instead of "module not found".
_LEGACY_UTILITY_MODULES = {"quick_notes", "file_utilities", "calendar_email", "notification_center", "translation",
                           "study_mode", "skill_builder", "productivity_commands"}
_mod_original = mod


def mod(name):
    if name in _LEGACY_UTILITY_MODULES:
        name = "aurora_utilities"
    return _mod_original(name)



class FakeClient:
    """Stands in for Groq: brain._complete(client, ...) is patched to call .reply()."""
    def __init__(self, text="ok"):
        self.text, self.calls = text, []

    def reply(self, system, user):
        self.calls.append((system, user))
        return self.text


def with_complete(client):
    return mock.patch.object(mod("brain"), "_complete", lambda c, s, u, *a, **k: c.reply(s, u))


def util():
    return mod("aurora_utilities")


def newdir(prefix):
    return tempfile.mkdtemp(prefix=prefix, dir=tmpdir)


# ---- Quick notes
def t_qn():
    sc, qn = sc_mod(), util()
    with mock.patch.object(sc, "NOTES_FILE", os.path.join(newdir("qn"), "notes.txt")):
        qn.add_quick_note("buy milk and eggs")
        qn.add_quick_note("call the dentist")
        eq([n[2] for n in qn.list_notes()], ["buy milk and eggs", "call the dentist"])
        eq(qn.search_notes("what did i note about dentist")[0][2], "call the dentist")
        eq(qn.get_quick_note(1)[2], "buy milk and eggs")
        assert qn.delete_note(1) and not qn.delete_note(9)
        eq(len(qn.list_notes()), 1)
        eq(qn.delete_all_notes(), 1)
        eq(qn.list_notes(), [])


check("Quick notes", "add, list, search, read, delete", t_qn)


# ---- File utilities
def t_files():
    fu = util()
    home = os.path.join(newdir("home"), "AppData", "Local", "Temp", "h")       # Windows temp dirs live under AppData
    for d in ("Downloads", "Documents"):
        os.makedirs(os.path.join(home, d))
    dl = os.path.join(home, "Downloads")
    for n in ("report.pdf", "pic.png", "song.mp3", "setup.exe", "notes.txt", "weird.zzz"):
        open(os.path.join(dl, n), "w").write("x")
    with mock.patch.object(fu, "HOME", home):
        eq(fu.find_file("report", fu.folder_path("downloads")), [os.path.join(dl, "report.pdf")])
        new = fu.rename_file(os.path.join(dl, "report.pdf"), "summary")
        eq(os.path.basename(new), "summary.pdf")                        # extension kept
        open(os.path.join(dl, "again.pdf"), "w").write("y")
        eq(os.path.basename(fu.rename_file(os.path.join(dl, "again.pdf"), "summary.pdf")), "summary (1).pdf")  # no overwrite
        moved = fu.move_file(os.path.join(dl, "summary.pdf"), os.path.join(home, "Documents"))
        assert os.path.exists(moved) and not os.path.exists(os.path.join(dl, "summary.pdf"))
        z = fu.compress_path(os.path.join(dl, "notes.txt"))
        out = fu.extract_zip(z)
        assert os.path.exists(os.path.join(out, "notes.txt"))
        plan = fu.plan_organize(dl)
        cats = {os.path.basename(s): c for s, c in plan}
        assert cats["pic.png"] == "Images" and cats["song.mp3"] == "Audio" and cats["weird.zzz"] == "Other"
        assert fu.organize_folder(dl, plan) == len(plan)
        assert os.path.exists(os.path.join(dl, "Images", "pic.png"))
        for bad in (lambda: fu.rename_file(os.path.join(dl, "Images", "pic.png"), "../x.png"),
                    lambda: fu.move_file(os.path.join(dl, "Images", "pic.png"), tempfile.gettempdir()),
                    lambda: fu.rename_file(os.path.abspath(__file__), "x.py")):
            try:
                bad()
            except fu.FileError:
                continue
            raise AssertionError("unsafe file operation was allowed")
        evil = os.path.join(dl, "evil.zip")
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../../escaped.txt", "x")
        try:
            fu.extract_zip(evil)
            raise AssertionError("zip-slip not blocked")
        except fu.FileError:
            pass
        assert not os.path.exists(os.path.join(home, "..", "escaped.txt"))


check("File utilities", "rename/move/compress/extract/organize + safety limits", t_files)

# ---- Calendar
ICS = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Standup\r\nDTSTART:{d}T090000\r\nDTEND:{d}T091500\r\n"
       "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=100\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nSUMMARY:Dentist\\, Dr. Lee\r\nDTSTART:{t}T150000\r\nDTEND:{t}T160000\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nSUMMARY:Birthday\r\nDTSTART;VALUE=DATE:{t}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def cal_env():
    cal = util()
    d = newdir("cal")
    today = datetime.now().date()
    ics = os.path.join(d, "c.ics")
    open(ics, "w").write(ICS.format(d=(today - timedelta(days=14)).strftime("%Y%m%d"), t=(today + timedelta(days=1)).strftime("%Y%m%d")))
    json.dump({"ics": [ics]}, open(os.path.join(d, "calendar_config.json"), "w"))
    return cal, d


def t_calendar():
    cal, d = cal_env()
    with mock.patch.object(cal, "BASE", d):
        tomorrow = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        evs, errs = cal.events_between(tomorrow, tomorrow + timedelta(days=1))
        titles = {e["title"] for e in evs}
        assert not errs and "Dentist, Dr. Lee" in titles and "Birthday" in titles, titles
        if tomorrow.weekday() < 5:
            assert "Standup" in titles, "weekly recurrence missing"
        title, start = cal.parse_event_phrase("dentist tomorrow at 3:30 pm", datetime(2026, 10, 1, 8, 0))
        eq((title, start), ("dentist", datetime(2026, 10, 2, 15, 30)))
        eq(cal.parse_event_phrase("lunch with sam", datetime(2026, 10, 1))[1], None)
        cal.add_local_event("Study group", datetime.now() + timedelta(hours=2))
        ev, _ = cal.next_event()
        assert ev and ev["title"] == "Study group" or ev, "next_event failed"
        evs, _ = cal.events_between(datetime.now(), datetime.now() + timedelta(days=1))
        assert "Study group" in {e["title"] for e in evs}


check("Calendar", "ICS parsing, recurrence, time phrases, local events, next event", t_calendar)
check("Calendar", "live calendar source (ICS URL / file you configured)",
      lambda: (_ for _ in ()).throw(Skip("no calendar_config.json in project root", "REQUIRES PERMISSION"))
      if not os.path.exists(os.path.join(ROOT, "calendar_config.json")) else
      (lambda c: (c.events_between(datetime.now(), datetime.now() + timedelta(days=7)) and None))(util()))


# ---- Email
RAW = (b"From: =?utf-8?q?Ann_Lee?= <ann@example.com>\r\nSubject: Project update\r\nDate: Mon, 1 Oct 2026 09:00:00 +0000\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\nThe demo moved to Friday.\r\nPlease confirm.\r\n")


def mail_env():
    cal = util()
    d = newdir("mail")
    json.dump({"imap_host": "imap.test", "smtp_host": "smtp.test", "username": "me@test.com", "contacts": {"ann": "ann@example.com"}},
              open(os.path.join(d, "email_config.json"), "w"))
    return cal, d


def t_email_logic():
    cal, d = mail_env()
    with mock.patch.object(cal, "BASE", d), mock.patch.dict(os.environ, {"AURORA_EMAIL_PASSWORD": "pw"}):
        assert cal.email_configured()
        eq(cal.resolve_recipient("ann"), "ann@example.com")
        eq(cal.resolve_recipient("bob at example dot com"), "bob@example.com")
        eq(cal.resolve_recipient("nobody"), None)
        box = mock.MagicMock()
        box.search.return_value = (None, [b"1"])
        box.fetch.return_value = (None, [(b"1", RAW)])
        with mock.patch.object(cal.imaplib, "IMAP4_SSL", return_value=box):
            msgs = cal.fetch_unread(3)
        eq((msgs[0]["from"], msgs[0]["subject"]), ("Ann Lee", "Project update"))
        assert "Friday" in msgs[0]["body"]
        box.select.assert_called_with("INBOX", readonly=True)
        assert "PEEK" in box.fetch.call_args[0][1], "must not mark mail as read"
        draft = cal.save_draft("ann@example.com", "Hi", "Body")
        eq(cal.latest_draft()["to"], "ann@example.com")
        try:
            cal.send_draft(draft)
            raise AssertionError("send without confirmation was allowed")
        except PermissionError:
            pass
        smtp = mock.MagicMock()
        with mock.patch.object(cal.smtplib, "SMTP", return_value=smtp):
            assert cal.send_draft(draft, confirmed=True)
        smtp.send_message.assert_called_once()
        assert cal.discard_draft() and cal.latest_draft() is None
    with mock.patch.object(cal, "BASE", newdir("nomail")):
        try:
            cal.fetch_unread()
            raise AssertionError("expected MailError")
        except cal.MailError:
            pass


check("Email", "IMAP read (mocked), drafts, send needs confirmed=True, recipients", t_email_logic)
check("Email", "real mailbox read", lambda: requires("API", "needs email_config.json + AURORA_EMAIL_PASSWORD")
      if not util().email_configured() else (util().fetch_unread(1) and None))


# ---- Skill builder
def t_skills():
    skm = util()
    with mock.patch.object(skm, "BASE", newdir("sk")):
        eq(skm.parse_steps("open chrome then what's on my calendar today; set a timer for 5 minutes"),
           ["open chrome", "what's on my calendar today", "set a timer for 5 minutes"])
        skm.teach_skill("Morning", ["a", "b"])
        eq(skm.skill_names(), ["morning"])
        eq(skm.add_step("the morning skill", "c"), 3)
        skm.edit_step("morning", 2, "b2")
        skm.remove_step("morning", 1)
        eq(skm.get_skill("morning")["steps"], ["b2", "c"])
        eq(skm.run_skill("morning", lambda s: s.upper()), [("b2", "B2"), ("c", "C")])
        skm.rename_skill("morning", "evening")
        for bad in (lambda: skm.teach_skill("loop", ["run skill loop"]), lambda: skm.teach_skill("x", []), lambda: skm.edit_step("evening", 9, "z")):
            try:
                bad()
                raise AssertionError("expected SkillError")
            except skm.SkillError:
                pass
        skm.delete_skill("evening")
        eq(skm.skill_names(), [])


check("Skill builder", "teach, list, edit, rename, delete, run, recursion guard", t_skills)


# ---- Notification center
DUMP = """  NotificationRecord(0x1: pkg=com.whatsapp user=UserHandle{0} id=1 importance=3 key=0|com.whatsapp|1)
      extras={
        android.title=String (Mom)
        android.text=CharSequence (Dinner at 7?)
  NotificationRecord(0x2: pkg=com.android.systemui user=UserHandle{0} id=2 importance=2 key=x)
      extras={
        android.title=String (USB debugging connected)
  NotificationRecord(0x3: pkg=com.google.android.gm user=UserHandle{0} id=3 importance=3 key=y)
      extras={
        android.title=Bob
        android.text=Invoice attached
"""


def t_notifs():
    nc = util()
    p = nc.parse_phone_dump(DUMP)
    eq([(x["package"], x["title"], x["text"]) for x in p],
       [("com.whatsapp", "Mom", "Dinner at 7?"), ("com.google.android.gm", "Bob", "Invoice attached")])
    c = nc.NotificationCenter()
    assert c.push("phone", "a") and not c.push("phone", "a")                 # de-duplicated
    c.push("calendar", "Meeting soon")
    eq(c.ranked()[0]["source"], "calendar")                                  # calendar outranks phone
    sc, cal = sc_mod(), util()
    with mock.patch.object(sc, "is_device_connected", return_value=(True, "")), \
            mock.patch.object(sc, "_shell", return_value=(True, DUMP)), \
            mock.patch.object(cal, "email_configured", return_value=False), mock.patch.object(cal, "BASE", newdir("nc")):
        c2 = nc.NotificationCenter()
        eq(c2.collect(), [])
        assert any("Mom" in n["title"] for n in c2.items)
        eq(c2.clear(), 2)


check("Notification center", "ADB dump parsing, dedupe, ranking, collect (mocked phone)", t_notifs)


def t_notifs_hw():
    ok, out = sc_mod().is_device_connected()
    if not ok:
        requires("HARDWARE", "no authorised Android phone over ADB")
    util().NotificationCenter().collect()


check("Notification center", "real phone notifications", t_notifs_hw)


# ---- Study mode
def t_study():
    sm = util()
    with mock.patch.object(sm, "BASE", newdir("st")):
        sm.add_card("Bio", "Powerhouse of the cell?", "mitochondria")
        sm.add_cards("bio", [{"q": "Plants make food by?", "a": "photosynthesis"}])
        eq(sm.list_decks(), {"bio": 2})
        eq(sm.find_deck("biology"), "bio")
        assert sm.grade_answer("the mitochondria", "mitochondria") and sm.grade_answer("photosyntesis", "photosynthesis")
        assert not sm.grade_answer("nucleus", "mitochondria")
        name, cards = sm.get_cards("bio")
        s = sm.StudySession(name, cards, "quiz", shuffle=False)
        assert s.check("mitochondria") and not (s.next() and s.check("wrong"))
        eq(s.score_text(), "1 of 2 correct")
        assert s.next() is None
        said = []
        voice = SimpleNamespace(speak_now=said.append, active_timers=[])
        t = sm.StudyTimer(voice, seconds_per_minute=0.02)
        assert t.start(1, pomodoro=True, rest=1, cycles=2) and not t.start(1)
        time.sleep(0.6)
        assert not t.running and sm.minutes_today() == 2 and any("complete" in x for x in said), said
        t.start(5)
        assert t.stop()
        time.sleep(0.2)
        assert not t.running and voice.active_timers == []
        with with_complete(None):
            cards = sm.generate_cards(FakeClient('Sure! [{"q":"2+2?","a":"4"},{"q":"","a":"x"}]'), "math", 5)
        eq(cards, [{"q": "2+2?", "a": "4"}])
        assert sm.delete_deck("bio") and sm.list_decks() == {}


check("Study mode", "decks, grading, quiz session, pomodoro timer, AI card parsing (mocked)", t_study)


# ---- Translation
def t_translation():
    tr = util()
    eq(tr.parse_translation_request("translate good morning to spanish"), ("good morning", "spanish"))
    eq(tr.parse_translation_request("Translate to French: where is the library"), ("where is the library", "french"))
    eq(tr.parse_translation_request("how do you say thank you in japanese"), ("thank you", "japanese"))
    eq(tr.parse_translation_request("what is the weather"), None)
    eq(tr.voice_for_language("spanish"), "es-ES-AlvaroNeural")
    with with_complete(None):
        eq(tr.translate_text(FakeClient('"Buenos días"'), "good morning", "spanish"), "Buenos días")


check("Translation", "request parsing, voice mapping, translate (mocked model)", t_translation)


def t_translation_live():
    tr = util()
    out = tr.translate_text(get_client(), "good morning", "spanish")
    assert "buen" in out.lower(), out


live("Translation (real Groq)", t_translation_live)
live("Study flashcards (real Groq)", lambda: eq(len(util().generate_cards(get_client(), "the water cycle", 3)) >= 2, True))


# ---- Voice routing end to end (confirmation rules are the important part)
def pv():
    v, H, said = make_voice()
    return v, said, util()


def say(pc, v, phrase):
    return pc.handle_command(v, phrase)


def t_route_confirm_files():
    fu = util()
    home = os.path.join(newdir("rhome"), "AppData", "Local", "Temp", "h")
    dl = os.path.join(home, "Downloads")
    os.makedirs(dl)
    os.makedirs(os.path.join(home, "Documents"))
    open(os.path.join(dl, "report.pdf"), "w").write("x")
    v, said, pc = pv()
    with mock.patch.object(fu, "HOME", home):
        assert say(pc, v, "rename report.pdf in downloads to summary.pdf")
        assert os.path.exists(os.path.join(dl, "report.pdf")), "renamed BEFORE confirmation"
        assert "yes" in said[-1].lower(), f"no confirmation prompt: {said[-1:]}"
        assert say(pc, v, "yes") and os.path.exists(os.path.join(dl, "summary.pdf")), f"rename after yes failed: {said[-1:]}"
        assert say(pc, v, "move summary.pdf in downloads to documents") and os.path.exists(os.path.join(dl, "summary.pdf"))
        assert say(pc, v, "no") and os.path.exists(os.path.join(dl, "summary.pdf")), "moved despite 'no'"
        assert say(pc, v, "move summary.pdf in downloads to documents")
        pc.handle_command(v, "list my skills")                     # an unrelated command must cancel the pending move
        assert not (say(pc, v, "yes")), "stale confirmation executed"
        assert os.path.exists(os.path.join(dl, "summary.pdf"))
        assert say(pc, v, "organize my downloads") and os.path.exists(os.path.join(dl, "summary.pdf")), said[-1:]
        assert say(pc, v, "yes") and os.path.exists(os.path.join(dl, "Documents", "summary.pdf")), f"organize failed: {said[-1:]}"
        assert not say(pc, v, "move the panel to the left"), "must not steal non-file phrases"
        open(os.path.join(home, "Documents", "todo.txt"), "w").write("x")
        assert say(pc, v, "zip todo.txt in documents") and os.path.exists(os.path.join(home, "Documents", "todo.txt.zip"))


def t_route_email_send():
    cal, d = mail_env()
    v, said, pc = pv()
    sent = mock.MagicMock()
    with mock.patch.object(cal, "BASE", d), mock.patch.dict(os.environ, {"AURORA_EMAIL_PASSWORD": "pw"}), \
            mock.patch.object(cal.smtplib, "SMTP", return_value=sent), with_complete(None):
        v.client = FakeClient("Subject: Demo\n\nSee you Friday.")
        assert say(pc, v, "draft an email to ann about the demo on friday")
        assert cal.latest_draft()["to"] == "ann@example.com"
        sent.send_message.assert_not_called()
        assert say(pc, v, "send the email") and "about to send" in said[-1]
        sent.send_message.assert_not_called()
        assert say(pc, v, "no")
        sent.send_message.assert_not_called()
        say(pc, v, "send the email")
        say(pc, v, "yes")
        sent.send_message.assert_called_once()


def t_route_notes_skills_calendar():
    sc, skm = sc_mod(), util()
    v, said, pc = pv()
    cal, d = cal_env()
    with mock.patch.object(sc, "NOTES_FILE", os.path.join(newdir("rn"), "notes.txt")), mock.patch.object(skm, "BASE", newdir("rs")), \
            mock.patch.object(cal, "BASE", d):
        assert say(pc, v, "remember this: pick up the parcel") and "saved" in said[-1].lower()
        assert not say(pc, v, "remember my face as sam"), "face enrollment must still reach its own handler"
        assert say(pc, v, "search notes for parcel") and "parcel" in said[-1]
        assert say(pc, v, "delete all notes") and "permanently" in said[-1]
        assert say(pc, v, "no") and len(util().list_notes()) == 1
        assert say(pc, v, "delete note 1") and util().list_notes() == []
        assert say(pc, v, "teach a skill called warmup: what time is it then list my skills")
        assert skm.get_skill("warmup") and len(skm.get_skill("warmup")["steps"]) == 2
        assert say(pc, v, "list my skills") and "warmup" in said[-1]
        assert say(pc, v, "run skill warmup") and "Finished warmup" in said[-1]
        assert say(pc, v, "delete skill warmup") and skm.get_skill("warmup")
        assert say(pc, v, "yes") and not skm.get_skill("warmup")
        assert say(pc, v, "what do i have tomorrow") and ("Dentist" in said[-1] or "Birthday" in said[-1]), said[-1]
        assert say(pc, v, "add lunch with sam tomorrow at noon to my calendar") and "yes" in said[-1].lower()
        assert not os.path.exists(os.path.join(d, "calendar_local.json")), "calendar changed before confirmation"
        assert say(pc, v, "yes") and os.path.exists(os.path.join(d, "calendar_local.json"))


def t_route_study_translate():
    sm = util()
    v, said, pc = pv()
    with mock.patch.object(sm, "BASE", newdir("rst")), with_complete(None):
        v.client = FakeClient('[{"q":"Capital of France?","a":"Paris"},{"q":"Capital of Italy?","a":"Rome"}]')
        assert say(pc, v, "make 2 flashcards about capitals") and sm.list_decks() == {"capitals": 2}
        assert say(pc, v, "quiz me on capitals") and v._productivity.session.mode == "quiz"
        for _ in range(2):
            card = v._productivity.session.card
            assert say(pc, v, f"the answer is {card['a']}")
        assert "2 of 2" in said[-1] and v._productivity.session is None
        v.client = FakeClient("Buenos días")
        assert say(pc, v, "translate good morning to spanish") and v.hologram.show_info_card.called
        assert "Buenos días" in v.last_reply, f"last_reply={v.last_reply!r}"
        v.client = None
        assert say(pc, v, "translate hello to french") and "Groq" in said[-1]


for _name, _fn in (("confirm-before-change for files", t_route_confirm_files), ("email never sends without yes", t_route_email_send),
                   ("notes, skills, calendar confirmation", t_route_notes_skills_calendar), ("study quiz + translation", t_route_study_translate)):
    check("Productivity voice routing", _name, _fn)


def t_hook():
    v, H, said = make_voice()
    assert v._handle_local_command("list my skills"), "productivity router not hooked"
    assert v._handle_local_command("translate hello to french"), "productivity router not hooked into voice_assistant"
    assert v._handle_local_command("take a note: buy milk"), "existing notes command broke"


check("Productivity voice routing", "hooked into VoiceAssistant._handle_local_command (existing commands intact)", t_hook)


# ============================================================================ dashboard modes + system monitor
def modes_env():
    dm = mod("dashboard_modes")
    v, _, said = make_voice()
    H = mock.MagicMock(name="HologramForModes")
    H.overlays, H.event_listeners, H.quiet_alerts = [], [], False
    H.modes = H.labs = None            # MagicMock auto-creates attributes; install() must see None to build real ones
    H.mode, H.info_card, H.translate_x = "empty", None, 0.0
    v.hologram = H
    v.speak_now = said.append
    return dm, H, v, said


def t_slide_parse():
    dm = mod("dashboard_modes")
    s = dm.parse_slides("# A\n- x\n- y\nNotes: hi\nDemo: show me the solar system\n---\n# B")
    eq(len(s), 2)
    eq((s[0]["title"], s[0]["bullets"], s[0]["notes"], s[0]["demo"]), ("A", ["x", "y"], "hi", "show me the solar system"))


def t_presentation_flow():
    dm, H, v, said = modes_env()
    with mock.patch.object(dm, "load_deck", lambda name=None: (dm.SAMPLE_DECK, "test deck")):
        assert dm.handle_command(v, "presentation mode")
        m = H.modes
        assert m.active == "presentation" and H.quiet_alerts is True
        assert dm.handle_command(v, "next slide") and m.presentation.i == 1
        H.load_atom.assert_called_with("carbon")                      # slide 2's Demo: line ran
        assert dm.handle_command(v, "previous slide") and m.presentation.i == 0
        assert dm.handle_command(v, "last slide") and m.presentation.i == 3
        assert dm.handle_command(v, "next slide") and "last slide" in said[-1]
        assert dm.handle_command(v, "show speaker notes") and m.presentation.show_notes
        assert dm.handle_command(v, "set a timer for 10 minutes") and m.presentation.timer_end
        assert dm.handle_command(v, "blank screen") and m.presentation.blank
        assert dm.handle_command(v, "end presentation") and m.active is None and H.quiet_alerts is False
        assert not dm.handle_command(v, "next slide"), "must not react outside presentation mode"


def t_timeline():
    dm, H, v, said = modes_env()
    d = tempfile.mkdtemp(dir=tmpdir)
    with mock.patch.object(dm, "EVENTS_FILE", os.path.join(d, "ev.jsonl")), \
            mock.patch.object(dm, "_git_commits", lambda limit=40: [(time.time() - 50, "git", "fix bug")]), \
            mock.patch.object(dm, "_recent_files", lambda limit=40: [(time.time() - 20, "file", "main.py")]):
        dm.install(H, v)
        for fn in H.event_listeners:
            fn("CODE: wrote demo")
            fn("FACE: recognized sam")                                 # not a recorded category
        assert dm.handle_command(v, "show my project timeline")
        eq(sorted(k for _, k, _ in H.modes.timeline.items), ["aurora", "file", "git"])
        assert H.modes.active == "timeline"
        assert dm.handle_command(v, "close timeline") and H.modes.active is None
        assert not dm.handle_command(v, "make a timeline of world war two"), "topic timelines must reach the AI"


def monitor(quiet=False):
    sm = mod("system_monitor")
    if sm.psutil is None:
        skip("psutil not installed")
    H = mock.MagicMock()
    H.quiet_alerts, H.mode = quiet, "empty"
    voice = mock.MagicMock()
    return sm, sm.SystemMonitor(H, voice), H, voice


def t_monitor_alert():
    sm, mon, H, voice = monitor()
    mon._alert("x", "Disk dropped", "why", 60)
    mon._alert("x", "again", "why", 60)                                # inside the cooldown
    eq(len(mon.alerts), 1)
    voice.speak_now.assert_called_once()
    H.quiet_alerts = True
    mon._alert("y", "Net down", "why", 60)
    eq((len(mon.alerts), voice.speak_now.call_count), (2, 1))          # silent while presenting


def t_monitor_disk():
    sm, mon, H, voice = monitor()
    usage = lambda free: SimpleNamespace(free=free, total=200 * sm.GB, percent=50)
    with mock.patch.object(sm.psutil, "disk_usage", side_effect=[usage(100 * sm.GB), usage(90 * sm.GB)]), \
            mock.patch.object(mon, "_snapshot_io", lambda now: None):
        mon._check_disk(1000.0)
        mon._check_disk(1005.0)
    assert [a["kind"] for a in mon.alerts] == ["disk_drop"], mon.alerts


def t_monitor_cpu():
    sm, mon, H, voice = monitor()
    with mock.patch.object(sm.psutil, "cpu_percent", return_value=97.0), \
            mock.patch.object(mon, "_top_cpu", lambda: ("MsMpEng.exe", 60.0)):
        for i in range(6):
            mon._check_cpu(float(i))
    assert mon.alerts and mon.alerts[-1]["kind"] == "cpu" and "Defender" in mon.alerts[-1]["explanation"]


def t_monitor_network():
    sm, mon, H, voice = monitor()
    states = iter([True, False, False, True])
    with mock.patch.object(sm.SystemMonitor, "_online", staticmethod(lambda: next(states))), \
            mock.patch.object(sm.SystemMonitor, "_net_explain", staticmethod(lambda: "adapter down")):
        for t in (100.0, 110.0, 120.0, 130.0):
            mon._check_network(t)
    assert [a["kind"] for a in mon.alerts] == ["network"]
    assert any("back" in c.args[0] for c in H.log_event.call_args_list), "recovery notice missing"


def t_monitor_crash():
    if os.name != "nt":
        skip("crash detection reads the Windows event log")
    sm, mon, H, voice = monitor()
    fake = lambda text: SimpleNamespace(stdout=text, returncode=0)
    ev = lambda date: (f"Event[0]\n  Date: {date}\n  Description:\nFaulting application name: notepad.exe, version: 1\n"
                       "Faulting module name: ntdll.dll, version 2\nException code: 0xc0000005\n")
    with mock.patch.object(sm.subprocess, "run", side_effect=[fake(ev("2026-10-01T10:00:00Z")), fake(ev("2026-10-01T10:00:00Z")),
                                                              fake(ev("2026-10-02T11:00:00Z"))]):
        for t in (1, 2, 3):
            mon._check_crashes(t)                                      # baseline, unchanged, new crash
    assert [a["kind"] for a in mon.alerts] == ["crash"]
    assert "notepad.exe" in mon.alerts[0]["summary"] and "access violation" in mon.alerts[0]["explanation"]


def t_monitor_voice():
    sm = mod("system_monitor")
    v, H, said = make_voice()
    assert not sm.handle_command(v, "what happened"), "'what happened' must reach the AI when there are no alerts"
    assert sm.handle_command(v, "any alerts") and "No alerts" in said[-1]
    assert sm.handle_command(v, "emergency monitor status") and said


def t_modes_hooked():
    dm = mod("dashboard_modes")
    hsrc = open(os.path.join(ROOT, "jarvis_ui", "hologram.py"), encoding="utf-8").read()
    for needle in ("self.overlays = []", "self.event_listeners = []", "fn(theme_color)", "fn(text)"):
        assert needle in hsrc, f"hologram.py is missing a hook ({needle}): run apply_dashboard_modes_patch.py"
    v, H, said = make_voice()
    H.modes = H.labs = None
    with mock.patch.object(dm, "collect", lambda since=None, limit=300: []):
        assert v._handle_local_command("show my project timeline"), "dashboard_modes is not hooked into voice_assistant"
    assert v._handle_local_command("emergency monitor status"), "system_monitor is not hooked into voice_assistant"


check("Dashboard modes", "slide file parsing", t_slide_parse)
check("Dashboard modes", "presentation voice flow (slides, notes, timer, blank, demo, exit)", t_presentation_flow)
check("Dashboard modes", "project timeline (event recording, sources, close)", t_timeline)
check("Dashboard modes", "hooks present in hologram.py and voice_assistant.py", t_modes_hooked)
check("System monitor", "alert cooldown + silent while presenting", t_monitor_alert)
check("System monitor", "sudden disk-space loss", t_monitor_disk)
check("System monitor", "unusual CPU usage + explanation", t_monitor_cpu)
check("System monitor", "network disconnect + recovery", t_monitor_network)
check("System monitor", "application crash from event log", t_monitor_crash)
check("System monitor", "voice commands", t_monitor_voice)


# ============================================================================ report
# ---- Screen understanding + file assistant (added by apply_screen_and_file_features.py)
def t_file_assistant():
    fu = util()
    home = os.path.join(newdir("fa"), "h")
    for d in ("Documents/School", "Downloads"):
        os.makedirs(os.path.join(home, d))
    dl = os.path.join(home, "Downloads")
    open(os.path.join(dl, "physics_notes.txt"), "w").write("newton physics")
    open(os.path.join(dl, "cooking.txt"), "w").write("pasta")
    v, said, pc = pv()
    with mock.patch.object(fu, "HOME", home):
        assert say(pc, v, "find documents about physics")
        eq([os.path.basename(p) for p in v._productivity.found], ["physics_notes.txt"])
        assert say(pc, v, "rename these files properly") and "yes" in said[-1].lower()
        assert os.path.exists(os.path.join(dl, "physics_notes.txt")), "renamed BEFORE confirmation"
        assert say(pc, v, "yes") and os.path.exists(os.path.join(dl, "Physics Notes.txt")), said[-1:]
        assert say(pc, v, "move these into my school folder") and "yes" in said[-1].lower()
        assert say(pc, v, "no") and os.path.exists(os.path.join(dl, "Physics Notes.txt")), "moved despite 'no'"
        assert say(pc, v, "move these into my school folder") and say(pc, v, "yes")
        assert os.path.exists(os.path.join(home, "Documents", "School", "Physics Notes.txt")), said[-1:]
        assert say(pc, v, "undo the move") and say(pc, v, "yes")
        assert os.path.exists(os.path.join(dl, "Physics Notes.txt")), "undo failed"
        assert not say(pc, v, "find files about budget"), "bare file search must reach the semantic search"
        assert not say(pc, v, "show me a picture of a cat"), "must not search files for picture requests"


def t_screen_understanding():
    sc, br = sc_mod(), mod("brain")
    v, H, said = make_voice()
    v.client = object()
    with mock.patch.object(sc, "capture_screen", return_value=object()), \
            mock.patch.object(br, "describe_screen", return_value="Missing colon on line 3.") as d:
        assert v._handle_local_command("why isn't this code working")
    assert any("colon" in s for s in said), said
    assert "why isn't" in d.call_args[0][2]
    v2, _, said2 = make_voice()
    assert v2._handle_local_command("what's on my screen") and any("Groq" in s for s in said2)
    assert not make_voice()[0]._handle_local_command("why is the sky blue"), "general questions must reach the AI"
    assert "describe_screen" in {t["function"]["name"] for t in br.TOOLS}


check("File assistant", "find, rename, move, undo (confirm first, 'no' cancels)", t_file_assistant)
check("Screen understanding", "routes to vision, needs API, ignores unrelated questions", t_screen_understanding)


def report():
    shutil.rmtree(tmpdir, ignore_errors=True)
    groups = {}
    for g, n, s, d in RESULTS:
        groups.setdefault(g, []).append((n, s, d))
    icon = {"PASS": "[ OK ]", "FAIL": "[FAIL]", "SKIP": "[skip]", "REQUIRES API": "[ API ]",
            "REQUIRES PERMISSION": "[PERM]", "REQUIRES HARDWARE": "[ HW ]"}
    print("\n" + "=" * 78 + "\n AURORA SELF-TEST\n" + "=" * 78)
    for g, rows in groups.items():
        p = sum(1 for r in rows if r[1] == "PASS")
        f = sum(1 for r in rows if r[1] == "FAIL")
        state = "BROKEN" if f else ("WORKING" if p else "not tested")
        print(f"\n{g}  -  {state}  ({p}/{len(rows)} passed)")
        for n, s, d in rows:
            print(f"  {icon[s]} {n}" + (f"  -> {d}" if d else ""))
    counts = {k: sum(1 for r in RESULTS if r[2] == k) for k in icon}
    print("\n" + "=" * 78)
    print(f" {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped, {counts['REQUIRES API']} require API, "
          f"{counts['REQUIRES PERMISSION']} require permission, {counts['REQUIRES HARDWARE']} require hardware, of {len(RESULTS)}")
    broken = [g for g, rows in groups.items() if any(r[1] == "FAIL" for r in rows)]
    print(" WORKING: " + ", ".join(g for g in groups if g not in broken))
    if broken:
        print(" BROKEN : " + ", ".join(broken))
    print("=" * 78)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    rc = report()
    sys.stdout.flush()
    os._exit(rc)
