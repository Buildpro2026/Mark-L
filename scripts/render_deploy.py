"""One-shot Render deployment for jarvis-headless-core. Every secret is
entered directly by you at a hidden (`getpass`) prompt in your own shell —
never as a command-line argument, never echoed. Requires a real console on
stdin (checked up front); a Git-Bash/MSYS-style shell without one will exit
immediately rather than hang.

Nothing here ever prints a secret value — only key *names* and booleans
("set"/"not set"). API responses that could echo a secret back (env var
listings) are never dumped raw; only non-secret fields are extracted.

Usage:
    python scripts/render_deploy.py

You'll be prompted for:
  - RENDER_API_KEY   (unless already set in this shell's environment)
  - JARVIS_API_TOKEN (offered: auto-generate, or paste your own)
  - GEMINI_API_KEY   (required)
  - GOOGLE_TOKEN_JSON / GOOGLE_CLIENT_SECRET_JSON (optional — blank to skip;
    Gmail/Calendar just report NOT_CONFIGURED in the cloud if skipped)

Safe to re-run: if a service named jarvis-headless-core already exists in
your Render workspace, this triggers a fresh deploy of it instead of
creating a duplicate.
"""
from __future__ import annotations

import ctypes
import getpass
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.render.com/v1"
SERVICE_NAME = "jarvis-headless-core"
REPO_URL = "https://github.com/Buildpro2026/Mark-L"
DEFAULT_BRANCH = "feature/jarvis-2"


def _load_dotenv() -> None:
    """Minimal .env loader (deliberately not python-dotenv, matching the
    lean-dependency convention in core/headless/config.py).

    Unlike that module, .env here WINS over an already-set process env
    var, not the other way around. config.py needs "real env wins"
    because it runs as the actual deployed service, where the cloud
    platform's real env vars must always beat a bundled .env fallback.
    This script is different: it's a local one-shot tool where the user
    manages secrets by editing .env directly, and a stale value already
    sitting in the current shell/process's environment (e.g. left over
    from earlier in a long-running session) must not silently shadow an
    intentional .env update. No-op if the file is missing or malformed —
    a bad .env must never block this script, since the whole point is to
    let the interactive prompts still work as a fallback."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value
    except Exception:
        pass

# Windows' getpass() reads keystrokes via the msvcrt console API, which only
# works if a real Win32 console is attached. Some shell wrappers (e.g.
# Git-Bash/MSYS-style terminals) don't provide one — in that case msvcrt
# blocks forever with zero output, indistinguishable from "just slow". This
# checks for a real console up front so we fail fast and visibly instead of
# hanging silently.
def _has_real_console() -> bool:
    if sys.platform != "win32":
        return sys.stdin.isatty()
    try:
        STD_INPUT_HANDLE = -10
        handle = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:
        return False


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=None, capture_output=True, text=True, check=True).stdout.strip()


def _prompt_secret(label: str, env_name: str, required: bool, allow_generate: bool = False) -> str | None:
    existing = os.environ.get(env_name)
    if existing:
        print(f"  {env_name}: using value already set (shell env or .env).", flush=True)
        return existing
    if allow_generate:
        choice = input(f"  {label} — press Enter to auto-generate a new one, or type 'p' to paste your own: ").strip().lower()
        if choice != "p":
            val = secrets.token_urlsafe(32)
            print(f"  {env_name}: auto-generated (not displayed).", flush=True)
            return val
    # Only reachable when env_name wasn't found in the shell env or .env —
    # i.e. we're actually about to prompt. Check for a real console here,
    # lazily, rather than upfront in main(): that way a fully-.env-backed
    # run never trips this even under a console-less shell.
    if not _has_real_console():
        print(
            f"  {env_name} not found in the environment or .env, and this shell has no real "
            "console attached — hidden input isn't safe here, so I can't prompt for it.",
            flush=True,
        )
        if required:
            print(
                f"  {env_name} is required — aborting. Add it to .env, or run this script from "
                "a native PowerShell/cmd.exe console instead.",
                flush=True,
            )
            sys.exit(1)
        print(f"  Skipping optional {env_name}.", flush=True)
        return None
    val = getpass.getpass(f"  {label} (input hidden): ").strip()
    if not val and required:
        print(f"  {env_name} is required — aborting.", flush=True)
        sys.exit(1)
    return val or None


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}


def _get(api_key: str, path: str, **kw):
    r = requests.get(f"{API_BASE}{path}", headers=_headers(api_key), timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def _post(api_key: str, path: str, body: dict):
    r = requests.post(f"{API_BASE}{path}", headers=_headers(api_key), json=body, timeout=30)
    if not r.ok:
        # Render error responses are JSON with a "message" field — safe to
        # print, this is our own request failing, not a secret echo.
        print(f"  Render API error {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return r.json()


def get_owner_id(api_key: str) -> str:
    owners = _get(api_key, "/owners")
    if not owners:
        print("No Render workspace/owner found for this API key. Aborting.")
        sys.exit(1)
    if len(owners) > 1:
        print("Multiple Render owners/workspaces found:")
        for o in owners:
            owner = o.get("owner", o)
            print(f"  - {owner.get('id')}  {owner.get('name') or owner.get('email')}")
        oid = input("Paste the ownerId to use: ").strip()
        return oid
    owner = owners[0].get("owner", owners[0])
    print(f"  Using Render workspace: {owner.get('name') or owner.get('email')} ({owner.get('id')})")
    return owner["id"]


def find_service(api_key: str, name: str) -> dict | None:
    results = _get(api_key, "/services", params={"name": name, "limit": 20})
    for entry in results:
        svc = entry.get("service", entry)
        if svc.get("name") == name:
            return svc
    return None


def create_service(api_key: str, owner_id: str, branch: str, env_pairs: list[dict]) -> dict:
    # Free-tier proof-of-life (J4 decision): plan "free", no disk. Render's
    # free web services don't support a persistent disk at all, and one
    # costs money regardless of plan — so DATA_DIR is left to fall back to
    # config.py's local-dev default (BASE_DIR/"data") inside the container,
    # which is plain ephemeral storage: fine for boot and for exercising
    # the app during a short test, wiped on every restart/redeploy/sleep
    # cycle. Not a persistence story for anything durable — see the
    # deployment report's "Persistence limitation" note.
    body = {
        "type": "web_service",
        "name": SERVICE_NAME,
        "ownerId": owner_id,
        "repo": REPO_URL,
        "branch": branch,
        "serviceDetails": {
            "runtime": "python",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "python -m core.headless_main",
            },
            "healthCheckPath": "/health",
            "plan": "free",
        },
        "envVars": env_pairs,
    }
    resp = _post(api_key, "/services", body)
    svc = resp.get("service", resp)
    print(f"  Created service {svc.get('id')} ({SERVICE_NAME}).")
    return svc


def trigger_deploy(api_key: str, service_id: str) -> str:
    resp = _post(api_key, f"/services/{service_id}/deploys", {})
    deploy = resp.get("deploy", resp)
    return deploy["id"]


def latest_deploy_id(api_key: str, service_id: str) -> str | None:
    deploys = _get(api_key, f"/services/{service_id}/deploys", params={"limit": 1})
    if not deploys:
        return None
    return deploys[0].get("deploy", deploys[0])["id"]


def poll_deploy(api_key: str, service_id: str, deploy_id: str, timeout_s: int = 600) -> str:
    terminal = {"live", "build_failed", "update_failed", "canceled", "deactivated"}
    start = time.time()
    last_status = None
    while time.time() - start < timeout_s:
        resp = _get(api_key, f"/services/{service_id}/deploys/{deploy_id}")
        deploy = resp.get("deploy", resp)
        status = deploy.get("status")
        if status != last_status:
            print(f"  Deploy status: {status}")
            last_status = status
        if status in terminal:
            return status
        time.sleep(10)
    print("  Timed out waiting for deploy to finish (still building?). Check the Render dashboard.")
    return "timeout"


def service_url(api_key: str, service_id: str) -> str:
    resp = _get(api_key, f"/services/{service_id}")
    svc = resp.get("service", resp)
    details = svc.get("serviceDetails", {})
    url = details.get("url")
    if url:
        return url.rstrip("/")
    return f"https://{SERVICE_NAME}.onrender.com"


def smoke_test(base_url: str) -> dict:
    r = requests.get(f"{base_url}/health", timeout=30)
    r.raise_for_status()
    return r.json()


def auth_test(base_url: str, token: str) -> tuple[int, int]:
    unauth = requests.get(f"{base_url}/api/status", timeout=30)
    authed = requests.get(f"{base_url}/api/status", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return unauth.status_code, authed.status_code


def main() -> None:
    # Auto-flush every print() immediately (stdout is fully buffered by
    # default when it isn't a real terminal, which hides progress until
    # the buffer fills or the process exits — misleading during an
    # interactive run like this one).
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    print("== Render deployment: jarvis-headless-core ==\n")

    _load_dotenv()
    print("Step 0/6 — .env check (presence only, values never printed)")
    for name in (
        "RENDER_API_KEY", "JARVIS_API_TOKEN", "GEMINI_API_KEY", "HUBSPOT_TOKEN",
        "BUFFER_TOKEN", "APOLLO_TOKEN", "ANTHROPIC_TOKEN",
    ):
        print(f"  {name}: {'set' if os.environ.get(name) else 'NOT SET'}", flush=True)

    print("\nStep 1/6 — Render authentication")
    render_key = _prompt_secret("RENDER_API_KEY", "RENDER_API_KEY", required=True)

    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    except Exception:
        branch = DEFAULT_BRANCH
    print(f"  Deploying branch: {branch}")

    owner_id = get_owner_id(render_key)

    print("\nStep 2/6 — app secrets (only asked if creating a new service)")
    existing = find_service(render_key, SERVICE_NAME)

    if existing is None:
        jarvis_token = _prompt_secret("JARVIS_API_TOKEN — the bearer token this deployed API will require",
                                       "JARVIS_API_TOKEN", required=True, allow_generate=True)
        gemini_key = _prompt_secret("GEMINI_API_KEY", "GEMINI_API_KEY", required=True)
        google_token = _prompt_secret("GOOGLE_TOKEN_JSON (paste contents of config/google/token.json, or blank to skip)",
                                       "GOOGLE_TOKEN_JSON", required=False)
        google_secret = _prompt_secret("GOOGLE_CLIENT_SECRET_JSON (paste contents of client_secret_*.json, or blank to skip)",
                                        "GOOGLE_CLIENT_SECRET_JSON", required=False)
        # Business-integration secrets config.py already reads as env vars
        # (see its "J3 Part 2" section) — optional, each integration module
        # degrades to NOT_CONFIGURED on its own if left unset, same as local.
        hubspot_token = _prompt_secret("HUBSPOT_TOKEN (blank to skip)", "HUBSPOT_TOKEN", required=False)
        buffer_token = _prompt_secret("BUFFER_TOKEN (blank to skip)", "BUFFER_TOKEN", required=False)
        airtable_token = _prompt_secret("AIRTABLE_TOKEN (blank to skip)", "AIRTABLE_TOKEN", required=False)
        twilio_sid = _prompt_secret("TWILIO_ACCOUNT_SID (blank to skip)", "TWILIO_ACCOUNT_SID", required=False)
        twilio_auth = _prompt_secret("TWILIO_AUTH_TOKEN (blank to skip)", "TWILIO_AUTH_TOKEN", required=False)
        twilio_from = _prompt_secret("TWILIO_FROM_NUMBER (blank to skip)", "TWILIO_FROM_NUMBER", required=False)

        # No JARVIS_DATA_DIR here on purpose — Free plan has no disk to
        # mount, so DATA_DIR falls back to config.py's local-dev default
        # (plain ephemeral container storage). See create_service()'s
        # docstring comment.
        env_pairs = [
            {"key": "JARVIS_API_TOKEN", "value": jarvis_token},
            {"key": "GEMINI_API_KEY", "value": gemini_key},
            {"key": "JARVIS_HEADLESS_HOST", "value": "0.0.0.0"},
        ]
        for key, val in (
            ("GOOGLE_TOKEN_JSON", google_token),
            ("GOOGLE_CLIENT_SECRET_JSON", google_secret),
            ("HUBSPOT_TOKEN", hubspot_token),
            ("BUFFER_TOKEN", buffer_token),
            ("AIRTABLE_TOKEN", airtable_token),
            ("TWILIO_ACCOUNT_SID", twilio_sid),
            ("TWILIO_AUTH_TOKEN", twilio_auth),
            ("TWILIO_FROM_NUMBER", twilio_from),
        ):
            if val:
                env_pairs.append({"key": key, "value": val})

        print("\nStep 3/6 — creating the service")
        svc = create_service(render_key, owner_id, branch, env_pairs)
        service_id = svc["id"]
        # A newly created git-backed service auto-triggers its first deploy.
        time.sleep(3)
        deploy_id = latest_deploy_id(render_key, service_id)
    else:
        service_id = existing["id"]
        print(f"  Service already exists ({service_id}) — triggering a new deploy instead of creating a duplicate.")
        jarvis_token = _prompt_secret(
            "JARVIS_API_TOKEN — needed here only to run the live auth check below, not sent anywhere new "
            "(must match what's already configured on the existing service)",
            "JARVIS_API_TOKEN", required=True)
        print("\nStep 3/6 — triggering deploy")
        deploy_id = trigger_deploy(render_key, service_id)

    if not deploy_id:
        print("Could not determine the deploy to watch. Check the Render dashboard.")
        sys.exit(1)

    print("\nStep 4/6 — waiting for deploy to go live (this can take a few minutes)")
    status = poll_deploy(render_key, service_id, deploy_id)
    if status != "live":
        print(f"\nDeploy did not reach 'live' (final status: {status}). Stopping before smoke tests.")
        sys.exit(1)

    base_url = service_url(render_key, service_id)
    print(f"\nStep 5/6 — live smoke test against {base_url}")
    health = smoke_test(base_url)
    print(f"  /health -> {health}")

    print("\nStep 6/6 — authentication verification")
    unauth_code, authed_code = auth_test(base_url, jarvis_token)
    print(f"  GET /api/status without token -> {unauth_code} (expect 401 or 503)")
    print(f"  GET /api/status with token    -> {authed_code} (expect 200)")

    print("\n== Final report ==")
    print(f"Service:              {SERVICE_NAME} ({service_id})")
    print(f"Branch deployed:      {branch}")
    print(f"URL:                  {base_url}")
    print(f"Deploy status:        {status}")
    print(f"Health check:         {'OK' if health.get('status') == 'ok' else 'UNEXPECTED — see above'}")
    print(f"DB reachable:         {health.get('db_reachable')}")
    print(f"Persistent data dir:  {health.get('data_dir')} (exists: {health.get('data_dir_exists')})")
    print(f"Gemini key set:       {health.get('gemini_api_key_env_set')}")
    print(f"Auth gate working:    {unauth_code in (401, 503) and authed_code == 200}")


if __name__ == "__main__":
    main()
