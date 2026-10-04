"""
Simulation Room: preview what a disruptive operation WOULD do, without touching anything.

  "Aurora, simulate organizing my downloads"        "what would happen if I rename report.pdf in downloads to summary.pdf"
  "preview moving these into my school folder"      "dry run zipping todo.txt in documents"
  "simulate extracting archive.zip"                 "simulate deleting all notes" / "deleting skill warmup" / "deleting deck bio"
  "simulate volume to 90" / "locking my computer" / "turning off wifi on my phone" / "sending the email"
  "simulate editing sorter to add a reverse option"  (needs the AI online)

Afterwards say "go ahead" to run the real command; it still goes through the normal "say yes to confirm" step.
Everything except the code-edit preview works offline. Phrases it can't simulate fall through untouched.
"""
import os
import re
import time
import zipfile
from collections import Counter

from jarvis_ui import aurora_utilities as au, system_control as sc

GERUNDS = {"organizing": "organize", "organising": "organise", "renaming": "rename", "moving": "move", "zipping": "zip",
           "compressing": "compress", "extracting": "extract", "unzipping": "unzip", "deleting": "delete", "clearing": "clear",
           "erasing": "erase", "locking": "lock", "muting": "mute", "sending": "send", "editing": "edit", "turning": "turn",
           "cleaning": "clean", "tidying": "tidy"}
_PREFIX = re.compile(r"(?:simulate|preview|dry[- ]run|test[- ]run|what (?:would|will) happen if i|what happens if i)\s+(.+)$", re.I)
_GO = re.compile(r"(?:go ahead|do it for real|run it for real)$", re.I)
GO_TTL = 120
_FROM = r"(?: (?:in|from|on) (?:my |the )?(.+?))?"


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs if os.path.exists(os.path.join(r, f)))


def _mb(n):
    return f"{n / 1024 ** 2:.1f} MB" if n >= 1024 ** 2 else f"{max(1, n // 1024)} KB"


class Simulator:
    def __init__(self, voice):
        self.v, self.last = voice, None

    def _source(self, name, folder_word):
        folder = au.find_folder(folder_word) if folder_word else None
        if folder_word and not folder:
            return None, f"I don't know a folder called {folder_word}."
        hits = au.find_file(au._clean_name(name), folder)
        if not hits:
            return None, None
        if len(hits) > 1:
            return None, f"{name} exists in {len(hits)} places, so the real command would ask which folder."
        if not au.inside_home(hits[0]):
            return None, "I only touch files inside your user folder, so this would be refused."
        return hits[0], None

    # ---- file operations -------------------------------------------------------
    def organize(self, m):
        folder = au.find_folder(m.group(1))
        if not folder:
            return None
        plan = au.plan_organize(folder)
        if not plan:
            return f"There's nothing to organize in {os.path.basename(folder) or 'home'}."
        cats = Counter(c for _, c in plan)
        clash = sum(os.path.exists(os.path.join(folder, c, os.path.basename(s))) for s, c in plan)
        return (f"Organizing {os.path.basename(folder) or 'home'} would move {len(plan)} loose files into {len(cats)} folders: "
                + ", ".join(f"{n} to {c}" for c, n in cats.most_common()) + ". "
                + (f"{clash} name clashes would get a number added. " if clash else "") + "Nothing is deleted or overwritten.")

    def rename(self, m):
        src, msg = self._source(m.group(1), m.group(2))
        if not src:
            return msg
        new = au._clean_name(m.group(3))
        if not new or "/" in new or os.sep in new or new in (".", ".."):
            return "That isn't a valid file name, so the real command would be refused."
        if not os.path.splitext(new)[1] and os.path.isfile(src):
            new += os.path.splitext(src)[1]
        wanted = os.path.join(os.path.dirname(src), new)
        dest = au.unique_path(wanted)
        return (f"{os.path.basename(src)} would be renamed to {os.path.basename(dest)}."
                + (" That name is taken, so a number is added instead of overwriting." if dest != wanted else " Nothing is overwritten."))

    def move(self, m):
        src, msg = self._source(m.group(1), m.group(2))
        if not src:
            return msg
        dest = au.find_folder(m.group(3))
        if not dest:
            return f"I couldn't find a folder called {m.group(3)}."
        if not (au.inside_home(dest) or os.path.realpath(dest) == os.path.realpath(au.HOME)):
            return "The destination must be inside your user folder, so this would be refused."
        if os.path.isdir(src) and os.path.realpath(dest).startswith(os.path.realpath(src) + os.sep):
            return "A folder can't be moved into itself, so this would be refused."
        final = au.unique_path(os.path.join(dest, os.path.basename(src)))
        return (f"{os.path.basename(src)} would move to {os.path.basename(dest) or 'home'} as {os.path.basename(final)}. "
                f"It disappears from {os.path.basename(os.path.dirname(src))}.")

    def _found(self):
        return list(getattr(getattr(self.v, "_productivity", None), "found", []) or [])

    def move_batch(self, m):
        files = self._found()
        if not files:
            return "Tell me which files first, like: find PDFs about physics."
        name = au._clean_name(m.group(1))
        dest = au.find_folder(name)
        if not dest:
            return f"There's no {name} folder, so {len(files)} files would go into a new one in Documents."
        clash = sum(os.path.exists(os.path.join(dest, os.path.basename(f))) for f in files)
        return (f"{len(files)} files would move into {os.path.basename(dest)}, starting with "
                + ", ".join(os.path.basename(f) for f in files[:3]) + ". "
                + (f"{clash} name clashes get a number added. " if clash else "") + "You can undo the move afterwards.")

    def rename_batch(self, m):
        files = self._found()
        if not files:
            return "Tell me which files first, like: find PDFs about physics."
        plan = au.propose_names(None, files)
        if not plan:
            return "Those names already look fine, so nothing would change."
        return (f"{len(plan)} of {len(files)} files would be renamed, for example "
                + "; ".join(f"{os.path.basename(s)} to {n}" for s, n in plan[:3]) + ". The AI may pick better names when online.")

    def compress(self, m):
        src, msg = self._source(m.group(1), m.group(2))
        if not src:
            return msg
        out = au.unique_path(src.rstrip("/\\") + ".zip")
        return f"A zip called {os.path.basename(out)} would be created next to the original, covering about {_mb(_size(src))}. The original stays."

    def extract(self, m):
        name = au._clean_name(m.group(1))
        src, msg = self._source(name if name.lower().endswith(".zip") else name + ".zip", m.group(2))
        if not src:
            return msg
        if not zipfile.is_zipfile(src):
            return "That isn't a zip file, so this would be refused."
        with zipfile.ZipFile(src) as z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos)
            unsafe = any(os.path.isabs(i.filename) or ".." in i.filename.replace("\\", "/").split("/") for i in infos)
        if unsafe:
            return "That archive contains unsafe paths, so extraction would be refused."
        if total > au.MAX_EXTRACT_BYTES:
            return "That archive is too large to extract safely, so this would be refused."
        return (f"Extracting would create a folder called {os.path.basename(au.unique_path(os.path.splitext(src)[0]))} "
                f"with {len(infos)} entries, about {_mb(total)}. The zip stays.")

    # ---- data operations ---------------------------------------------------------
    def delete_notes(self, m):
        notes = au.list_notes()
        if not notes:
            return "You have no notes, so nothing would change."
        return (f"All {len(notes)} notes would be permanently erased, including: "
                + "; ".join(t[:50] for _, _, t in notes[-2:]) + ". There's no undo.")

    def delete_note(self, m):
        n = au.get_quick_note(au._num(m.group(1)) or -1)
        return f"Note {n[0]} would be deleted: {n[2][:80]}. Later notes renumber." if n else "I don't have that note."

    def delete_skill(self, m):
        s = au.get_skill(m.group(1))
        return f"The skill {au.skill_key(m.group(1))} and its {len(s['steps'])} steps would be deleted." if s else f"I don't have a skill called {m.group(1)}."

    def delete_deck(self, m):
        key = au.find_deck(m.group(1))
        return f"The deck {key} and its {au.list_decks()[key]} flashcards would be deleted." if key else "I don't have that deck."

    def send_email(self, m):
        d = au.latest_draft()
        if not d:
            return "You have no draft, so nothing would be sent."
        return (f"An email to {d['to']} with subject {d['subject'] or 'none'} and {len(d['body'].split())} words would be sent"
                + ("." if au.email_configured() else ", but email isn't set up, so it would fail.") + " Nothing is sent without your yes.")

    # ---- system operations ----------------------------------------------------------
    def volume(self, m):
        cur = sc.get_volume_percent()
        if cur is None:
            return "Volume control isn't available here."
        direction, mute, level = m.groups()
        new = int(level) if level else 0 if mute else cur + (10 if direction == "up" else -10)
        new = max(0, min(100, new))
        return f"Volume would go from {cur} to {new} percent." + (" That's loud." if new >= 80 and new > cur else "")

    def lock(self, m):
        return "Your PC would lock right away. Aurora keeps running, but you'd need your password before the camera and screen are usable."

    def phone_wifi(self, m):
        ok, out = sc.is_device_connected()
        wireless = ok and re.search(r"\d+\.\d+\.\d+\.\d+:\d+\s+device", out or "")
        return ("Your phone's wifi would turn off. It's connected to Aurora over wifi, so I would lose the connection until you plug in a cable."
                if wireless else "Your phone's wifi would turn off. The USB connection keeps working." if ok else
                "I can't see your phone, so this would fail.")

    def edit_code(self, m):
        name, instr = m.group(1).strip(), m.group(2).strip()
        path = sc.resolve_project_path(name) or sc.find_arduino_sketch(name)
        if not path:
            return None
        plus, client = getattr(self.v, "plus", None), getattr(self.v, "client", None)
        if client is None or (plus is not None and not plus.offline.online()):
            return "I need the AI online to preview a code edit. Everything else can be simulated offline."
        from jarvis_ui import brain
        with open(path, encoding="utf-8", errors="ignore") as f:
            current = f.read()
        return f"Editing {name} would change it: {sc.summarize_diff(current, brain.edit_code(client, current, instr))}. A .bak backup is kept."

    RULES = (
        (rf"(?:move|put|send|file)\s+(?:all\s+)?(?:these|those|them)(?:\s+files)?\s+(?:in|into|to)\s+(?:my\s+|the\s+)?(.+?)(?:\s+folder)?$", "move_batch"),
        (r"(?:rename|fix|clean up|tidy(?: up)?)\s+(?:all\s+)?(?:these|those|them)(?:\s+files)?(?:\s+(?:properly|nicely|up))?$", "rename_batch"),
        (r"organi[sz]e (?:my |the )?(.+?)(?: folder)?$", "organize"),
        (rf"rename (?:the |my )?(?:file |folder )?(.+?){_FROM} to (.+)$", "rename"),
        (rf"move (?:the |my )?(?:file |folder )?(.+?){_FROM} to (?:my |the )?(?:folder )?(.+)$", "move"),
        (rf"(?:compress|zip(?: up)?) (?:the |my )?(?:file |folder )?(.+?){_FROM}$", "compress"),
        (rf"(?:extract|unzip|uncompress) (?:the |my )?(?:file )?(.+?){_FROM}$", "extract"),
        (r"(?:delete|clear|erase) (?:all )?(?:of )?(?:my )?notes$", "delete_notes"),
        (r"delete note (?:number )?(\w+)$", "delete_note"),
        (r"delete (?:the )?skill (.+)$", "delete_skill"),
        (r"delete (?:the )?deck (.+)$", "delete_deck"),
        (r"send (?:the |my |that |this )?(?:e-?mail|mail|draft)(?: now)?$", "send_email"),
        (r"(?:volume (up|down)|(mute)(?: volume)?|volume to (\d+))$", "volume"),
        (r"lock (?:my |the )?(?:computer|pc|laptop|screen)$", "lock"),
        (r"(?:turn|switch) off (?:the )?wifi (?:on|in) (?:my )?phone$|(?:turn|switch) off (?:my )?phone(?:'s)? wifi$", "phone_wifi"),
        (r"edit (?:code |the file |the sketch |the project )?(.+?) to (.+)$", "edit_code"),
    )

    def run(self, op):
        for pattern, name in self.RULES:
            m = re.match(pattern, op, re.I)
            if not m:
                continue
            try:
                return getattr(self, name)(m)
            except (au.FileError, OSError) as e:
                return str(e)
        return None


def handle_command(voice, text):
    """Voice entry point for voice_assistant.py. Returns True if handled."""
    sim = getattr(voice, "_simulator", None)
    if sim is None:
        sim = voice._simulator = Simulator(voice)
    t = text.strip(" .!?,")
    if sim.last and _GO.fullmatch(t):
        (op, stamp), sim.last = sim.last, None
        if time.time() - stamp > GO_TTL:
            voice._speak("That simulation has expired. Simulate it again first.")
        elif not voice._handle_local_command(op):
            voice._speak("I couldn't run that.")
        return True
    sim.last = None
    m = _PREFIX.match(t)
    if not m:
        return False
    op = re.sub(r"^(\w+)\b", lambda g: GERUNDS.get(g.group(1).lower(), g.group(1)), re.sub(r"\s+for me$", "", m.group(1)))
    result = sim.run(op)
    if result is None:
        return False
    sim.last = (op, time.time())
    try:
        voice.hologram.show_info_card("Simulation Room: " + op[:60], result)
    except Exception:
        pass
    voice._speak(result + " Say go ahead to run it for real.")
    return True
