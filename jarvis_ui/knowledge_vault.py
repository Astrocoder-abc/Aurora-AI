"""
Local Knowledge Vault — indexes text files from folders you point Aurora
at, fully on-disk, no cloud calls. Folders list: vault_folders.json.
Index: vault_index.json (both next to api_key.txt).

Place this file at: jarvis_ui/knowledge_vault.py

"Aurora, add folder C:/Users/you/notes to my vault"
"Aurora, rebuild my vault"
"Aurora, find everything I have about Arduino"
"Aurora, what did I write about my Mars project"

Supports .txt/.md/.py/.json/.csv/.log, plus .pdf if pypdf or PyPDF2 is
installed (silently skipped otherwise). Search is a dependency-free
keyword-overlap scorer — instant, no embeddings, no network.
"""
import json
import os
import re

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FOLDERS_FILE = os.path.join(BASE_DIR, "vault_folders.json")
INDEX_FILE = os.path.join(BASE_DIR, "vault_index.json")

TEXT_EXTS = {".txt", ".md", ".py", ".json", ".csv", ".log"}
CHUNK_WORDS = 120

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is",
    "are", "was", "were", "with", "that", "this", "it", "i", "my", "me",
    "have", "has", "did", "do", "does", "what", "everything", "about",
    "write", "wrote", "find", "you", "your",
}


def _words(text):
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS]


def load_folders():
    if os.path.exists(FOLDERS_FILE):
        try:
            with open(FOLDERS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def add_folder(path):
    path = os.path.normpath(path.strip().strip('"'))
    if not os.path.isdir(path):
        return False, f"'{path}' isn't a folder I can find."
    folders = load_folders()
    if path not in folders:
        folders.append(path)
        with open(FOLDERS_FILE, "w") as f:
            json.dump(folders, f, indent=2)
    return True, path


def _extract_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return ""
    try:
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _read_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext not in TEXT_EXTS:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _chunk(text):
    words = text.split()
    for i in range(0, len(words), CHUNK_WORDS):
        piece = " ".join(words[i:i + CHUNK_WORDS]).strip()
        if piece:
            yield piece


def build_index(on_log=None):
    """Returns (file_count, chunk_count)."""
    log = on_log or (lambda msg: None)
    entries, file_count = [], 0
    for folder in load_folders():
        if not os.path.isdir(folder):
            log(f"VAULT: folder missing, skipping — {folder}")
            continue
        for root, _, files in os.walk(folder):
            for fname in files:
                path = os.path.join(root, fname)
                text = _read_file(path)
                if not text.strip():
                    continue
                file_count += 1
                for chunk in _chunk(text):
                    entries.append({"path": path, "text": chunk, "words": _words(chunk)})
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    log(f"VAULT: indexed {file_count} file(s), {len(entries)} chunk(s)")
    return file_count, len(entries)


def _load_index():
    if not os.path.exists(INDEX_FILE):
        return None
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def search(query, top_k=3):
    """Returns None if no index exists yet, else a best-first list of
    {path, text, score}."""
    entries = _load_index()
    if entries is None:
        return None
    q_words = set(_words(query))
    if not q_words:
        return []
    q_lower = query.lower().strip()

    scored = []
    for e in entries:
        overlap = q_words & set(e["words"])
        if not overlap:
            continue
        score = len(overlap) + (2 if q_lower and q_lower in e["text"].lower() else 0)
        scored.append((score, e))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"path": e["path"], "text": e["text"], "score": s} for s, e in scored[:top_k]]


def vault_status():
    folders = load_folders()
    entries = _load_index()
    n_files = len({e["path"] for e in entries}) if entries else 0
    n_chunks = len(entries) if entries else 0
    return folders, n_files, n_chunks
