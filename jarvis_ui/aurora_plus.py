"""
Aurora Plus - four features, installed with one call from main.py (before voice.start()):

  1. COWORK      sandboxed autonomous coding agent (plan -> write -> test -> fix -> verify -> ZIP)
  2. NETWORK HUB control Aurora from a phone/PC on your LAN (token-protected web page)
  3. OFFLINE MODE local commands, UI, tools and file search work with no AI API / no internet
  4. FILE SEARCH semantic search over folders you add to your vault (TF-IDF + synonyms + fuzzy trigrams)

Voice:
  "Aurora, cowork build a prime number checker" / "cowork status" / "cowork stop"
  "Aurora, start network hub" / "stop network hub" / "hub address"
  "Aurora, offline mode on" / "offline mode off" / "offline mode status"
  "Aurora, add folder C:/Users/you/notes to my vault" / "index my files"
  "Aurora, find files about budget" / "what did I write about arduino"

COWORK SAFETY (layered, best-effort - NOT a VM): fresh temp folder, flat layout, .py/.txt/.md/.json/.csv only,
secret/destructive-intent scan, static Python scan (stdlib only, no subprocess/socket/eval/...), the only command
it can run is a fixed `python -I -m unittest discover`, every step also validated by GPT-OSS Safeguard (fails
CLOSED). Output is a ZIP in cowork_exports/ for YOU to review.
Optional overrides (one line each, next to api_key.txt): cowork_model.txt, safeguard_model.txt
Optional offline speech recognition: pip install pocketsphinx
"""
import ast
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import speech_recognition as sr

from jarvis_ui import system_control as sc

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _cfg(name, default):
    try:
        with open(os.path.join(BASE, name), "r", encoding="utf-8") as f:
            return f.read().strip() or default
    except OSError:
        return default


CODER_MODEL = _cfg("cowork_model.txt", "openai/gpt-oss-120b")
SAFEGUARD_MODEL = _cfg("safeguard_model.txt", "openai/gpt-oss-safeguard-20b")

# ============================================================================
# GUARD - credentials / private data / destructive / unauthorized actions
# ============================================================================

SECRETS = re.compile("|".join([
    r"gsk_[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9_\-]{20,}", r"AKIA[0-9A-Z]{16}", r"ghp_[A-Za-z0-9]{30,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY", r"\b\d{3}-\d{2}-\d{4}\b", r"\b(?:\d[ -]?){15,16}\b",
    r"\b(?:api[_-]?key|secret|passw(?:or)?d|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}",
    r"api_key\.txt|phone_pin|my_number|contacts\.json|hub_token|\.env\b|id_rsa|\.ssh\b|"
    r"credentials\.(?:json|txt)|cookies\.sqlite|login data",
]), re.I)

BAD_INTENT = re.compile(
    r"rm\s+-rf|format\s+[a-z]:|system32|disable\s+(?:antivirus|defender|firewall)|keylogger|ransomware|"
    r"reverse\s+shell|password\s+stealer|steal\s+(?:passwords|cookies|credentials)|privilege\s+escalation|"
    r"registry\s+(?:edit|hack)|bypass\s+(?:login|auth|security)|ddos|exploit\s+(?:the|a)\s|"
    r"read\s+(?:my|the)\s+(?:api\s*key|password|pin)|delete\s+(?:all|my)\s+files", re.I)

BAD_MODULES = {"subprocess", "socket", "ctypes", "winreg", "_winreg", "shutil", "http", "urllib", "ftplib",
               "smtplib", "telnetlib", "requests", "importlib", "pickle", "marshal", "multiprocessing",
               "builtins", "runpy", "pkgutil", "pty", "signal", "webbrowser", "code", "codeop", "imp",
               "socketserver", "xmlrpc", "ssl", "select", "mmap", "msvcrt", "winsound", "sysconfig"}
BAD_NAMES = {"eval", "exec", "compile", "__import__", "breakpoint"}
STRICT_ATTRS = {"unlink", "rmdir", "removedirs", "rmtree", "startfile", "chmod", "chown", "chdir", "chroot",
                "environ", "getenv", "putenv", "expanduser", "home", "kill", "killpg", "fork", "forkpty",
                "execv", "execve", "execl", "execlp", "execvp", "spawnl", "spawnv", "popen", "abort", "rename",
                "__subclasses__", "__globals__", "__builtins__", "__code__", "__mro__"}
OS_ONLY_ATTRS = {"system", "remove"}
BAD_PATH_LITERAL = re.compile(r"^(?:[A-Za-z]:[\\/]|/|~|\\\\)|(?:^|[\\/])\.\.(?:[\\/]|$)")
ALLOWED_EXT = {".py", ".txt", ".md", ".json", ".csv"}
TEST_CMD = [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test*.py"]
CMD_DENY = re.compile(r"\b(rm|del|erase|rmdir|rd|format|shutdown|reboot|taskkill|reg|regedit|net|sudo|runas|"
                      r"powershell|cmd|bash|sh|curl|wget|pip|npm|chmod|chown|diskpart|bcdedit|schtasks|sc)\b", re.I)

SAFEGUARD_POLICY = (
    "You validate actions of a sandboxed coding agent. ALLOW: writing/editing small source files inside its "
    "temporary sandbox and running that project's unit tests with the Python standard library. VIOLATION: "
    "accessing or embedding credentials, API keys, PINs, personal/private data; network access; spawning "
    "processes or shells; destructive, admin, or system-modifying commands; reading or writing outside the "
    "sandbox; obfuscated or malicious code (malware, keyloggers, exfiltration); anything unauthorized by the "
    "user's task. Reply with ONLY JSON: {\"allow\": true|false, \"reason\": \"short reason\"}")


class Guard:
    def __init__(self, plus):
        self.p = plus
        self._approved = set()

    @staticmethod
    def text_ok(text):
        if SECRETS.search(text):
            return False, "it contains credentials or private data"
        if BAD_INTENT.search(text):
            return False, "it looks destructive, malicious or admin-level"
        return True, ""

    @staticmethod
    def path_ok(rel):
        rel = (rel or "").strip().replace("\\", "/")
        if not re.fullmatch(r"[\w\-.]{1,60}", rel) or rel.startswith(".") or ".." in rel:
            return False, f"illegal file name '{rel}' (flat layout, simple names only)"
        if os.path.splitext(rel)[1].lower() not in ALLOWED_EXT:
            return False, f"file type not allowed: {rel}"
        return True, rel

    @staticmethod
    def python_ok(src, local_mods):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return True, ""  # the tests will report it and the agent will fix it
        stdlib = getattr(sys, "stdlib_module_names", None)
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                mods = [n.module.split(".")[0]]
            for m in mods:
                if m in BAD_MODULES:
                    return False, f"module '{m}' is not allowed"
                if m not in local_mods and stdlib is not None and m not in stdlib:
                    return False, f"third-party module '{m}' is not available (stdlib only)"
            if isinstance(n, ast.Name) and n.id in BAD_NAMES:
                return False, f"'{n.id}' is not allowed"
            if isinstance(n, ast.Attribute):
                base = n.value.id if isinstance(n.value, ast.Name) else None
                if n.attr in STRICT_ATTRS or (n.attr in OS_ONLY_ATTRS and base == "os"):
                    return False, f"'.{n.attr}' is not allowed"
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" \
                    and len(n.args) > 1 and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value in (STRICT_ATTRS | OS_ONLY_ATTRS):
                return False, "dynamic access to a blocked attribute"
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and BAD_PATH_LITERAL.search(n.value):
                return False, f"path outside the sandbox: {n.value[:40]!r}"
        return True, ""

    @staticmethod
    def command_ok(cmd):
        if cmd != TEST_CMD:
            return False, "only the sandbox unit-test command may run"
        if CMD_DENY.search(" ".join(cmd[1:])):
            return False, "destructive/admin command blocked"
        return True, ""

    def judge(self, kind, detail):
        """GPT-OSS Safeguard verdict. Fails CLOSED if the model is unreachable."""
        client = self.p.voice.client
        if client is None:
            return False, "no AI client for safeguard validation"
        key = (kind, zlib.crc32(detail.encode("utf-8", "ignore")))
        if key in self._approved:
            return True, ""
        try:
            r = client.chat.completions.create(
                model=SAFEGUARD_MODEL, max_tokens=1200, temperature=0,
                messages=[{"role": "system", "content": SAFEGUARD_POLICY},
                          {"role": "user", "content": f"ACTION: {kind}\n---\n{detail[:7000]}"}])
            raw = (r.choices[0].message.content or "") if r.choices else ""
            m = re.search(r"\{[^{}]*\"allow\"[^{}]*\}", raw, re.S)
            verdict = json.loads(m.group(0)) if m else None
        except Exception as e:
            return False, f"safeguard unavailable ({type(e).__name__})"
        if not verdict:
            return False, "safeguard returned no verdict"
        if verdict.get("allow") is True:
            self._approved.add(key)
            return True, ""
        return False, str(verdict.get("reason", "blocked by safeguard"))[:160]


# ============================================================================
# COWORK
# ============================================================================

MAX_ATTEMPTS, RUN_TIMEOUT, MAX_FILES, MAX_FILE_CHARS = 4, 20, 8, 40000
EXPORT_DIR = os.path.join(BASE, "cowork_exports")

PLAN_SYS = ("You are Cowork, a careful coding agent. Write a plan of at most 6 short lines: which files you will "
            "create and how unit tests will verify them. Python standard library only, no network, no external "
            "packages, nothing outside the project folder.")
BUILD_SYS = (
    "You are Cowork, a sandboxed coding agent. Implement the task using ONLY the Python standard library. "
    "Output files in exactly this format and nothing else:\n"
    "### FILE: name.py\n<full file content>\n### END\n"
    "Rules: flat layout (no folders), max 8 files, at least one unittest file named test_*.py that really tests "
    "the behaviour (run with `python -m unittest discover`). Never use network, subprocess, os.system, eval/exec, "
    "environment variables, absolute paths, credentials or private data. When fixing errors, output only the "
    "changed files, in full.")
_FILE_RE = re.compile(r"^### FILE:\s*(.+?)\s*\n(.*?)^### END\s*$", re.M | re.S)


def parse_files(reply):
    files = {}
    for name, body in _FILE_RE.findall(reply or ""):
        body = re.sub(r"^```[\w+-]*\n", "", body.strip("\n"))
        body = re.sub(r"\n```\s*$", "", body)
        files[name.strip().strip("`")] = body.rstrip() + "\n"
    return files


class Cowork:
    def __init__(self, plus):
        self.p = plus
        self.busy = False
        self.cancel = False
        self.last_zip = None
        self.status = "idle"

    def start(self, task):
        v = self.p.voice
        if self.busy:
            return "Cowork is already working. Say cowork status, or cowork stop."
        if v.client is None or not self.p.offline.online():
            return "Cowork needs the AI connection, and I'm offline right now."
        ok, why = Guard.text_ok(task)
        if not ok:
            return f"I won't run that task because {why}."
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return "Cowork is on it. I'll work in a sandbox and tell you when the ZIP is ready."

    def stop(self):
        self.cancel = True
        return "Stopping Cowork after the current step." if self.busy else "Cowork isn't running."

    def _card(self, task, text):
        self.status = text
        self.p.log(f"COWORK: {text}")
        try:
            self.p.holo.show_info_card(f"Cowork: {task[:80]}", text)
        except Exception:
            pass

    def _chat(self, msgs, max_tokens):
        r = self.p.voice.client.chat.completions.create(
            model=CODER_MODEL, max_completion_tokens=max_tokens, temperature=0.2, messages=msgs,
            extra_body={"reasoning_effort": "medium", "include_reasoning": False})
        return (r.choices[0].message.content or "") if r.choices else ""

    def _apply(self, root, files):
        """Validate a whole batch, then write it. Nothing is written unless everything passes."""
        if not files or len(files) > MAX_FILES:
            return False, f"between 1 and {MAX_FILES} files per round"
        existing = {os.path.splitext(f)[0] for f in os.listdir(root)}
        local = existing | {os.path.splitext(n)[0] for n in files}
        clean = {}
        for name, body in files.items():
            ok, rel = Guard.path_ok(name)
            if not ok:
                return False, rel
            if len(body) > MAX_FILE_CHARS:
                return False, f"{rel} is too large"
            ok, why = Guard.text_ok(body)
            if not ok:
                return False, f"{rel}: {why}"
            if rel.endswith(".py"):
                ok, why = Guard.python_ok(body, local)
                if not ok:
                    return False, f"{rel}: {why}"
            clean[rel] = body
        ok, why = self.p.guard.judge("write_files", "\n\n".join(f"# {k}\n{v[:3000]}" for k, v in clean.items()))
        if not ok:
            return False, f"safeguard: {why}"
        for rel, body in clean.items():
            dest = os.path.realpath(os.path.join(root, rel))
            if os.path.dirname(dest) != os.path.realpath(root):
                return False, "path escaped the sandbox"
            with open(dest, "w", encoding="utf-8") as f:
                f.write(body)
        return True, ""

    def _test(self, root):
        if not any(f.startswith("test") and f.endswith(".py") for f in os.listdir(root)):
            return False, "No test_*.py file exists. Add a unittest file."
        for check in (Guard.command_ok(TEST_CMD), self.p.guard.judge("run_command", " ".join(TEST_CMD[1:]))):
            if not check[0]:
                return False, f"command blocked: {check[1]}"
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        try:
            r = subprocess.run(TEST_CMD, cwd=root, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {RUN_TIMEOUT}s (infinite loop?)"
        out = (r.stdout + r.stderr)[-4000:]
        m = re.search(r"Ran (\d+) tests?", out)
        return (r.returncode == 0 and bool(m) and int(m.group(1)) > 0), out

    def _export(self, root, task):
        os.makedirs(EXPORT_DIR, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "_", task.lower()).strip("_")[:30] or "task"
        return shutil.make_archive(os.path.join(EXPORT_DIR, f"{slug}_{time.strftime('%Y%m%d_%H%M%S')}"), "zip", root)

    def _run(self, task):
        p = self.p
        self.busy, self.cancel = True, False
        root = tempfile.mkdtemp(prefix="aurora_cowork_")
        result, attempt, verified = "Cowork stopped.", 0, False
        try:
            self._card(task, "Planning...")
            plan = self._chat([{"role": "system", "content": PLAN_SYS}, {"role": "user", "content": task}], 2000)
            ok, why = p.guard.judge("plan", f"Task: {task}\nPlan: {plan}")
            if not ok:
                result = f"Cowork blocked this task: {why}"
                return
            msgs = [{"role": "system", "content": BUILD_SYS},
                    {"role": "user", "content": f"Task: {task}\nPlan:\n{plan}"}]
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if self.cancel:
                    break
                self._card(task, f"Attempt {attempt}/{MAX_ATTEMPTS}: writing code...")
                reply = self._chat(msgs, 8000)
                msgs.append({"role": "assistant", "content": reply})
                files = parse_files(reply)
                if not files:
                    feedback = "No '### FILE:' blocks found. Use the exact format."
                else:
                    ok, why = self._apply(root, files)
                    if not ok:
                        feedback = f"Rejected by safety validation: {why}. Rewrite without it."
                        p.log(f"COWORK: rejected - {why}")
                    else:
                        self._card(task, f"Attempt {attempt}/{MAX_ATTEMPTS}: running tests...")
                        verified, out = self._test(root)
                        if verified:
                            break
                        feedback = f"Tests failed. Output:\n{out[-2500:]}\nFix it; output only changed files."
                msgs.append({"role": "user", "content": feedback})
            names = sorted(os.listdir(root))
            if not names:
                result = "Cowork couldn't produce any files that passed safety validation."
                return
            self.last_zip = self._export(root, task)
            state = "verified, all tests pass" if verified else "NOT verified, tests still failing"
            result = (f"Cowork done after {attempt} attempt(s): {state}. Files: {', '.join(names)}. "
                      f"ZIP: {self.last_zip}")
        except Exception as e:
            result = f"Cowork error: {type(e).__name__}: {e}"
        finally:
            shutil.rmtree(root, ignore_errors=True)
            self.busy = False
            self._card(task, result)
            short = ("Cowork finished. The ZIP is in the cowork exports folder, tests passed."
                     if verified else result.split(" ZIP:")[0])
            self.p.voice.speak_now(short)


# ============================================================================
# SEMANTIC FILE SEARCH (extends the keyword vault in system_control)
# ============================================================================

INDEX_FILE = os.path.join(BASE, "semantic_index.json")
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "site-packages", "$RECYCLE.BIN"}
SKIP_FILES = re.compile(r"api_key|phone_pin|my_number|contacts|hub_token|\.env|password|secret|credential|"
                        r"id_rsa|cookies|login", re.I)
sc.VAULT_TEXT_EXTS |= {".js", ".ino", ".cpp", ".c", ".html", ".ini", ".yaml", ".yml"}

_SYN = [
    {"money", "budget", "cost", "price", "invoice", "payment", "expense", "bill", "finance", "salary"},
    {"car", "vehicle", "auto", "automobile"}, {"bug", "error", "exception", "crash", "fail", "issue", "problem"},
    {"test", "exam", "quiz", "assessment"}, {"arduino", "esp32", "microcontroller", "sketch", "firmware"},
    {"meeting", "appointment", "schedule", "calendar", "event"}, {"idea", "plan", "proposal", "concept"},
    {"homework", "assignment", "project", "task", "coursework"}, {"photo", "image", "picture", "screenshot"},
    {"note", "memo", "journal", "diary"}, {"experiment", "lab", "trial", "observation"},
    {"code", "script", "program", "source"}, {"space", "astronomy", "planet", "star", "orbit", "galaxy"},
    {"phone", "mobile", "android", "call"}, {"recipe", "food", "cook", "meal"},
]
SYN_MAP = defaultdict(set)
for _g in _SYN:
    for _w in _g:
        SYN_MAP[_w] |= _g - {_w}
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "were", "with", "that",
         "this", "it", "i", "my", "me", "have", "has", "did", "do", "does", "what", "everything", "about", "write",
         "wrote", "find", "you", "your", "files", "file", "documents", "docs", "search", "show", "any", "all"}


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _fid(kind, val):
    return zlib.crc32(f"{kind}:{val}".encode())


def _feats(text, expand=False):
    c = Counter()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        s = _stem(w)
        c[_fid("w", s)] += 1
        if expand:
            for x in SYN_MAP.get(w, set()) | SYN_MAP.get(s, set()):
                c[_fid("w", _stem(x))] += 0.6
        if len(s) >= 4:
            for i in range(len(s) - 2):
                c[_fid("g", s[i:i + 3])] += 0.15
    return c


def _chunks(text, words=120):
    w = text.split()
    for i in range(0, len(w), words):
        piece = " ".join(w[i:i + words]).strip()
        if piece:
            yield piece


class SemanticSearch:
    def __init__(self, plus):
        self.p = plus
        self.data = None
        self._load()

    def _load(self):
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = None

    def has_index(self):
        return bool(self.data and self.data.get("docs"))

    def build(self):
        """Index every vault folder. Secrets and private files are skipped. -> (files, chunks)"""
        docs, feats = [], []
        for folder in sc._load_json("vault_folders.json", []):
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for fn in files:
                    path = os.path.join(root, fn)
                    try:
                        if SKIP_FILES.search(fn) or os.path.getsize(path) > 2_000_000:
                            continue
                    except OSError:
                        continue
                    for chunk in _chunks(sc._read_any(path)):
                        if SECRETS.search(chunk) or len(docs) >= 20000:
                            continue
                        docs.append({"path": path, "text": chunk[:400]})
                        feats.append(_feats(chunk))
        df = Counter(k for f in feats for k in f)
        n = len(docs)
        idf = {k: math.log((n + 1) / (v + 1)) + 1 for k, v in df.items()}
        post = defaultdict(list)
        for d, f in enumerate(feats):
            vec = {k: (1 + math.log(v)) * idf[k] if v >= 1 else v * idf[k] for k, v in f.items()}
            norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
            for k, x in vec.items():
                post[k].append([d, round(x / norm, 4)])
        self.data = {"docs": docs, "idf": {str(k): v for k, v in idf.items()},
                     "post": {str(k): v for k, v in post.items()}}
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        return len({d["path"] for d in docs}), n

    def query(self, text, top_k=3, min_score=0.06):
        """None if no index, else best-first [{path, text, score}]."""
        if not self.has_index():
            return None
        idf, post = self.data["idf"], self.data["post"]
        q = {k: v * idf[str(k)] for k, v in _feats(text, expand=True).items() if str(k) in idf}
        norm = math.sqrt(sum(x * x for x in q.values()))
        if not norm:
            return []
        scores = defaultdict(float)
        for k, qw in q.items():
            for d, w in post.get(str(k), ()):
                scores[d] += (qw / norm) * w
        best = {}
        for d, s in scores.items():
            path = self.data["docs"][d]["path"]
            if s >= min_score and s > best.get(path, (0, 0))[0]:
                best[path] = (s, d)
        ranked = sorted(best.values(), reverse=True)[:top_k]
        return [{"path": self.data["docs"][d]["path"], "text": self.data["docs"][d]["text"],
                 "score": round(s, 3)} for s, d in ranked]


# ============================================================================
# OFFLINE MODE
# ============================================================================

class Offline:
    """Detects missing internet / AI key and answers basic things locally. Local commands (timers, notes,
    math, apps, volume, displays) never need the AI anyway."""

    def __init__(self, plus):
        self.p = plus
        self.force = False
        self._ok, self._t = True, 0.0

    def online(self):
        if self.force:
            return False
        if time.time() - self._t > 10:
            self._t = time.time()
            try:
                socket.create_connection(("1.1.1.1", 53), 1.5).close()
                self._ok = True
            except OSError:
                self._ok = False
        return self._ok

    def reason(self):
        return "no AI key" if self.p.voice.client is None else "offline"

    def answer(self, text):
        t = text.lower()
        why = self.reason()
        if re.search(r"\b(hi|hello|hey)\b", t):
            return f"Hello! I'm in offline mode ({why}), but local commands and file search still work."
        if re.search(r"\b(date|what day)\b", t):
            return time.strftime("Today is %A, %B %d.")
        if re.search(r"\btime\b", t):
            return time.strftime("It's %I:%M %p.")
        if re.search(r"status|battery|cpu|memory|computer", t):
            return sc.get_system_status() or "System stats need psutil."
        if re.search(r"\bhelp\b|what can you do", t):
            return ("Offline I can do timers, notes, math, opening apps, volume and media, screenshots, "
                    "system status, the hologram displays, and searching your files.")
        hits = self.p.search.query(text) or sc.vault_search(text)
        if hits:
            h = hits[0]
            return f"From your files, {os.path.basename(h['path'])}: {h['text'][:180]}"
        return (f"I can't answer that without the AI ({why}). Try a timer, a note, math, "
                f"or say 'find files about' something.")


class OfflineAwareBrain:
    """Wraps the real Brain: answers locally when there is no AI key or no internet."""

    def __init__(self, plus, real):
        self.p, self.real = plus, real

    def ask(self, text, on_delta, cancel):
        if self.real is None or not self.p.offline.online():
            reply = self.p.offline.answer(text)
            on_delta(reply)
            return reply
        return self.real.ask(text, on_delta, cancel)

    def __getattr__(self, name):
        return getattr(self.real, name)


# ============================================================================
# NETWORK HUB
# ============================================================================

HUB_PORT = 8765
HUB_BLOCKED = ("unlock", "lock my", "shutdown", "delete", "forget", "clear my notes")
TOKEN_FILE = os.path.join(BASE, "hub_token.txt")

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Aurora Hub</title>
<style>body{font:16px system-ui;background:#06101c;color:#cfe8ff;margin:0;padding:14px}
input,button{font:inherit;padding:12px;border-radius:8px;border:1px solid #2a7;background:#0b1e30;color:#cfe8ff}
input{width:100%;box-sizing:border-box}button{margin:6px 6px 0 0}#o{white-space:pre-wrap;margin-top:14px;min-height:90px}
small{color:#7aa}</style><h3>AURORA HUB</h3>
<input id=t placeholder="Say something to Aurora" autofocus onkeydown="if(event.key=='Enter')s()">
<div><button onclick=s()>Send</button><button onclick="q('what time is it')">Time</button>
<button onclick="q('system status')">Status</button><button onclick="q('stop')">Stop</button>
<button onclick="q('play')">Play/Pause</button><button onclick="q('volume up')">Vol +</button>
<button onclick="q('volume down')">Vol -</button></div><div id=o></div><small id=st></small>
<script>const T=location.hash.slice(1),H={'X-Token':T,'Content-Type':'application/json'};
async function q(x){t.value=x;s()}
async function s(){o.textContent='...';try{const r=await fetch('/cmd',{method:'POST',headers:H,
body:JSON.stringify({text:t.value})});const j=await r.json();o.textContent=j.reply||j.error;t.value=''}
catch(e){o.textContent='Connection failed'}}
async function u(){try{const r=await fetch('/status',{headers:H});const j=await r.json();
st.textContent=j.error||('state: '+j.state+' | '+(j.online?'online':'offline')+' | CPU '+j.cpu+'% | MEM '+j.mem+'%')}catch(e){}}
u();setInterval(u,5000)</script>"""


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Hub:
    def __init__(self, plus):
        self.p = plus
        self.server = None
        self.token = None
        self.port = HUB_PORT
        self.fails = defaultdict(list)

    def url(self):
        return f"http://{lan_ip()}:{self.port}/#{self.token}" if self.server else None

    def start(self):
        if self.server:
            return True, self.url()
        try:
            with open(TOKEN_FILE, "r") as f:
                self.token = f.read().strip()
        except OSError:
            self.token = ""
        if len(self.token) < 12:
            self.token = secrets.token_urlsafe(16)
            with open(TOKEN_FILE, "w") as f:
                f.write(self.token)
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _allowed(self):
                try:
                    if not ipaddress.ip_address(self.client_address[0]).is_private:
                        self._send(403, json.dumps({"error": "LAN only"}))
                        return False
                except ValueError:
                    return False
                return True

            def _authed(self):
                ip, now = self.client_address[0], time.time()
                hub.fails[ip] = [t for t in hub.fails[ip] if now - t < 60]
                if len(hub.fails[ip]) >= 10:
                    self._send(429, json.dumps({"error": "too many attempts"}))
                    return False
                if not hmac.compare_digest(self.headers.get("X-Token", ""), hub.token):
                    hub.fails[ip].append(now)
                    self._send(401, json.dumps({"error": "bad token"}))
                    return False
                return True

            def do_GET(self):
                if not self._allowed():
                    return
                if self.path == "/":
                    return self._send(200, PAGE, "text/html")
                if self.path == "/status":
                    if not self._authed():
                        return
                    v = hub.p.voice
                    try:
                        import psutil
                        cpu, mem = round(psutil.cpu_percent()), round(psutil.virtual_memory().percent)
                    except Exception:
                        cpu = mem = "n/a"
                    return self._send(200, json.dumps({"state": v.state, "online": hub.p.offline.online(),
                                                       "cpu": cpu, "mem": mem, "last_reply": v.last_reply}))
                self._send(404, json.dumps({"error": "not found"}))

            def do_POST(self):
                if not self._allowed():
                    return
                if self.path != "/cmd":
                    return self._send(404, json.dumps({"error": "not found"}))
                if not self._authed():
                    return
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if not 0 < n <= 2048:
                        raise ValueError
                    text = str(json.loads(self.rfile.read(n)).get("text", ""))
                except Exception:
                    return self._send(400, json.dumps({"error": "bad request"}))
                self._send(200, json.dumps({"reply": hub.p.run_text(text)}))

        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as e:
            return False, f"couldn't open port {self.port}: {e}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return True, self.url()

    def stop(self):
        if not self.server:
            return False
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        return True


# ============================================================================
# PLUS: voice router + install()
# ============================================================================

class Plus:
    def __init__(self, voice, hologram):
        self.voice, self.holo = voice, hologram
        self.log = getattr(voice, "_on_log", print)
        self.lock = threading.RLock()
        self.offline = Offline(self)
        self.search = SemanticSearch(self)
        self.guard = Guard(self)
        self.cowork = Cowork(self)
        self.hub = Hub(self)

    def _say(self, text):
        self.voice._speak(text)

    def run_text(self, text):
        """Entry used by the LAN hub: same pipeline as a spoken command."""
        text = re.sub(r"^\s*aurora[,\s]+", "", text.strip()[:300], flags=re.I)
        if not text:
            return "Empty command."
        if any(b in text.lower() for b in HUB_BLOCKED):
            return "That command isn't allowed from the network hub."
        with self.lock:
            self.log(f"HUB: {text}")
            try:
                return self.voice.run_text(text)
            except Exception as e:
                return f"Error: {type(e).__name__}: {e}"

    def _do_search(self, q):
        hits = self.search.query(q)
        if not hits:
            hits = sc.vault_search(q)        # fall back to the keyword vault
        if hits is None:
            self._say("I haven't indexed any files yet. Add a folder to my vault, then say index my files.")
        elif not hits:
            self._say(f"I didn't find anything about {q}.")
        else:
            card = " | ".join(f"{os.path.basename(h['path'])}: {h['text'][:90]}" for h in hits)
            self.holo.show_info_card(f"Files about: {q}", card)
            self.log(f"SEARCH: '{q}' -> {[os.path.basename(h['path']) for h in hits]}")
            self._say(f"Best match is {os.path.basename(hits[0]['path'])}. "
                      f"I found {len(hits)} file{'s' if len(hits) != 1 else ''}, details on screen.")

    def route(self, text):
        """Returns True if handled (and already spoken)."""
        t = text.lower().strip()

        m = re.match(r"co[- ]?work\b[,:\s]*(.*)$", t)
        if m:
            arg = m.group(1).strip()
            if arg in ("stop", "cancel"):
                self._say(self.cowork.stop())
            elif arg in ("", "status"):
                self._say(f"Cowork is {'working: ' + self.cowork.status if self.cowork.busy else 'idle'}."
                          + (" Last ZIP is in cowork exports." if self.cowork.last_zip and not self.cowork.busy else ""))
            else:
                self._say(self.cowork.start(arg))
            return True

        if re.search(r"\b(?:network|lan|local)\s+hub\b|\bhub\s+(?:address|status)\b", t):
            if re.search(r"\b(stop|close|disable|shut)\b", t):
                self._say("Network hub stopped." if self.hub.stop() else "The hub isn't running.")
            elif re.search(r"\b(start|open|enable|launch)\b", t) or not self.hub.server:
                ok, info = self.hub.start()
                if ok:
                    self.holo.show_info_card("Network hub", f"Open on your phone (same wifi): {info}")
                    self.log(f"HUB: {info}")
                    self._say("Network hub is on. The address is on the screen. Keep the link private.")
                else:
                    self._say(info)
            else:
                self.holo.show_info_card("Network hub", f"Open on your phone (same wifi): {self.hub.url()}")
                self._say("The hub is running. The address is on the screen.")
            return True

        if "offline mode" in t:
            t2 = t.replace("offline", "")
            if re.search(r"\b(off|disable|exit|stop|leave)\b", t2):
                self.offline.force = False
                self._say("Offline mode forced off. I'll use the AI when it's reachable.")
            elif re.search(r"\b(on|enable|start|enter|turn)\b", t2):
                self.offline.force = True
                self._say("Offline mode on. Local commands, tools and file search only.")
            else:
                mode = "offline" if not self.offline.online() or self.voice.client is None else "online"
                self._say(f"I'm currently {mode}." + (" No AI key is set." if self.voice.client is None else ""))
            return True

        m = re.search(r"add (?:the )?folder (.+?)(?: to (?:my )?(?:vault|search|files|index))?$", t)
        if m:
            ok, info = sc.vault_add_folder(m.group(1))
            self._say(f"Added {os.path.basename(info) or info}. Say index my files." if ok else info)
            return True

        if re.search(r"\b(?:index|reindex|rebuild|scan)\s+(?:my\s+)?(?:files|documents|folders|vault|search)", t):
            if not sc._load_json("vault_folders.json", []):
                self._say("No folders yet. Say add folder, then the path.")
                return True
            self._say("Indexing your files.")

            def work():
                nf, nc = self.search.build()
                sc.vault_build(self.log)
                self.log(f"SEARCH: indexed {nf} files, {nc} chunks")
                self.voice.speak_now(f"Indexed {nf} files.")
            threading.Thread(target=work, daemon=True).start()
            return True

        m = (re.search(r"(?:search|find|look for)\s+(?:my\s+)?(?:files|documents|docs|notes)\s+"
                       r"(?:about|for|on|mentioning|containing)\s+(.+)$", t)
             or re.search(r"semantic search\s+(?:for\s+)?(.+)$", t)
             or re.search(r"what did i (?:write|save|note)\s+(?:down\s+)?about\s+(.+)$", t))
        if m:
            self._do_search(m.group(1).strip())
            return True
        return False


def install(voice, hologram=None):
    """Wires Aurora Plus into an existing VoiceAssistant. Call BEFORE voice.start()."""
    plus = Plus(voice, hologram or voice.hologram)
    voice.plus = plus

    # brain: local answers when there's no AI key or no internet
    voice.brain = OfflineAwareBrain(plus, voice.brain)

    # keyword-vault searches (LLM tool + voice) also use the semantic index when it exists
    orig_vault_search = sc.vault_search

    def vault_search(query, top_k=3):
        hits = plus.search.query(query, top_k) if plus.search.has_index() else None
        return hits or orig_vault_search(query, top_k)
    sc.vault_search = vault_search

    # neural TTS only when online; otherwise the local voice
    voice.speech.use_edge = plus.offline.online

    # speech-to-text: Google when online, PocketSphinx when offline (if installed)
    rec = getattr(voice, "recognizer", None)
    if rec is not None:
        google = rec.recognize_google

        def hybrid(audio, *a, **k):
            if plus.offline.online():
                try:
                    return google(audio, *a, **k)
                except sr.RequestError:
                    pass
            try:
                return rec.recognize_sphinx(audio)
            except sr.RequestError:
                raise sr.UnknownValueError()      # sphinx not installed: treat as "nothing heard"
        rec.recognize_google = hybrid

    voice._on_log("PLUS: Cowork, Network Hub, Offline Mode, File Search ready")
    return plus
