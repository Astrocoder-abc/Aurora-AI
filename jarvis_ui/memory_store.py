"""Persistent user data lives in <project>/memory/ (contacts, calendar, notes, timeline, skills, face data...).

Credentials and tuning files (api_key.txt, phone_pin.txt, *_model.txt, mic_index.txt, ...) stay in the project root.
Files left in the root by older versions are moved into memory/ automatically, once per process.
"""
import os
import shutil

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
MEMORY_DIRNAME = "memory"

LEGACY_NAMES = (
    "contacts.json", "wifi_networks.json", "code_projects.json", "vault_folders.json", "vault_index.json",
    "semantic_index.json", "calendar_config.json", "calendar_local.json", "email_config.json", "email_drafts.json",
    "study_data.json", "skills.json", "notes.txt", "timeline_events.jsonl", "task_state.json",
    "sentinel_profile.json", "face_data",
)
_migrated = set()


def migrate_legacy_files(base=None):
    """Moves known data files/folders from the project root into memory/. Never overwrites; returns moved names."""
    base = os.path.abspath(base or BASE)
    dest = os.path.join(base, MEMORY_DIRNAME)
    moved = []
    for name in LEGACY_NAMES:
        src, dst = os.path.join(base, name), os.path.join(dest, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.makedirs(dest, exist_ok=True)
                shutil.move(src, dst)
                moved.append(name)
            except OSError:
                pass
    return moved


def memory_path(name, base=None):
    """Absolute path of a file/folder inside memory/ (folder is created and legacy files migrated on first use)."""
    base = os.path.abspath(base or BASE)
    if base not in _migrated:
        _migrated.add(base)
        migrate_legacy_files(base)
    os.makedirs(os.path.join(base, MEMORY_DIRNAME), exist_ok=True)
    return os.path.join(base, MEMORY_DIRNAME, name)
