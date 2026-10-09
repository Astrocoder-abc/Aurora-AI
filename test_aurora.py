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
          "addons", "aurora_plus", "sandbox_labs", "dashboard_modes", "system_monitor", "gmail_client"]:
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
def mail_env():
    cal = util()
    d = newdir("mail")
    with open(os.path.join(d, "email_config.json"), "w") as f:
        json.dump({"contacts": {"ann": "ann@example.com"}}, f)
    return cal, d


def no_gmail(d):
    """Gmail not connected: points gmail_client at a missing token file (your real token is never touched)."""
    return mock.patch.object(mod("gmail_client"), "TOKEN_FILE", os.path.join(d, "no_token.json"))


def mail_token(d, scopes=("read", "send")):
    g = mod("gmail_client")
    path = os.path.join(d, "gmail_token.json")
    with open(path, "w") as f:
        json.dump({"access_token": "at", "expires_at": time.time() + 3600, "refresh_token": "r",
                   "scopes": [g.SCOPES[s] for s in scopes]}, f)
    return mock.patch.object(g, "TOKEN_FILE", path)


def t_email_logic():
    cal, d = mail_env()
    with mock.patch.object(cal, "BASE", d), no_gmail(d):
        assert not cal.email_configured()
        eq(cal.resolve_recipient("ann"), "ann@example.com")
        eq(cal.resolve_recipient("bob at example dot com"), "bob@example.com")
        eq(cal.resolve_recipient("nobody"), None)
        draft = cal.save_draft("ann@example.com", "Hi", "Body")
        eq(cal.latest_draft()["to"], "ann@example.com")
        try:
            cal.send_draft(draft)
            raise AssertionError("send without confirmation was allowed")
        except PermissionError:
            pass
        for call in (lambda: cal.fetch_unread(), lambda: cal.send_draft(draft, confirmed=True)):
            try:
                call()
                raise AssertionError("mail worked without a Gmail connection")
            except cal.MailError:
                pass
        assert cal.discard_draft() and cal.latest_draft() is None


def t_no_password_mail():
    src = open(util().__file__, encoding="utf-8").read()
    for word in ("imaplib", "smtplib", "AURORA_EMAIL_PASSWORD", "email_password"):
        assert word not in src, f"password-based mail code still present: {word}"


check("Email", "recipients, drafts, send needs confirmed=True, clear error when Gmail isn't connected", t_email_logic)
check("Email", "no password-based (IMAP/SMTP) code remains", t_no_password_mail)


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
    g = mod("gmail_client")
    v, said, pc = pv()
    sent = []
    with mock.patch.object(cal, "BASE", d), mail_token(d), \
            mock.patch.object(g, "_api", lambda p, body=None: sent.append(p) or {}), with_complete(None):
        v.client = FakeClient("Subject: Demo\n\nSee you Friday.")
        assert say(pc, v, "draft an email to ann about the demo on friday")
        assert cal.latest_draft()["to"] == "ann@example.com"
        assert not sent
        assert say(pc, v, "send the email") and "about to send" in said[-1]
        assert not sent, "sent before confirmation"
        assert say(pc, v, "no")
        assert not sent, "sent despite 'no'"
        say(pc, v, "send the email")
        say(pc, v, "yes")
        eq(sent, ["messages/send"])


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


# ---- Gmail OAuth (added by install_gmail_oauth.py)
import base64
import urllib.parse


def gm():
    return mod("gmail_client")


def gm_token(d, scopes=("read",), expires=3600, refresh="r"):
    """Patches gmail_client.TOKEN_FILE to a temp file holding a token with the given permissions."""
    g = gm()
    path = os.path.join(d, "gmail_token.json")
    with open(path, "w") as f:
        json.dump({"access_token": "at", "expires_at": time.time() + expires, "refresh_token": refresh,
                   "scopes": [g.SCOPES[s] for s in scopes]}, f)
    return mock.patch.object(g, "TOKEN_FILE", path)


def t_gmail_permissions():
    g, d = gm(), newdir("gm")
    with mock.patch.object(g, "TOKEN_FILE", os.path.join(d, "none.json")):
        assert not g.connected() and not g.has("read") and not g.has("send")
    with gm_token(d, ("read",)):
        assert g.connected() and g.has("read") and not g.has("send")
        try:
            g.send({"to": "a@b.c", "body": "x"}, True)
            raise AssertionError("send allowed without the send permission")
        except g.GmailError:
            pass
    with gm_token(d, ("send",)):
        try:
            g.fetch_unread()
            raise AssertionError("read allowed without the read permission")
        except g.GmailError:
            pass
    with gm_token(d, ("read", "send")):
        try:
            g.send({"to": "a@b.c", "body": "x"})
            raise AssertionError("send without confirmation was allowed")
        except PermissionError:
            pass


def t_gmail_fetch():
    g, d = gm(), newdir("gm")
    data = base64.urlsafe_b64encode(b"The demo moved to Friday.\nPlease confirm.").decode().rstrip("=")
    msg = {"payload": {"headers": [{"name": "From", "value": "Ann Lee <ann@example.com>"},
                                   {"name": "Subject", "value": "Project update"}, {"name": "Date", "value": "Mon"}],
                       "mimeType": "multipart/alternative",
                       "parts": [{"mimeType": "text/html", "body": {"data": "PGI+"}},
                                 {"mimeType": "text/plain", "body": {"data": data}}]}}
    calls = []

    def fake(path, body=None):
        calls.append((path, body))
        return {"messages": [{"id": "1"}]} if path.startswith("messages?") else msg

    with gm_token(d), mock.patch.object(g, "_api", fake):
        out = g.fetch_unread(3)
    eq((out[0]["from"], out[0]["address"], out[0]["subject"]), ("Ann Lee", "ann@example.com", "Project update"))
    assert "Friday" in out[0]["body"], out[0]["body"]
    assert "is%3Aunread" in calls[0][0] and all(c[1] is None for c in calls), "reading must be read-only"


def t_gmail_send():
    g, d = gm(), newdir("gm")
    sent = []
    with gm_token(d, ("send",)), mock.patch.object(g, "_api", lambda p, body=None: sent.append((p, body)) or {}):
        assert g.send({"to": "ann@example.com", "subject": "Hi", "body": "See you Friday."}, True)
    eq(sent[0][0], "messages/send")
    raw = base64.urlsafe_b64decode(sent[0][1]["raw"]).decode()
    assert "To: ann@example.com" in raw and "See you Friday." in raw, raw


def t_gmail_refresh():
    g, d = gm(), newdir("gm")
    fake_req = lambda url, data=None, headers=None, as_json=False: {"access_token": "new", "expires_in": 3600}
    with gm_token(d, expires=-100):
        with mock.patch.object(g, "_client", lambda: ("cid", "sec")), mock.patch.object(g, "_request", fake_req):
            eq(g._access_token(), "new")
        with open(g.TOKEN_FILE) as f:
            eq(json.load(f)["access_token"], "new")
    with gm_token(d, expires=-100, refresh=None):
        try:
            g._access_token()
            raise AssertionError("expired token without refresh token must ask to reconnect")
        except g.GmailError:
            pass


def t_gmail_signin():
    g, d = gm(), newdir("gm")
    seen = {}

    def run(state_ok):
        def fake_open(url):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            seen.clear()
            seen.update(q)
            st = q["state"] if state_ok else "forged"

            def callback():
                time.sleep(0.2)
                urllib.request.urlopen(f"{q['redirect_uri']}/?code=abc&state={st}", timeout=5).read()
            threading.Thread(target=callback, daemon=True).start()
            return True

        exchange = lambda url, data=None, headers=None, as_json=False: {
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": g.SCOPES["read"]}
        with mock.patch.object(g, "TOKEN_FILE", os.path.join(d, f"t{state_ok}.json")), \
                mock.patch.object(g, "_client", lambda: ("cid", "sec")), \
                mock.patch.object(g.webbrowser, "open", fake_open), mock.patch.object(g, "_request", exchange):
            g.authorize(["read"], timeout=10)
            return g._load_token()

    tok = run(True)
    eq((tok["refresh_token"], tok["scopes"]), ("rt", [g.SCOPES["read"]]))
    assert seen["code_challenge_method"] == "S256" and g.SCOPES["read"] in seen["scope"]
    assert seen["redirect_uri"].startswith("http://127.0.0.1:"), seen["redirect_uri"]
    try:
        run(False)
        raise AssertionError("forged state must be rejected")
    except g.GmailError:
        pass


def t_gmail_disconnect():
    g, d = gm(), newdir("gm")
    with gm_token(d), mock.patch.object(g, "_request", lambda *a, **k: {}):
        assert g.disconnect() and not os.path.exists(g.TOKEN_FILE)
        assert not g.disconnect()


def t_gmail_routing():
    g, cal, d = gm(), util(), newdir("gm")
    v, said, pc = pv()
    calls = []
    with mock.patch.object(g, "connect_async", lambda perms, done: calls.append(perms) or done(None)):
        assert say(pc, v, "connect gmail") and calls[-1] == ["read"]
        assert "connected" in said[-1].lower(), said[-1:]
        assert say(pc, v, "connect gmail with sending") and calls[-1] == ["read", "send"]
    with mock.patch.object(g, "disconnect", lambda: True):
        assert say(pc, v, "disconnect gmail") and "disconnected" in said[-1].lower()
    sent = []
    with gm_token(d, ("read", "send")), mock.patch.object(cal, "BASE", newdir("gmm")), \
            mock.patch.object(g, "_api", lambda p, body=None: sent.append(p) or {}):
        cal.save_draft("ann@example.com", "Hi", "Body")
        assert say(pc, v, "send the email") and "about to send" in said[-1], said[-1:]
        assert not sent, "sent before confirmation"
        assert say(pc, v, "no") and not sent, "sent despite 'no'"
        say(pc, v, "send the email")
        say(pc, v, "yes")
        eq(sent, ["messages/send"])
        assert cal.latest_draft() is None


check("Gmail OAuth", "permissions: read/send are separate, send needs confirmed=True", t_gmail_permissions)
check("Gmail OAuth", "read unread mail (mocked API, read-only)", t_gmail_fetch)
check("Gmail OAuth", "send builds a valid message (mocked API)", t_gmail_send)
check("Gmail OAuth", "token refresh and reconnect prompt", t_gmail_refresh)
check("Gmail OAuth", "sign-in flow: PKCE, state check, token saved (no password)", t_gmail_signin)
check("Gmail OAuth", "disconnect revokes and deletes token", t_gmail_disconnect)
check("Gmail OAuth", "voice: connect/disconnect, send needs 'yes'", t_gmail_routing)
check("Gmail OAuth", "real Gmail read", lambda: requires("PERMISSION", "say 'Aurora, connect gmail' first")
      if not gm().has("read") else (gm().fetch_unread(1) and None))


# ---- Gmail OAuth (added by install_gmail_oauth.py)
import base64
import urllib.parse


def gm():
    return mod("gmail_client")


def gm_token(d, scopes=("read",), expires=3600, refresh="r"):
    """Patches gmail_client.TOKEN_FILE to a temp file holding a token with the given permissions."""
    g = gm()
    path = os.path.join(d, "gmail_token.json")
    with open(path, "w") as f:
        json.dump({"access_token": "at", "expires_at": time.time() + expires, "refresh_token": refresh,
                   "scopes": [g.SCOPES[s] for s in scopes]}, f)
    return mock.patch.object(g, "TOKEN_FILE", path)


def t_gmail_permissions():
    g, d = gm(), newdir("gm")
    with mock.patch.object(g, "TOKEN_FILE", os.path.join(d, "none.json")):
        assert not g.connected() and not g.has("read") and not g.has("send")
    with gm_token(d, ("read",)):
        assert g.connected() and g.has("read") and not g.has("send")
        try:
            g.send({"to": "a@b.c", "body": "x"}, True)
            raise AssertionError("send allowed without the send permission")
        except g.GmailError:
            pass
    with gm_token(d, ("send",)):
        try:
            g.fetch_unread()
            raise AssertionError("read allowed without the read permission")
        except g.GmailError:
            pass
    with gm_token(d, ("read", "send")):
        try:
            g.send({"to": "a@b.c", "body": "x"})
            raise AssertionError("send without confirmation was allowed")
        except PermissionError:
            pass


def t_gmail_fetch():
    g, d = gm(), newdir("gm")
    data = base64.urlsafe_b64encode(b"The demo moved to Friday.\nPlease confirm.").decode().rstrip("=")
    msg = {"payload": {"headers": [{"name": "From", "value": "Ann Lee <ann@example.com>"},
                                   {"name": "Subject", "value": "Project update"}, {"name": "Date", "value": "Mon"}],
                       "mimeType": "multipart/alternative",
                       "parts": [{"mimeType": "text/html", "body": {"data": "PGI+"}},
                                 {"mimeType": "text/plain", "body": {"data": data}}]}}
    calls = []

    def fake(path, body=None):
        calls.append((path, body))
        return {"messages": [{"id": "1"}]} if path.startswith("messages?") else msg

    with gm_token(d), mock.patch.object(g, "_api", fake):
        out = g.fetch_unread(3)
    eq((out[0]["from"], out[0]["address"], out[0]["subject"]), ("Ann Lee", "ann@example.com", "Project update"))
    assert "Friday" in out[0]["body"], out[0]["body"]
    assert "is%3Aunread" in calls[0][0] and all(c[1] is None for c in calls), "reading must be read-only"


def t_gmail_send():
    g, d = gm(), newdir("gm")
    sent = []
    with gm_token(d, ("send",)), mock.patch.object(g, "_api", lambda p, body=None: sent.append((p, body)) or {}):
        assert g.send({"to": "ann@example.com", "subject": "Hi", "body": "See you Friday."}, True)
    eq(sent[0][0], "messages/send")
    raw = base64.urlsafe_b64decode(sent[0][1]["raw"]).decode()
    assert "To: ann@example.com" in raw and "See you Friday." in raw, raw


def t_gmail_refresh():
    g, d = gm(), newdir("gm")
    fake_req = lambda url, data=None, headers=None, as_json=False: {"access_token": "new", "expires_in": 3600}
    with gm_token(d, expires=-100):
        with mock.patch.object(g, "_client", lambda: ("cid", "sec")), mock.patch.object(g, "_request", fake_req):
            eq(g._access_token(), "new")
        with open(g.TOKEN_FILE) as f:
            eq(json.load(f)["access_token"], "new")
    with gm_token(d, expires=-100, refresh=None):
        try:
            g._access_token()
            raise AssertionError("expired token without refresh token must ask to reconnect")
        except g.GmailError:
            pass


def t_gmail_signin():
    g, d = gm(), newdir("gm")
    seen = {}

    def run(state_ok):
        def fake_open(url):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            seen.clear()
            seen.update(q)
            st = q["state"] if state_ok else "forged"

            def callback():
                time.sleep(0.2)
                urllib.request.urlopen(f"{q['redirect_uri']}/?code=abc&state={st}", timeout=5).read()
            threading.Thread(target=callback, daemon=True).start()
            return True

        exchange = lambda url, data=None, headers=None, as_json=False: {
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": g.SCOPES["read"]}
        with mock.patch.object(g, "TOKEN_FILE", os.path.join(d, f"t{state_ok}.json")), \
                mock.patch.object(g, "_client", lambda: ("cid", "sec")), \
                mock.patch.object(g.webbrowser, "open", fake_open), mock.patch.object(g, "_request", exchange):
            g.authorize(["read"], timeout=10)
            return g._load_token()

    tok = run(True)
    eq((tok["refresh_token"], tok["scopes"]), ("rt", [g.SCOPES["read"]]))
    assert seen["code_challenge_method"] == "S256" and g.SCOPES["read"] in seen["scope"]
    assert seen["redirect_uri"].startswith("http://127.0.0.1:"), seen["redirect_uri"]
    try:
        run(False)
        raise AssertionError("forged state must be rejected")
    except g.GmailError:
        pass


def t_gmail_disconnect():
    g, d = gm(), newdir("gm")
    with gm_token(d), mock.patch.object(g, "_request", lambda *a, **k: {}):
        assert g.disconnect() and not os.path.exists(g.TOKEN_FILE)
        assert not g.disconnect()


def t_gmail_routing():
    g, cal, d = gm(), util(), newdir("gm")
    v, said, pc = pv()
    calls = []
    with mock.patch.object(g, "connect_async", lambda perms, done: calls.append(perms) or done(None)):
        assert say(pc, v, "connect gmail") and calls[-1] == ["read"]
        assert "connected" in said[-1].lower(), said[-1:]
        assert say(pc, v, "connect gmail with sending") and calls[-1] == ["read", "send"]
    with mock.patch.object(g, "disconnect", lambda: True):
        assert say(pc, v, "disconnect gmail") and "disconnected" in said[-1].lower()


check("Gmail OAuth", "permissions: read/send are separate, send needs confirmed=True", t_gmail_permissions)
check("Gmail OAuth", "read unread mail (mocked API, read-only)", t_gmail_fetch)
check("Gmail OAuth", "send builds a valid message (mocked API)", t_gmail_send)
check("Gmail OAuth", "token refresh and reconnect prompt", t_gmail_refresh)
check("Gmail OAuth", "sign-in flow: PKCE, state check, token saved (no password)", t_gmail_signin)
check("Gmail OAuth", "disconnect revokes and deletes token", t_gmail_disconnect)
check("Gmail OAuth", "voice: connect/disconnect", t_gmail_routing)
check("Gmail OAuth", "real Gmail read", lambda: requires("PERMISSION", "say 'Aurora, connect gmail' first")
      if not gm().has("read") else (gm().fetch_unread(1) and None))


# ---- Feature modules: visual calculator, adaptive interface, project workspace, operation simulator
import contextlib


def _fdir():
    return tempfile.mkdtemp(dir=tmpdir)


def fake_holo():
    """Hologram stand-in: real ints/lists where the code does arithmetic or iteration, mocks elsewhere."""
    H = mock.MagicMock(name="Hologram")
    H.width, H.height, H.translate_x = 1280, 720, 0.0
    H.mode, H.info_card, H.quiet_alerts = "empty", None, False
    H.overlays, H.event_listeners, H.event_log = [], [], []
    H.modes = H.labs = H.adaptive_interface = H.workspace = None
    H._wrap_text.side_effect = lambda f, t, w: [t]
    H._truncate.side_effect = lambda t, n: t
    H._pill_width.side_effect = lambda t: 8 * len(t) + 28
    H._materialize_progress.return_value = 1.0
    return H


def stub_voice(H=None, **kw):
    said = []
    attrs = dict(hologram=H or fake_holo(), _speak=said.append, _log=lambda m: None, client=None, plus=None,
                 latest_frame="frame")
    attrs.update(kw)
    v = SimpleNamespace(**attrs)
    return v, said


THEME = (0.3, 0.6, 1.0)

# ============================================================================ visual calculator
G = "Visual calculator"


def vc():
    return mod("visual_calculator")


def t_vc_normalize():
    v = vc()
    for raw, want in [("2 × 3", "2*3"), ("3x + 2", "3*x+2"), ("4 x 5", "4*5"), ("12,500 + 1", "12500+1"),
                      ("x^2", "x**2"), ("2 + 2 =", "2+2"), ("2(3+4)", "2*(3+4)")]:
        eq(v.normalize(raw), want)


def t_vc_solve():
    v = vc()
    for problem, want in [("12 + 30", "The answer is 42."), ("10 / 4", "The answer is 2.5."),
                          ("2x + 4 = 10", "x equals 3."), ("x^2 - 5x + 6 = 0", "x equals 2 or x equals 3."),
                          ("x^2 + 1 = 0", "There is no real solution."), ("x + 1 = x + 2", "There is no real solution."),
                          ("x + 1 = x + 1", "Every number works."), ("2 + 2 = 4", "That's correct."), ("1 / 0", "That divides by zero.")]:
        eq(v.solve_text(problem), want)
    assert v.solve_text("2 + 2 = 5").startswith("That's wrong"), "wrong equation not flagged"
    assert v.solve_text("x^3 = 8") is None, "cubic should go to the AI"
    assert v.solve_text("hello") is None
    assert v.solve_text("9**9**9") is None, "huge exponent must be refused"


def t_vc_spoken():
    v = vc()
    eq(v.spoken("2+3"), "2 plus 3")
    eq(v.fmt(3.0), "3")
    eq(v.fmt(2.5), "2.5")


def vc_run(phrase, ocr=None, vision=None, vision_error=False, complete=None, client=None, frame="img"):
    v = vc()
    voice, said = stub_voice(client=None)
    voice.client, voice.latest_frame = client, frame
    br = mod("brain")
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(v, "ocr_math", return_value=ocr))
        st.enter_context(mock.patch.object(v.sc, "capture_screen", return_value="screen"))
        if vision_error:
            st.enter_context(mock.patch.object(br, "describe_scene", side_effect=RuntimeError("down")))
        else:
            st.enter_context(mock.patch.object(br, "describe_scene", return_value=vision))
        st.enter_context(mock.patch.object(br, "_complete", return_value=complete))
        handled = v.handle_command(voice, phrase)
    return handled, said, voice


def t_vc_offline_ocr():
    handled, said, voice = vc_run("solve this problem", ocr="2+3")
    assert handled
    eq(said[-1], "I read 2 plus 3. The answer is 5.")
    assert voice.hologram.show_info_card.called
    handled, said, _ = vc_run("solve the equation on my screen", ocr="2x+4=10")
    assert handled and "x equals 3" in said[-1], said
    handled, said, _ = vc_run("read this equation", ocr=None)
    assert handled and "couldn't read" in said[-1] and "pytesseract" in said[-1], said
    handled, said, _ = vc_run("solve this problem", frame=None)
    assert handled and "can't get an image" in said[-1]
    assert not vc_run("what time is it")[0], "unrelated phrase must not be handled"


def t_vc_online():
    handled, said, _ = vc_run("solve this problem", vision="x^2-4=0", client=object())
    assert handled and "x equals -2 or x equals 2" in said[-1], said
    handled, said, _ = vc_run("solve this problem", vision="x^3=8", complete="x equals 2.", client=object())
    assert handled and "x equals 2." in said[-1], "hard problem should go to the AI"
    handled, said, _ = vc_run("solve this problem", vision="NONE", client=object())
    assert handled and "couldn't read" in said[-1] and "pytesseract" not in said[-1], said
    handled, said, _ = vc_run("solve this problem", vision_error=True, ocr="7*6", client=object())
    assert handled and "The answer is 42." in said[-1], "must fall back to local OCR when the AI fails"


check(G, "normalize spoken/printed math", t_vc_normalize)
check(G, "local solver: arithmetic, linear, quadratic, edge cases, safety", t_vc_solve)
check(G, "spoken formatting", t_vc_spoken)
check(G, "voice flow offline (OCR), screen source, no frame, unrelated phrase", t_vc_offline_ocr)
check(G, "voice flow online (vision model, AI fallback, NONE, error -> OCR)", t_vc_online)

# ============================================================================ adaptive interface
G = "Adaptive interface"


def ai_env():
    ai = mod("adaptive_interface")
    H = fake_holo()
    gs = mock.MagicMock()
    gs.connected = False
    gs.latest = {"fps": 60, "cpu": 20.0, "ram": 40.0, "temp_c": 55.0, "session": "1m 2s", "foreground": "game.exe",
                 "recording": "no"}
    voice, said = stub_voice(H, game_session=gs, active_timers=[{"label": "study session", "ends_at": time.time() + 90}])
    return ai, H, gs, voice, said


def t_ai_switch():
    ai, H, gs, voice, said = ai_env()
    for phrase, mode, hidden in [("switch to coding mode", "coding", ("log",)),
                                 ("Switch Into The Programming Mode", "coding", ("log",)),
                                 ("switch to studying mode", "studying", ("system", "log", "readout")),
                                 ("switch over to the gaming mode", "gaming", ("system", "log", "readout")),
                                 ("switch to normal mode", "standard", ())]:
        assert ai.handle_command(voice, phrase), phrase
        eq(H.adaptive_interface.mode, mode)
        eq(H.hidden_panels, hidden)
        eq(said[-1], ai.REPLIES[mode])
    gs.start.assert_called_once()
    gs.stop.assert_called_once()                       # leaving gaming mode stops the reader it started
    eq(len(H.overlays), 1)                             # install() is idempotent
    assert not ai.handle_command(voice, "switch to chemistry mode")
    assert not ai.handle_command(voice, "switch to coding")


def t_ai_gather():
    ai, H, gs, voice, said = ai_env()
    ui = ai.install(H, voice)
    ui.mode, ui._busy = "coding", True
    run = lambda *a, **k: SimpleNamespace(stdout="main\n")
    with mock.patch.object(ai.subprocess, "run", run), \
            mock.patch.object(ai.dashboard_modes, "_recent_files", lambda n: [(time.time(), "file", "main.py")]):
        ui._gather()
    eq(ui._info["branch"], "main")
    eq(ui._info["files"][0][2], "main.py")
    assert ui._busy is False
    ui.mode, ui._busy = "studying", True
    au, sc = mod("aurora_utilities"), mod("system_control")
    with mock.patch.object(ai.subprocess, "run", run), mock.patch.object(au, "minutes_today", return_value=12), \
            mock.patch.object(au, "list_decks", return_value={"bio": 3}), mock.patch.object(sc, "read_notes", return_value=["n1"]):
        ui._gather()
    eq((ui._info["minutes"], ui._info["decks"], ui._info["notes"]), (12, {"bio": 3}, ["n1"]))


def t_ai_draw():
    ai, H, gs, voice, said = ai_env()
    ui = ai.install(H, voice)
    ui._refresh = lambda: None
    ui._info = {"branch": "main", "dirty": 2, "files": [(time.time(), "file", "a.py")], "minutes": 5,
                "decks": {"bio": 3}, "notes": ["n1"]}
    for mode in ("coding", "studying", "gaming"):
        ui.mode = mode
        ui.draw(THEME)
    assert H._draw_panel.called, "nothing was drawn"
    H._draw_panel.reset_mock()
    ui.mode = "standard"
    ui.draw(THEME)
    assert not H._draw_panel.called, "standard mode must not draw"
    ui.mode, H.modes = "coding", SimpleNamespace(active="presentation")
    ui._refresh = lambda: (_ for _ in ()).throw(AssertionError("drew during presentation"))
    ui.draw(THEME)


check(G, "voice: switching modes, hidden panels, game reader start/stop, rejects bad phrases", t_ai_switch)
check(G, "background data gathering (git, files, study stats)", t_ai_gather)
check(G, "each mode draws; standard and presentation draw nothing", t_ai_draw)

# ============================================================================ project workspace
G = "Project workspace"


@contextlib.contextmanager
def ws_env():
    pw, tel = mod("project_workspace"), mod("telemetry")
    root = _fdir()
    with mock.patch.object(pw, "WORKSPACES_DIR", root), mock.patch.object(tel, "EXPERIMENTS_DIR", tel.EXPERIMENTS_DIR):
        voice, said = stub_voice()
        try:
            yield SimpleNamespace(pw=pw, tel=tel, root=root, voice=voice, said=said,
                                  say=lambda p: pw.handle_command(voice, p))
        finally:
            mgr = getattr(voice.hologram, "workspace", None)
            if mgr is not None:
                mgr.close()                              # restores telemetry.EXPERIMENTS_DIR


def t_ws_data():
    pw = mod("project_workspace")
    eq(pw.slug("Mars Project!"), "mars_project")
    assert "temperature" in pw.words("what was the temperature in my last experiment")
    assert "what" not in pw.words("what was the temperature")
    d = _fdir()
    ws = pw.Workspace(os.path.join(d, "mars"), "Mars")
    for sub in ("files", "data", "experiments"):
        assert os.path.isdir(os.path.join(d, "mars", sub)), sub
    ws.add("tasks", text="order sensors", done=False)
    assert ws.complete_task(1) and not ws.complete_task(5)
    ws.add("notes", text="battery drains fast")
    assert pw.Workspace(os.path.join(d, "mars")).meta["notes"][0]["text"] == "battery drains fast", "not persisted"
    eq(ws.tab_lines("tasks"), ["1. [x] order sensors"])
    csv, txt = os.path.join(d, "run1.csv"), os.path.join(d, "thermal.txt")
    open(csv, "w").write("a,b\n1,2\n")
    open(txt, "w").write("thermal insulation results look good")
    assert os.path.dirname(ws.add_file(csv)) == ws.data_dir and os.path.dirname(ws.add_file(txt)) == ws.files_dir
    assert os.path.basename(ws.add_file(csv)) == "run1 (1).csv", "duplicate import overwrote the file"
    hits = ws.search("battery")
    assert hits and "battery" in hits[0][1], hits
    assert any("thermal" in t.lower() for _, t in ws.search("thermal insulation"))
    eq(ws.search("zzzz"), [])
    assert "couldn't find" in ws.ask("zzzz")


def t_ws_experiment_answer():
    pw = mod("project_workspace")
    ws = pw.Workspace(os.path.join(_fdir(), "p"), "P")
    log = os.path.join(ws.exp_dir, "exp1")
    os.makedirs(log)
    with open(os.path.join(log, "log.jsonl"), "w") as f:
        for temp in (20.0, 24.5):
            f.write(json.dumps({"type": "sensor", "data": {"temperature": temp}}) + "\n")
    ans = ws.ask("what was the temperature in my last experiment")
    assert "exp1" in ans and "24.5" in ans and "20" in ans and "2 readings" in ans, ans


def t_ws_voice():
    with ws_env() as e:
        say, said, pw = e.say, e.said, e.pw
        assert not say("tell me a joke") and not say("add note: x"), "must ignore non-workspace phrases when none is open"
        assert say("create a workspace for my Mars project") and "Mars project workspace ready" in said[-1]
        assert os.path.isdir(os.path.join(e.root, "mars", "files"))
        eq(e.tel.EXPERIMENTS_DIR, os.path.join(e.root, "mars", "experiments"))
        assert say("add note: battery drains fast") and said[-1] == "Note added."
        assert say("add task: order sensors") and said[-1] == "Task added."
        assert say("complete task 1") and said[-1] == "Task completed."
        assert say("complete task 9") and "don't have" in said[-1]
        assert say("add link: nasa dot gov") and e.voice.hologram.workspace.ws.meta["links"][0]["url"] == "https://nasa.gov"
        n = len(said)
        assert say("show tasks") and len(said) == n, "tab switch should be silent"
        eq(e.voice.hologram.workspace.tab, "tasks")
        assert say("search project for battery") and "battery" in said[-1], said[-1]
        # experiments recorded now land inside the project and are answerable
        rec = e.tel.ExperimentRecorder()
        assert rec.start("trial1")[0]
        rec.log_sensor({"temperature": 21.5})
        assert say("what was the temperature in my last experiment") and "21.5" in said[-1], said[-1]
        assert say("list my workspaces") and "mars" in said[-1]
        assert say("open my Venus workspace") and "don't have a workspace" in said[-1]
        assert say("open my Mars workspace") and "Opened" in said[-1]
        e.voice.hologram.workspace.draw(THEME)
        assert e.voice.hologram._draw_panel.called, "overlay did not draw"
        assert say("close workspace") and said[-1] == "Workspace closed."
        assert e.tel.EXPERIMENTS_DIR != os.path.join(e.root, "mars", "experiments"), "experiments dir not restored"
        assert not say("close workspace") or "No workspace" in said[-1]


check(G, "workspace data: folders, persistence, tasks, file import, search", t_ws_data)
check(G, "'last experiment' sensor answer", t_ws_experiment_answer)
check(G, "voice flow end to end (create, edit, ask, open, close, overlay draw)", t_ws_voice)

# ============================================================================ operation simulator
G = "Operation simulator"


@contextlib.contextmanager
def sim_env(files=()):
    au, sc, sm = mod("aurora_utilities"), mod("system_control"), mod("operation_simulator")
    home = os.path.join(_fdir(), "h")
    dl = os.path.join(home, "Downloads")
    os.makedirs(dl)
    os.makedirs(os.path.join(home, "Documents", "School"))
    for f in files:
        open(os.path.join(dl, f), "w").write("x")
    with mock.patch.object(au, "HOME", home), mock.patch.object(au, "BASE", _fdir()), \
            mock.patch.object(sc, "NOTES_FILE", os.path.join(_fdir(), "notes.txt")):
        said, ran = [], []
        voice = SimpleNamespace(_speak=said.append, _handle_local_command=lambda op: ran.append(op) or True,
                                hologram=mock.MagicMock(), client=None, plus=None, _productivity=SimpleNamespace(found=[]))

        def run(phrase):
            handled = sm.handle_command(voice, phrase)
            return handled, (said[-1] if said else "")
        yield SimpleNamespace(au=au, sc=sc, sm=sm, home=home, dl=dl, voice=voice, said=said, ran=ran, run=run)


def expect(e, phrase, *frags):
    handled, msg = e.run(phrase)
    assert handled, f"'{phrase}' not handled"
    for f in frags:
        assert f.lower() in msg.lower(), f"{phrase!r} -> {msg!r} lacks {f!r}"
    return msg


def t_sim_organize_and_go():
    files = ["report.pdf", "pic.png", "song.mp3"]
    with sim_env(files) as e:
        msg = expect(e, "simulate organizing my downloads", "3 loose files", "go ahead")
        eq(sorted(os.listdir(e.dl)), sorted(files))                 # nothing moved
        assert e.voice.hologram.show_info_card.called
        assert e.run("go ahead")[0]
        eq(e.ran, ["organize my downloads"])
        assert not e.run("go ahead")[0], "go ahead must only work once"
    with sim_env() as e:
        expect(e, "preview organizing my downloads", "nothing to organize")
    with sim_env(["a.pdf"]) as e:
        e.run("simulate organizing my downloads")
        e.voice._simulator.last = (e.voice._simulator.last[0], time.time() - 1000)
        expect(e, "go ahead", "expired")
        eq(e.ran, [])


def t_sim_files():
    with sim_env(["report.pdf", "summary.pdf"]) as e:
        expect(e, "what would happen if I rename report.pdf in downloads to notes.pdf", "renamed to notes.pdf", "Nothing is overwritten")
        expect(e, "simulate renaming report.pdf in downloads to summary.pdf", "summary (1).pdf", "taken")
        expect(e, "simulate renaming report.pdf in downloads to a/b", "isn't a valid file name")
        expect(e, "simulate moving report.pdf in downloads to documents", "would move to Documents")
        assert not e.run("simulate renaming nothing.pdf in downloads to x.pdf")[0], "unknown file should fall through"
        eq(sorted(os.listdir(e.dl)), ["report.pdf", "summary.pdf"])   # still untouched
        open(os.path.join(e.home, "Documents", "todo.txt"), "w").write("x")
        expect(e, "simulate zipping todo.txt in documents", "todo.txt.zip would be created")
        assert not os.path.exists(os.path.join(e.home, "Documents", "todo.txt.zip"))
    with sim_env() as e:
        with zipfile.ZipFile(os.path.join(e.dl, "archive.zip"), "w") as z:
            z.writestr("a.txt", "1")
            z.writestr("b.txt", "2")
        with zipfile.ZipFile(os.path.join(e.dl, "evil.zip"), "w") as z:
            z.writestr("../evil.txt", "x")
        expect(e, "simulate extracting archive.zip in downloads", "folder called archive", "2 entries")
        expect(e, "simulate extracting evil.zip in downloads", "unsafe paths")
        assert not os.path.exists(os.path.join(e.dl, "archive"))


def t_sim_batches():
    with sim_env(["physics_notes.txt", "b.txt"]) as e:
        expect(e, "simulate moving these into my school folder", "tell me which files first")
        expect(e, "simulate renaming these files properly", "tell me which files first")
        e.voice._productivity.found = [os.path.join(e.dl, "physics_notes.txt"), os.path.join(e.dl, "b.txt")]
        expect(e, "simulate moving these into my school folder", "2 files would move into School")
        expect(e, "simulate moving these into my chemistry folder", "no chemistry folder", "new one in Documents")
        expect(e, "simulate renaming these files properly", "physics_notes.txt to Physics Notes.txt")
        assert os.path.exists(os.path.join(e.dl, "physics_notes.txt")), "files changed during simulation"


def t_sim_data():
    with sim_env() as e:
        expect(e, "simulate deleting all notes", "no notes")
        e.au.add_quick_note("first note")
        e.au.add_quick_note("second note")
        expect(e, "simulate deleting all notes", "All 2 notes", "no undo")
        expect(e, "simulate deleting note 1", "Note 1 would be deleted", "first note")
        eq(len(e.au.list_notes()), 2)                                   # nothing was deleted
        e.au.teach_skill("warmup", ["a", "b"])
        expect(e, "simulate deleting skill warmup", "warmup", "2 steps")
        expect(e, "simulate deleting skill nope", "don't have a skill")
        assert e.au.get_skill("warmup")
        e.au.add_card("bio", "q", "a")
        expect(e, "simulate deleting deck bio", "bio", "1 flashcards")
        expect(e, "simulate deleting deck nope", "don't have that deck")
        assert e.au.list_decks() == {"bio": 1}


def t_sim_system():
    with sim_env() as e:
        with mock.patch.object(e.sc, "get_volume_percent", return_value=50):
            expect(e, "simulate volume to 90", "from 50 to 90", "loud")
            expect(e, "simulate volume up", "to 60")
            expect(e, "simulate muting", "to 0")
        with mock.patch.object(e.sc, "get_volume_percent", return_value=None):
            expect(e, "simulate volume up", "isn't available")
        expect(e, "simulate locking my computer", "lock")
        for result, frag in (((False, ""), "can't see your phone"), ((True, "abc\tdevice"), "USB connection keeps working"),
                             ((True, "192.168.1.5:5555\tdevice"), "lose the connection")):
            with mock.patch.object(e.sc, "is_device_connected", return_value=result):
                expect(e, "simulate turning off wifi on my phone", frag)
        with mock.patch.object(e.au, "latest_draft", return_value=None):
            expect(e, "simulate sending the email", "no draft")
        with mock.patch.object(e.au, "latest_draft", return_value={"to": "a@b.c", "subject": "Hi", "body": "see you friday"}), \
                mock.patch.object(e.au, "email_configured", return_value=False):
            expect(e, "simulate sending the email", "a@b.c", "isn't set up")


def t_sim_edit_code():
    with sim_env() as e:
        path = os.path.join(e.home, "sorter.py")
        open(path, "w").write("a\n")
        br = mod("brain")
        with mock.patch.object(e.sc, "resolve_project_path", return_value=path):
            expect(e, "simulate editing sorter to add a reverse option", "AI online")
            e.voice.client = object()
            with mock.patch.object(br, "edit_code", return_value="a\nb\n"):
                expect(e, "simulate editing sorter to add a reverse option", "1 line added", ".bak")
            eq(open(path).read(), "a\n")                                 # file untouched
        with mock.patch.object(e.sc, "resolve_project_path", return_value=None), \
                mock.patch.object(e.sc, "find_arduino_sketch", return_value=None):
            assert not e.run("simulate editing ghost to do x")[0]


def t_sim_passthrough():
    with sim_env(["a.pdf"]) as e:
        assert not e.run("organize my downloads")[0], "real commands must not be intercepted"
        assert not e.run("simulate world peace")[0], "unknown simulations must fall through"
        e.run("simulate organizing my downloads")
        assert not e.run("what time is it")[0]
        assert e.voice._simulator.last is None, "unrelated command should clear the pending simulation"


check(G, "organize preview, 'go ahead' runs once, expiry, nothing touched", t_sim_organize_and_go)
check(G, "rename / move / zip / extract previews (clashes, invalid, unsafe zip)", t_sim_files)
check(G, "batch move / rename of found files", t_sim_batches)
check(G, "notes, skills, decks previews", t_sim_data)
check(G, "volume, lock, phone wifi, email previews", t_sim_system)
check(G, "code-edit preview (needs AI, file untouched)", t_sim_edit_code)
check(G, "unrelated and real commands fall through", t_sim_passthrough)


# ---- Document generator (added by apply_document_generator_patch.py)
G = "Document generator"


def dgm():
    return mod("document_generator")


def t_dg_parse():
    d = dgm()
    for word, kind in [("presentation", "pptx"), ("slide deck", "pptx"), ("powerpoint", "pptx"), ("report", "docx"),
                       ("word doc", "docx"), ("spreadsheet", "xlsx"), ("excel sheet", "xlsx")]:
        eq(d._kind(word), kind)
    eq(d._REQ.search("make a 5 slide presentation about black holes").groups(), ("5", "presentation", "black holes"))
    assert not d._REQ.search("start presentation called robotics"), "must not steal presentation mode"
    assert not d._REQ.search("generate my experiment report"), "must not steal the experiment report"


def t_dg_offline_build():
    d = dgm()
    with mock.patch.object(d, "OUT_DIR", _fdir()):
        for kind in ("pptx", "docx", "xlsx"):
            path, n, ai, err = d.create(kind, "Black Holes!", None, 3)
            if err and err.startswith("Missing"):
                skip(err)
            assert not err and not ai, err
            assert path.endswith(d.EXT[kind]) and os.path.getsize(path) > 1000, path
    br = mod("brain")
    with mock.patch.object(br, "_complete", return_value="not json"), mock.patch.object(d, "OUT_DIR", _fdir()):
        path, n, ai, err = d.create("docx", "x", object())
    if err and err.startswith("Missing"):
        skip(err)
    assert not err and not ai, "bad AI output must fall back to the skeleton"


def t_dg_ai_content():
    d, br = dgm(), mod("brain")
    pptx = '{"title":"T","slides":[{"title":"A","bullets":["x","y"],"notes":"n"},{"title":"B","bullets":[],"notes":""}]}'
    xlsx = '{"title":"Budget","headers":["Item","Cost"],"rows":[["Rent",800],["Food",300],["Total","=SUM(B2:B3)"]]}'
    docx = '{"title":"Doc","sections":[{"heading":"Intro","paragraphs":["Hello world."],"bullets":["pt"]}]}'
    with mock.patch.object(d, "OUT_DIR", _fdir()):
        with mock.patch.object(br, "_complete", return_value="```json\n" + pptx + "\n```"):
            path, n, ai, err = d.create("pptx", "t", object(), 2)
        if err and err.startswith("Missing"):
            skip(err)
        assert ai and n == 3, (n, ai, err)
        from pptx import Presentation
        prs = Presentation(path)
        eq(len(prs.slides), 3)
        eq(prs.slides[1].shapes.title.text, "A")
        eq(prs.slides[1].notes_slide.notes_text_frame.text, "n")
        with mock.patch.object(br, "_complete", return_value=xlsx):
            path, n, ai, err = d.create("xlsx", "budget", object())
        from openpyxl import load_workbook
        ws = load_workbook(path).active
        eq((ws["A1"].value, ws["B2"].value, ws["B4"].value), ("Item", 800, "=SUM(B2:B3)"))
        assert ws["A1"].font.bold, "header not bold"
        with mock.patch.object(br, "_complete", return_value=docx):
            path, n, ai, err = d.create("docx", "doc", object())
        from docx import Document
        text = " ".join(p.text for p in Document(path).paragraphs)
        assert "Intro" in text and "Hello world." in text and "pt" in text, text


def t_dg_voice():
    d = dgm()
    voice, said = stub_voice()
    with mock.patch.object(d, "OUT_DIR", _fdir()):
        assert d.handle_command(voice, "create a 2 row spreadsheet for expenses")
    if "Missing" in said[-1]:
        skip(said[-1])
    assert "spreadsheet is ready with 2 rows" in said[-1] and "offline" in said[-1], said
    assert voice.hologram.show_info_card.called
    assert not d.handle_command(voice, "start presentation called robotics")
    assert not d.handle_command(voice, "what time is it")


def t_dg_hooked():
    src = open(os.path.join(ROOT, "jarvis_ui", "voice_assistant.py"), encoding="utf-8").read()
    assert "document_generator.handle_command" in src, "run apply_document_generator_patch.py"
    assert "create_document" in {t["function"]["name"] for t in mod("brain").TOOLS}, "AI tool missing"
    v, H, said = make_voice()
    with mock.patch.object(dgm(), "OUT_DIR", _fdir()):
        assert v._handle_local_command("make a presentation about mars"), "not hooked into _handle_local_command"
        assert "presentation" in v._exec_tool("create_document", {"kind": "presentation", "topic": "mars"}).lower()


check(G, "request parsing (and no clash with presentation mode)", t_dg_parse)
check(G, "builds pptx/docx/xlsx offline; bad AI output falls back", t_dg_offline_build)
check(G, "AI content -> real slides, notes, formulas, headings (mocked model)", t_dg_ai_content)
check(G, "voice flow: reply, info card, unrelated phrases fall through", t_dg_voice)
check(G, "hooked into voice_assistant and the AI tools", t_dg_hooked)


def t_dg_live():
    d = dgm()
    with mock.patch.object(d, "OUT_DIR", _fdir()):
        path, n, ai, err = d.create("pptx", "the water cycle", get_client(), 3)
    if err and err.startswith("Missing"):
        skip(err)
    assert ai and n >= 3, (n, ai, err)


live("Document generator (real Groq)", t_dg_live)


def t_debug_loop():
    ad, br = mod("addons"), mod("brain")
    with mock.patch.object(ad, "SANDBOX_ROOT", os.path.join(tmpdir, "dbg")), \
            mock.patch.object(br, "fix_code", lambda c, code, err, goal="", lang="python": "print(42)\n"):
        code, ok, out, err, fixes = ad.debug_loop(object(), "print(1/0)\n", "demo", "dbg_demo")
    assert ok and fixes == 1 and out.strip() == "42", (ok, fixes, err)


check("Code sandbox", "debug loop fixes a failing script (mocked model)", t_debug_loop)


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
