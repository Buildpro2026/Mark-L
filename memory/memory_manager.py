import json
import os
import time
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
# Guards every read-modify-write cycle below as ONE atomic critical section
# (never acquired twice for a single logical operation) — a bare
# load-then-later-save pattern would let two concurrent callers each read
# the same starting state and one silently clobber the other's write.
_lock            = Lock()
MAX_VALUE_LENGTH = 380
MEMORY_MAX_CHARS = 2200

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }


def _backup_corrupt_file() -> None:
    """Renames (never deletes) an unparseable long_term.json so real
    remembered facts stay recoverable by hand instead of silently
    vanishing the moment something writes a fresh empty file over them."""
    try:
        backup = MEMORY_PATH.parent / f"long_term.corrupt-{int(time.time())}.json"
        MEMORY_PATH.replace(backup)
    except Exception:
        pass


def _read_raw() -> dict:
    """Loads memory, backing up (not discarding) unparseable content.
    Caller must already hold _lock. Never raises."""
    if not MEMORY_PATH.exists():
        return _empty_memory()
    try:
        raw = MEMORY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        print(f"[Memory] ⚠️ Load error: {e}")
        _backup_corrupt_file()
        return _empty_memory()
    if not isinstance(data, dict):
        _backup_corrupt_file()
        return _empty_memory()
    base = _empty_memory()
    for key in base:
        if key not in data:
            data[key] = {}
    return data


def _write_atomic(data: dict) -> None:
    """Caller must already hold _lock. Writes to a temp file in the same
    directory then os.replace()s it into place, so a crash mid-write never
    leaves a truncated/partial long_term.json."""
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MEMORY_PATH.parent / f"long_term.tmp-{os.getpid()}-{id(data)}-{time.time_ns()}"
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, MEMORY_PATH)


def load_memory() -> dict:
    with _lock:
        return _read_raw()


def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    with _lock:
        _write_atomic(memory)


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    with _lock:
        memory = _read_raw()
        if _recursive_update(memory, memory_update):
            memory = _trim_to_limit(memory)
            _write_atomic(memory)
            print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
        return memory

def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    lines = []

    identity  = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("")
        lines.append("Preferences:")
        for key, entry in list(prefs.items())[:15]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    projects = memory.get("projects", {})
    if projects:
        lines.append("")
        lines.append("Active Projects / Goals:")
        for key, entry in list(projects.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    rels = memory.get("relationships", {})
    if rels:
        lines.append("")
        lines.append("People in their life:")
        for key, entry in list(rels.items())[:10]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    wishes = memory.get("wishes", {})
    if wishes:
        lines.append("")
        lines.append("Wishes / Plans / Wants:")
        for key, entry in list(wishes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    notes = memory.get("notes", {})
    if notes:
        lines.append("")
        lines.append("Other notes:")
        for key, entry in list(notes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key}: {val}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    result = header + "\n".join(lines)
    if len(result) > 2000:
        result = result[:1997] + "…"

    return result + "\n"

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    with _lock:
        memory = _read_raw()
        cat    = memory.get(category, {})
        if key in cat:
            del cat[key]
            memory[category] = cat
            _write_atomic(memory)
            return f"Forgotten: {category}/{key}"
        return f"Not found: {category}/{key}"


forget_memory = forget


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    with _lock:
        memory   = _read_raw()
        sessions = memory.get("sessions", [])
        if not isinstance(sessions, list):
            sessions = []
        sessions.append(entry)
        memory["sessions"] = sessions[-_SESSION_MAX:]
        _write_atomic(memory)
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        memory   = _read_raw()
        sessions = memory.get("sessions", [])
        if not isinstance(sessions, list) or not sessions:
            return None
        entry = sessions.pop()          # remove the last entry
        memory["sessions"] = sessions
        _write_atomic(memory)
        return entry
