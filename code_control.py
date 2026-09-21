"""
Code-editing hooks: lets Aurora open a project folder or file in VS Code,
open/create an Arduino sketch in the Arduino IDE, and scaffold a new blank
code file. Aurora never edits code for you here — this just gets the
right file open in the right editor, fast, by voice, so you can make the
change yourself.

PROJECT NAMES: code_projects.json (next to api_key.txt) maps a spoken
name to a real path on disk, e.g.:
    {
      "robot arm": "C:/Users/you/Projects/robot-arm",
      "blink test": "C:/Users/you/Arduino/blink_test/blink_test.ino"
    }
Add entries yourself; Aurora only reads this file, never writes to it.

NEW FILES Aurora creates itself land in:
    projects/<name>.<ext>          (via "new python script called X")
    arduino_sketches/<name>/<name>.ino   (via "new arduino sketch called X")
so they're always found again later without needing a projects.json entry.

REQUIREMENTS:
  - VS Code: the 'code' command must be on PATH (VS Code's installer has
    an "Add to PATH" checkbox — enable it, or run "Shell Command: Install
    'code' command in PATH" from VS Code's command palette).
  - Arduino IDE: no setup needed on Windows — .ino files are opened via
    whatever program Windows has associated with that extension, which
    is the Arduino IDE once it's installed.
"""

import json
import os
import re
import subprocess
import difflib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_FILE = os.path.join(BASE_DIR, "..", "code_projects.json")
PROJECTS_ROOT = os.path.join(BASE_DIR, "..", "projects")
ARDUINO_ROOT = os.path.join(BASE_DIR, "..", "arduino_sketches")

LANG_EXT = {
    "python": ".py", "javascript": ".js", "js": ".js",
    "cpp": ".cpp", "c++": ".cpp", "c": ".c", "html": ".html",
}

ARDUINO_TEMPLATE = (
    "void setup() {\n"
    "  Serial.begin(9600);\n"
    "\n"
    "}\n"
    "\n"
    "void loop() {\n"
    "\n"
    "}\n"
)

# Kept small and fast — code gen/edits happen inline in the voice loop,
# so a lighter model keeps the wait reasonable.
CODE_MODEL = "groq/compound-mini"

CODE_GEN_SYSTEM_PROMPT = (
    "You are a code generation engine. Given a request, output ONLY the "
    "complete source code for the file — no explanation, no markdown code "
    "fences, no commentary before or after. Just the raw file contents."
)

CODE_EDIT_SYSTEM_PROMPT = (
    "You are a code editing engine. You will be given the full current "
    "contents of a file and an instruction for how to change it. Output "
    "ONLY the complete, updated file contents after applying the change — "
    "no explanation, no markdown code fences, no commentary."
)


def _safe_filename(name):
    cleaned = re.sub(r"[^a-zA-Z0-9_\- ]", "", name).strip()
    cleaned = cleaned.replace(" ", "_")
    return cleaned or "untitled"


def _template_for(language, name):
    if language == "python":
        return f'"""{name}"""\n\n\ndef main():\n    pass\n\n\nif __name__ == "__main__":\n    main()\n'
    if language in ("javascript", "js"):
        return f"// {name}\n\nfunction main() {{\n\n}}\n\nmain();\n"
    if language in ("cpp", "c++"):
        return f"// {name}\n#include <iostream>\n\nint main() {{\n    return 0;\n}}\n"
    if language == "c":
        return f"// {name}\n#include <stdio.h>\n\nint main(void) {{\n    return 0;\n}}\n"
    if language == "html":
        return f"<!-- {name} -->\n<!DOCTYPE html>\n<html>\n<head><title>{name}</title></head>\n<body>\n\n</body>\n</html>\n"
    return f"# {name}\n"


# ---- project name lookup ---------------------------------------------------

def load_projects():
    if not os.path.exists(PROJECTS_FILE):
        return {}
    try:
        with open(PROJECTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def resolve_project_path(name):
    """Checks code_projects.json first, then anything Aurora itself
    created in projects/ via create_code_file()."""
    projects = load_projects()
    key = name.strip().lower()
    if key in projects:
        return projects[key]

    safe = _safe_filename(name)
    if os.path.isdir(PROJECTS_ROOT):
        for fname in os.listdir(PROJECTS_ROOT):
            if os.path.splitext(fname)[0] == safe:
                return os.path.join(PROJECTS_ROOT, fname)
    return None


def find_arduino_sketch(name):
    """Checks code_projects.json first, then anything Aurora itself
    created via create_arduino_sketch()."""
    projects = load_projects()
    key = name.strip().lower()
    if key in projects:
        return projects[key]

    safe = _safe_filename(name)
    candidate = os.path.join(ARDUINO_ROOT, safe, f"{safe}.ino")
    if os.path.exists(candidate):
        return candidate
    return None


def get_or_make_path(name, language="python"):
    """Resolves an existing project/sketch by name, or creates a fresh
    (templated) file/sketch for it if nothing exists yet. Used before
    generating code so there's always somewhere to write it."""
    existing = resolve_project_path(name) or find_arduino_sketch(name)
    if existing:
        return existing
    if language.lower() in ("arduino", "ino"):
        return create_arduino_sketch(name)
    return create_code_file(name, language)


# ---- scaffolding new files --------------------------------------------------

def create_code_file(name, language="python"):
    """Creates a blank templated file under projects/ (only if it doesn't
    already exist — never overwrites your work) and returns its path."""
    ext = LANG_EXT.get(language.lower(), ".txt")
    safe = _safe_filename(name)
    os.makedirs(PROJECTS_ROOT, exist_ok=True)
    file_path = os.path.join(PROJECTS_ROOT, safe + ext)
    if not os.path.exists(file_path):
        with open(file_path, "w") as f:
            f.write(_template_for(language.lower(), name))
    return file_path


def create_arduino_sketch(name):
    """Creates <name>/<name>.ino under arduino_sketches/ (Arduino requires
    the sketch file to live in a folder of the same name) and returns the
    .ino path. Never overwrites an existing sketch."""
    safe = _safe_filename(name)
    sketch_dir = os.path.join(ARDUINO_ROOT, safe)
    os.makedirs(sketch_dir, exist_ok=True)
    ino_path = os.path.join(sketch_dir, f"{safe}.ino")
    if not os.path.exists(ino_path):
        with open(ino_path, "w") as f:
            f.write(ARDUINO_TEMPLATE)
    return ino_path


# ---- opening editors ---------------------------------------------------------

def open_in_vscode(path):
    if not os.path.exists(path):
        return False, f"Path not found: {path}"
    try:
        # 'code' ships as a .cmd shim on Windows, so shell=True lets it
        # resolve the same way typing it in a terminal would.
        result = subprocess.run(f'code "{path}"', shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return True, ""
        return False, (result.stdout + result.stderr).strip() or "VS Code's 'code' command isn't on PATH"
    except Exception as e:
        return False, str(e)


def open_in_arduino(path):
    if not os.path.exists(path):
        return False, f"Path not found: {path}"
    try:
        os.startfile(path)  # .ino is associated with the Arduino IDE on Windows
        return True, ""
    except AttributeError:
        try:
            subprocess.run(["arduino", path], timeout=5)
            return True, ""
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


# ---- AI code generation / editing -------------------------------------------
# Uses the same Groq client the voice assistant already holds — passed in
# by the caller rather than created here, so there's only one place that
# owns the API key. Aurora writes a full file's contents at a time (never
# a diff/patch), and always backs up whatever was there before.

def _strip_code_fences(text):
    """Models often wrap code in ``` fences even when told not to —
    strip them defensively so raw markdown never ends up saved as code."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9_+\-]*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text.strip() + "\n"


def generate_code(client, description, language="python"):
    """Asks the model for a complete new file implementing `description`.
    Returns the code text. Raises on API failure — caller should catch."""
    prompt = f"Write {language} code that does the following: {description}"
    response = client.chat.completions.create(
        model=CODE_MODEL,
        max_tokens=1500,
        messages=[
            {"role": "system", "content": CODE_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or ""
    return _strip_code_fences(content)


def edit_code(client, current_content, instruction):
    """Sends the current file contents plus a plain-English instruction,
    returns the complete updated file contents."""
    user_msg = (
        f"Current file contents:\n---\n{current_content}\n---\n\n"
        f"Instruction: {instruction}\n\n"
        f"Output the complete updated file contents."
    )
    response = client.chat.completions.create(
        model=CODE_MODEL,
        max_tokens=2000,
        messages=[
            {"role": "system", "content": CODE_EDIT_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    content = response.choices[0].message.content or ""
    return _strip_code_fences(content)


def backup_file(path):
    """Copies whatever's currently at `path` to `path + '.bak'` before
    it gets overwritten, so one bad generation/edit never destroys the
    only copy of your work."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass


def write_file_content(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup_file(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def summarize_diff(old_content, new_content):
    """Short spoken-friendly summary of what changed between two
    versions of a file — line counts, not a full diff dump, so it's
    quick to say out loud before/after an edit."""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))

    if added == 0 and removed == 0:
        return "no changes"
    parts = []
    if added:
        parts.append(f"{added} line{'s' if added != 1 else ''} added")
    if removed:
        parts.append(f"{removed} line{'s' if removed != 1 else ''} removed")
    return " and ".join(parts)
