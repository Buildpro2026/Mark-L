"""Non-destructive JARVIS health check.

Verifies the local environment is set up correctly WITHOUT calling any
external API, sending any message, modifying any file, or printing any
secret value. Safe to run at any time, including in CI/test contexts.

Usage:
    .venv/Scripts/python.exe scripts/health_check.py
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

REQUIRED_MODULES = [
    "PyQt6", "sounddevice", "google.genai", "fastapi", "uvicorn",
    "requests", "psutil", "cv2", "PIL",
]

# key -> (path relative to BASE_DIR, required top-level key(s) that should be present)
CONFIG_CHECKS = {
    "gemini_api_key": (Path("config/api_keys.json"), ["gemini_api_key"]),
}
OPTIONAL_CONFIG_KEYS = ["hubspot_token", "twilio", "buffer_token", "airtable_token"]


class CheckResult:
    def __init__(self, name: str, ok: bool, detail: str):
        self.name = name
        self.ok = ok
        self.detail = detail


def check_python_version() -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    return CheckResult("python_version", ok, f"{v.major}.{v.minor}.{v.micro}")


def check_modules() -> list[CheckResult]:
    results = []
    for mod in REQUIRED_MODULES:
        spec = None
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        results.append(CheckResult(f"module:{mod}", spec is not None, "importable" if spec else "MISSING"))
    return results


def check_api_keys_config() -> list[CheckResult]:
    results = []
    path = BASE_DIR / "config" / "api_keys.json"
    if not path.exists():
        results.append(CheckResult("config/api_keys.json", False, "file does not exist"))
        return results
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        results.append(CheckResult("config/api_keys.json", False, f"invalid JSON: {type(e).__name__}"))
        return results

    gemini_present = bool(data.get("gemini_api_key"))
    results.append(CheckResult("gemini_api_key", gemini_present,
                                "present" if gemini_present else "MISSING (required to start JARVIS)"))

    for key in OPTIONAL_CONFIG_KEYS:
        present = bool(data.get(key))
        results.append(CheckResult(f"optional:{key}", present,
                                    "configured" if present else "not configured (integration will report NOT CONFIGURED)"))
    return results


def check_google_oauth() -> list[CheckResult]:
    results = []
    google_dir = BASE_DIR / "config" / "google"
    token = google_dir / "token.json"
    has_client_secret = any(google_dir.glob("client_secret_*.json")) if google_dir.exists() else False
    results.append(CheckResult("google_oauth:client_secret", has_client_secret,
                                "present" if has_client_secret else "missing (Gmail/Calendar OAuth setup incomplete)"))
    results.append(CheckResult("google_oauth:token", token.exists(),
                                "present (cached)" if token.exists() else "missing (will need interactive auth on first use)"))
    return results


def check_data_db() -> CheckResult:
    db_path = BASE_DIR / "data" / "jarvis2.db"
    if not db_path.exists():
        return CheckResult("data/jarvis2.db", True, "does not exist yet (created automatically on first write)")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        conn.close()
        return CheckResult("data/jarvis2.db", True, "opens cleanly (read-only check)")
    except Exception as e:
        return CheckResult("data/jarvis2.db", False, f"failed to open: {type(e).__name__}")


def check_memory_file() -> CheckResult:
    mem_path = BASE_DIR / "memory" / "long_term.json"
    if not mem_path.exists():
        return CheckResult("memory/long_term.json", True, "does not exist yet (created automatically)")
    try:
        json.loads(mem_path.read_text(encoding="utf-8"))
        return CheckResult("memory/long_term.json", True, "valid JSON")
    except Exception as e:
        return CheckResult("memory/long_term.json", False, f"invalid JSON: {type(e).__name__}")


def check_voice_provider_deps() -> list[CheckResult]:
    """Informational only (never a hard failure) — reports which TTS voice
    providers are actually usable given installed packages. Gemini voice
    always works (no extra deps); ElevenLabs/EdgeTTS need `miniaudio` to
    decode returned audio; Local (Kokoro) needs `kokoro` + `torch`."""
    results = []
    has_miniaudio = importlib.util.find_spec("miniaudio") is not None
    results.append(CheckResult(
        "voice:elevenlabs_playback", has_miniaudio,
        "miniaudio present — ElevenLabs/EdgeTTS playback works"
        if has_miniaudio else
        "miniaudio MISSING — ElevenLabs/EdgeTTS voice selection will fail to play audio (run: pip install miniaudio)",
    ))
    has_kokoro = importlib.util.find_spec("kokoro") is not None
    has_torch = importlib.util.find_spec("torch") is not None
    local_ok = has_kokoro and has_torch
    results.append(CheckResult(
        "voice:local_kokoro", local_ok,
        "kokoro + torch present — Local voice provider works"
        if local_ok else
        "kokoro/torch not installed — Local voice provider will fall back to Gemini voice automatically",
    ))
    return results


def check_dashboard_ports() -> list[CheckResult]:
    """Confirms the dashboard's default ports are free to bind (does NOT
    start the server, does NOT make any network request)."""
    results = []
    for port in (8000, 8001, 8002):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.bind(("0.0.0.0", port))
            free = True
        except OSError:
            free = False
        finally:
            s.close()
        results.append(CheckResult(f"port:{port}", True,
                                    "free" if free else "IN USE (dashboard may already be running, or another app holds it)"))
    return results


def run_health_check() -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(check_python_version())
    results.extend(check_modules())
    results.extend(check_api_keys_config())
    results.extend(check_google_oauth())
    results.append(check_data_db())
    results.append(check_memory_file())
    results.extend(check_voice_provider_deps())
    results.extend(check_dashboard_ports())
    return results


def main() -> int:
    results = run_health_check()
    hard_fail = False
    print("JARVIS health check (read-only, no external calls)\n")
    for r in results:
        # Missing optional integrations/voice providers/ports-in-use are
        # informational, not failures — e.g. Local (Kokoro) voice falling
        # back to Gemini voice automatically is expected behavior, not a bug.
        is_hard_requirement = not (r.name.startswith("optional:") or r.name.startswith("port:")
                                    or r.name.startswith("voice:")
                                    or "does not exist yet" in r.detail)
        marker = "OK  " if r.ok else ("--  " if not is_hard_requirement else "FAIL")
        print(f"[{marker}] {r.name}: {r.detail}")
        if not r.ok and is_hard_requirement:
            hard_fail = True
    print()
    if hard_fail:
        print("Result: one or more required checks failed — see FAIL lines above.")
        return 1
    print("Result: all required checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
