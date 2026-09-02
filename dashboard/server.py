"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import socket
import string
import time
from pathlib import Path

from actions import nucleus_hierarchy
from actions import buildpro_data as bd
from actions import daily_deal_finders as ddf
from actions import business_intelligence as biz_intel
from actions import opportunity_engine as opp_engine
from actions import strategic_objective as strategic_obj
from actions import google_auth
from actions import twilio_integration as twilio
from actions import hubspot_integration
from actions import buffer_integration
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions.system_monitor import get_system_status

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
# Plain-HTTP fallback alias for /3d when SSL is enabled (mirrors the
# PORT + 1 HTTPS alias pattern in get_manual_url() below). Defined here so
# it's a single source of truth; the plain-HTTP listener itself is not yet
# implemented — see test_dashboard_plain_http.py, which exercises the port
# math and will keep failing past collection until that listener exists.
HTTP_PORT   = PORT + 2
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _verify_to_health_status(verify: dict) -> str:
    """Maps a hubspot_integration.verify_hubspot()/buffer_integration.
    verify_buffer() live-check result onto the same CONFIGURED /
    AUTHENTICATED / NOT_CONFIGURED / AUTH_FAILED / RUNTIME_FAILED
    vocabulary _integration_health() uses, so a module opened from the
    Nucleus tree and the polled system panel never disagree on what
    "working" means. A 401/403 from the live call is a real auth
    rejection (AUTH_FAILED); anything else (network error, 5xx, an
    unexpected exception) is RUNTIME_FAILED — distinct because one means
    the token is wrong and the other means we couldn't tell."""
    if verify.get("verified"):
        return "AUTHENTICATED"
    if not verify.get("configured"):
        return "NOT_CONFIGURED"
    status = str(verify.get("status") or "")
    if status.startswith("UNAVAILABLE:401") or status.startswith("UNAVAILABLE:403"):
        return "AUTH_FAILED"
    return "RUNTIME_FAILED"


def _verify_twilio_signature(req, form: dict) -> bool:
    """Independently implements Twilio's documented request-signing
    algorithm (HMAC-SHA1 over the request URL + sorted form param
    key+value pairs, base64-encoded), compared against the
    X-Twilio-Signature header. False-closed on every failure mode
    (not configured, missing header, wrong token, tampered form,
    wrong URL, or any unexpected error) — the only routes in this file
    meant to receive traffic from the open internet, so refusing is
    always the safe default, never a fabricated pass."""
    try:
        if not twilio.is_configured():
            return False
        headers = getattr(req, "headers", None) or {}
        sig = headers.get("x-twilio-signature")
        if not sig:
            return False
        auth_token = twilio._load_twilio_config().get("auth_token", "")
        if not auth_token:
            return False
        data = str(req.url)
        for key in sorted(form.keys()):
            data += key + str(form[key])
        expected = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        # ── /3d spatial command center state ────────────────────────────
        self._nucleus_id = "jarvis"           # current focused Nucleus
        self._nucleus_back_stack: list[str] = []  # for the "back" nav action
        self._3d_ws_clients: set[WebSocket] = set()  # separate channel from the phone command center's _clients
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    # ── /3d spatial command center — navigation state ───────────────────

    def apply_navigation(self, action: str, nucleus_id: str = "") -> dict:
        """The single place server-side navigation state actually changes —
        called by the HTTP /3d/api/command route, the /3d/ws websocket, and
        (via core/headless/tool_registry.py's navigate_command_center tool)
        Gemini itself, so all three ways of moving JARVIS around the
        Nucleus tree stay in sync. 'status' never mutates state — it's a
        read of wherever navigation already put us."""
        if action == "open" and nucleus_id:
            if nucleus_id != self._nucleus_id:
                self._nucleus_back_stack.append(self._nucleus_id)
            self._nucleus_id = nucleus_id
        elif action == "back":
            if self._nucleus_back_stack:
                self._nucleus_id = self._nucleus_back_stack.pop()
        elif action == "home":
            self._nucleus_id = "jarvis"
            self._nucleus_back_stack = []
        # "status" (or any unrecognized action) falls through and just
        # reports current state without mutating it.
        node = nucleus_hierarchy.get_hierarchy_node(self._nucleus_id) or {"id": "jarvis", "name": "Jarvis"}
        return {
            "type": "navigate", "action": action,
            "nucleus_id": self._nucleus_id, "name": node.get("name", self._nucleus_id),
            "ts": time.time(),
        }

    async def _broadcast_3d(self, payload: dict) -> None:
        """Push to every connected /3d/ws client — used for navigation
        pushes and (see dashboard_bridge.py / twilio webhooks) toast
        notifications. Best-effort: a dead socket is dropped, never
        allowed to break the broadcast for everyone else."""
        dead = set()
        for ws in self._3d_ws_clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self._3d_ws_clients -= dead

    async def broadcast_nav(self, payload: dict) -> None:
        """Public alias for _broadcast_3d — the name main.py's
        navigate_command_center tool and _broadcast_orb_state call,
        matching what a live desktop/voice session (this dashboard's
        actual caller) conceptually does: push a navigation or state
        event to the spatial scene."""
        await self._broadcast_3d(payload)

    # ── /3d spatial command center — the real chat bridge ────────────────
    # Root-cause fix (Phase 2, priority 1): dashboard_bridge.py drains
    # _command_queue into run_chat_turn() but only ever broadcasts to the
    # PHONE _clients pool via self.broadcast() — never to _3d_ws_clients —
    # so the 3D dock's command box only ever showed "Sent." This handler is
    # the fix: same run_chat_turn() brain, same on_status/on_tool_event
    # hooks that already exist for exactly this purpose, just pushed out
    # over _broadcast_3d instead. No second chat implementation.

    async def _handle_3d_chat(self, text: str, history: list) -> dict:
        from core.headless.ui import run_chat_turn
        from actions.agent_orchestrator import orchestrator as agent_orchestrator

        async def _on_status(label: str) -> None:
            await self._broadcast_3d({"type": "jarvis_state", "state": "thinking", "label": label, "ts": time.time()})

        async def _on_tool_event(event: dict) -> None:
            ev_type = event.get("type")
            tool_name = event.get("name", "tool")
            if ev_type == "tool_start":
                await self._broadcast_3d({
                    "type": "jarvis_state", "state": "executing",
                    "label": f"Running {tool_name}...", "ts": time.time(),
                })
                await self._broadcast_3d({
                    "type": "activity", "source": "tool", "kind": "tool_start",
                    "message": f"{tool_name} started", "ts": time.time(),
                })
            elif ev_type == "tool_end":
                ok = event.get("ok", True)
                await self._broadcast_3d({
                    "type": "activity", "source": "tool",
                    "kind": "tool_end" if ok else "tool_error",
                    "message": f"{tool_name} {'completed' if ok else 'failed'}",
                    "ts": time.time(),
                })

        def _pending_count() -> int:
            try:
                return agent_orchestrator.summary().get("pending_approval_count", 0)
            except Exception:
                return 0

        before_pending = _pending_count()
        await self._broadcast_3d({"type": "jarvis_state", "state": "thinking", "label": "Thinking...", "ts": time.time()})

        error = None
        try:
            reply, tool_calls = await run_chat_turn(text, history, on_status=_on_status, on_tool_event=_on_tool_event)
        except Exception as e:
            reply = f"Error: {e}"
            tool_calls = []
            error = str(e)

        after_pending = _pending_count()

        if error:
            await self._broadcast_3d({"type": "jarvis_state", "state": "error", "label": reply[:160], "ts": time.time()})
            await self._broadcast_3d({
                "type": "activity", "source": "chat", "kind": "error",
                "message": reply[:200], "ts": time.time(),
            })
        elif after_pending > before_pending:
            await self._broadcast_3d({"type": "jarvis_state", "state": "waiting_for_approval", "label": "Waiting for approval...", "ts": time.time()})
            await self._broadcast_3d({
                "type": "activity", "source": "approval", "kind": "approval_requested",
                "message": "A new task needs your approval.", "ts": time.time(),
            })
        else:
            await self._broadcast_3d({"type": "jarvis_state", "state": "success", "label": "Done", "ts": time.time()})
            await self._broadcast_3d({
                "type": "activity", "source": "chat", "kind": "reply",
                "message": reply[:200], "ts": time.time(),
            })

        return {"reply": reply, "tool_calls": tool_calls, "error": error, "pending_approval_count": after_pending}

    # ── /3d spatial command center — per-module live data ───────────────
    # Each of these honestly reports NOT_CONFIGURED/empty rather than
    # fabricating data — matching the same standard as every tool in
    # core/headless/tool_executor.py. Wraps real, already-tested modules;
    # nothing here reimplements business logic that exists elsewhere.

    def _communications_placeholder(self) -> bool:
        """Live-computed, not read from the static hierarchy config: True
        (dim/"coming soon" in the 3D scene) unless Twilio is genuinely
        configured right now. False-closed if the twilio module itself
        failed to import (or was monkeypatched to None in a test)."""
        if twilio is None:
            return True
        try:
            return twilio.get_status().get("state") != "CONFIGURED"
        except Exception:
            return True

    def _overlay_communications_placeholder(self, children: list[dict]) -> list[dict]:
        placeholder = self._communications_placeholder()
        return [{**c, "placeholder": placeholder} for c in children]

    def _module_data(self, module_id: str, query: str = "", note: str = "") -> dict:
        if module_id == "buildpro":
            return self._module_buildpro()
        if module_id == "candidates":
            results = bd.list_candidates(limit=100)
            return {"results": results, "summary": f"{len(results)} candidate(s) on file."}
        if module_id == "clients":
            results = bd.list_clients(limit=100)
            return {"results": results, "summary": f"{len(results)} client(s) on file."}
        if module_id == "prospects":
            results = bd.list_clients(status="prospect", limit=100)
            return {"results": results, "summary": f"{len(results)} prospect(s) on file."}
        if module_id == "jobs":
            results = bd.list_jobs(limit=100)
            return {"results": results, "summary": f"{len(results)} job(s) on file."}
        if module_id == "matches":
            results = bd.top_matches(limit=100)
            return {"results": results, "summary": f"{len(results)} candidate/job match(es) on file."}
        if module_id in ("ddf", "deals"):
            return self._module_ddf()
        if module_id == "communications":
            return self._module_communications()
        if module_id == "system":
            return self._module_system()
        if module_id == "files":
            return self._module_files(query)
        if module_id == "knowledge":
            return self._module_knowledge(query, note)
        if module_id == "reports":
            return self._module_reports()
        if module_id == "email":
            return self._module_email()
        if module_id == "calendar":
            return self._module_calendar()
        if module_id in ("hubspot", "hubspot-contacts", "hubspot-companies"):
            return self._module_hubspot()
        if module_id in ("social", "buffer", "social-channels"):
            return self._module_social()
        # Any other real hierarchy node (careerrocket, personal, etc.) —
        # honest placeholder rather than a 404, since it's still a real,
        # navigable Nucleus even before it has its own live data source.
        return {"summary": "No live data source connected for this Nucleus yet."}

    def _module_buildpro(self) -> dict:
        candidates = bd.list_candidates(limit=200)
        clients = bd.list_clients(limit=200)
        jobs = bd.list_jobs(status="open", limit=200)
        prospects = [c for c in clients if c.get("status") == "prospect"]
        matches = bd.top_matches(limit=200)
        qualified = [m for m in matches if (m.get("match_score") or 0) >= 70]
        top_scores = qualified[:5]  # top_matches() is already ordered by score DESC
        data = {
            "buildpro_recruiting": {
                "candidate_count": len(candidates),
                "client_count": len(clients),
                "active_jobs": len(jobs),
                "prospect_count": len(prospects),
                "qualified_matches": len(qualified),
                "highest_match_scores": [
                    {"candidate_name": m.get("candidate_name"), "job_title": m.get("job_title"),
                     "match_score": m.get("match_score")}
                    for m in top_scores
                ],
            },
            "buildpro_followups": {
                "candidates": bd.list_candidates_needing_followup(),
                "clients": bd.list_clients_needing_followup(),
            },
        }
        try:
            data["business_intelligence"] = biz_intel.summary("buildpro")
        except Exception:
            data["business_intelligence"] = {"counts": {}}
        try:
            data["top_opportunities"] = opp_engine.rank_opportunities(business="buildpro", limit=5)
        except Exception:
            data["top_opportunities"] = []
        return data

    def _module_ddf(self) -> dict:
        data = {"top_products": ddf.get_top_products(limit=5)}
        try:
            data["business_intelligence"] = biz_intel.summary("ddf")
        except Exception:
            data["business_intelligence"] = {"counts": {}}
        return data

    def _module_communications(self) -> dict:
        try:
            status = twilio.get_status() if twilio is not None else {"state": "NOT_CONFIGURED", "detail": "Twilio module unavailable."}
        except Exception as e:
            status = {"state": "ERROR", "detail": str(e)}
        configured = status.get("state") == "CONFIGURED"
        children = nucleus_hierarchy.get_hierarchy_children("communications")
        # "contacts" has no backing store regardless of Twilio config — a
        # placeholder either way, distinct from the other channels which
        # flip live once Twilio is actually configured.
        channels = {}
        for c in children:
            key = c["id"].replace("comm-", "")
            channels[key] = {"status": "placeholder" if key == "contacts" or not configured else "live"}
        return {
            "configured": configured,
            "status": status.get("state", "NOT_CONFIGURED"),
            "detail": status.get("detail"),
            "channels": channels,
            "children": self._overlay_communications_placeholder(children),
        }

    def _module_system(self) -> dict:
        data = dict(get_system_status())
        try:
            data["agents"] = agent_orchestrator.summary()
        except Exception:
            data["agents"] = {"agents": []}
        try:
            data["strategic_objective"] = strategic_obj.get_objective_status()
        except Exception:
            data["strategic_objective"] = {}
        try:
            data["business_intelligence"] = biz_intel.summary()
        except Exception:
            data["business_intelligence"] = {"counts": {}}
        data["integration_health"] = self._integration_health()
        return data

    def _integration_health(self) -> dict:
        """Cheap, local/config-presence health for every system the /3d
        spec calls out.

        2026-09-02 reliability audit finding: the previous vocabulary used
        "CONNECTED" for both "a credential is present" and "this in-process
        component is actually running" — Lee's own words, "Do not label an
        integration 'working' merely because an environment variable
        exists," is exactly the gap that conflation created (e.g. Buffer/
        HubSpot showed CONNECTED purely because BUFFER_TOKEN/HUBSPOT_TOKEN
        were set, never because either was actually verified live).
        Vocabulary is now:
          CONFIGURED    — a credential/setting is present; not verified live
                          here (this feeds a 30s-polled panel — a live
                          network call per integration per poll isn't
                          reasonable; see _module_hubspot/_module_social for
                          the live AUTHENTICATED/AUTH_FAILED check, run only
                          when a user actually opens that module).
          AUTHENTICATED — a live credential check has actually succeeded
                          (Gmail/Calendar's OAuth status is already a real,
                          fast local check, not a network round-trip).
          OPERATIONAL   — an in-process component with no external
                          credential to check; it's already proven to work
                          by the fact that this call is executing.
          NOT_CONFIGURED — no credential/setting present.
          AUTH_FAILED   — a credential is present but the live check
                          rejected it.
          RUNTIME_FAILED — an unexpected error while checking, distinct
                          from "not configured" or "auth failed."
        Never returns a credential value — presence only, same standard as
        core/headless/config.py's summarize()."""
        from core.headless import config as headless_config
        health: dict[str, str] = {}
        health["jarvis_backend"] = "OPERATIONAL"     # this call running IS the backend
        health["render"] = "OPERATIONAL"             # same process — if this runs, Render is serving it
        health["tool_executor"] = "OPERATIONAL"      # importable/running in this same process
        health["ollama"] = "CONFIGURED" if headless_config.OLLAMA_API_KEY else "NOT_CONFIGURED"
        health["cartesia"] = (
            "CONFIGURED" if (headless_config.CARTESIA_API_KEY and headless_config.CARTESIA_VOICE_ID)
            else "NOT_CONFIGURED"
        )
        health["buffer"] = "CONFIGURED" if headless_config.BUFFER_TOKEN else "NOT_CONFIGURED"
        health["hubspot"] = "CONFIGURED" if headless_config.HUBSPOT_TOKEN else "NOT_CONFIGURED"
        try:
            g_status = google_auth.get_credential_status()
            if g_status.get("authorized"):
                health["gmail"] = "AUTHENTICATED"
                health["calendar"] = "AUTHENTICATED"
            elif g_status.get("credential_file") == "present":
                health["gmail"] = "AUTH_FAILED"
                health["calendar"] = "AUTH_FAILED"
            else:
                health["gmail"] = "NOT_CONFIGURED"
                health["calendar"] = "NOT_CONFIGURED"
        except Exception:
            health["gmail"] = "RUNTIME_FAILED"
            health["calendar"] = "RUNTIME_FAILED"
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{headless_config.DB_PATH}?mode=ro", uri=True, timeout=2)
            conn.execute("SELECT 1")
            conn.close()
            health["database"] = "OPERATIONAL"
        except Exception:
            health["database"] = "RUNTIME_FAILED"
        try:
            from memory.memory_manager import load_memory
            load_memory()
            health["memory"] = "OPERATIONAL"
        except Exception:
            health["memory"] = "RUNTIME_FAILED"
        try:
            from core.headless.obsidian import ObsidianVault
            vstatus = ObsidianVault().status()
            if not vstatus.get("configured"):
                health["knowledge"] = "NOT_CONFIGURED"
            elif vstatus.get("exists"):
                health["knowledge"] = "OPERATIONAL"
            else:
                health["knowledge"] = "RUNTIME_FAILED"
        except Exception:
            health["knowledge"] = "RUNTIME_FAILED"
        return health

    def _module_hubspot(self) -> dict:
        """Real HubSpot module — verify_hubspot() is a live, lightweight
        auth check (see actions/hubspot_integration.py), then a small
        recent-records pull. User-initiated (opened from the Nucleus tree),
        not polled, so a live call here is fine. NOT_AVAILABLE is reported
        honestly rather than fabricating data when HubSpot isn't
        configured or the live check fails."""
        try:
            verify = hubspot_integration.verify_hubspot()
        except Exception as e:
            verify = {"configured": False, "verified": False, "status": f"ERROR:{e}"}
        data: dict = {"status": verify, "health_status": _verify_to_health_status(verify)}
        if not verify.get("verified"):
            data["recent_contacts"] = []
            data["recent_companies"] = []
            data["note"] = "NOT AVAILABLE" if not verify.get("configured") else "NOT AVAILABLE — HubSpot check failed."
            return data
        try:
            contacts = hubspot_integration.get_contacts(limit=10)
            data["recent_contacts"] = contacts.get("results", []) if contacts.get("ok") else []
        except Exception:
            data["recent_contacts"] = []
        try:
            companies = hubspot_integration.get_companies(limit=10)
            data["recent_companies"] = companies.get("results", []) if companies.get("ok") else []
        except Exception:
            data["recent_companies"] = []
        return data

    def _module_social(self) -> dict:
        """Real Buffer/social module — status, connected channels, and
        (live schema introspection) which scheduled-post operations this
        account's token genuinely supports. Never returns the Buffer token
        anywhere in this payload — get_channels()/verify_buffer() don't
        carry it, and channel dicts are defensively stripped of anything
        that looks like a credential field before being sent to the
        browser. User-initiated (opened from the Nucleus tree), not
        polled."""
        try:
            verify = buffer_integration.verify_buffer()
        except Exception as e:
            verify = {"configured": False, "verified": False, "status": f"ERROR:{e}"}
        data: dict = {"status": verify, "health_status": _verify_to_health_status(verify)}
        if not verify.get("verified"):
            data["channels"] = []
            data["scheduling_capabilities"] = {"configured": verify.get("configured", False), "status": verify.get("status"), "capabilities": {}}
            data["note"] = "NOT AVAILABLE" if not verify.get("configured") else "NOT AVAILABLE — Buffer check failed."
            return data
        try:
            channels_result = buffer_integration.get_channels()
            raw_channels = channels_result.get("channels", []) if channels_result.get("status") == "VERIFIED" else []
        except Exception:
            raw_channels = []
        _CRED_KEYS = {"token", "accesstoken", "access_token", "secret", "apikey", "api_key"}
        data["channels"] = [
            {k: v for k, v in c.items() if k.lower() not in _CRED_KEYS}
            for c in raw_channels
        ]
        try:
            data["scheduling_capabilities"] = buffer_integration.discover_scheduling_capabilities()
        except Exception as e:
            data["scheduling_capabilities"] = {"configured": True, "status": f"ERROR:{e}", "capabilities": {}}
        return data

    def _module_files(self, query: str) -> dict:
        results = []
        try:
            if query.strip():
                for p in BASE_DIR.rglob(f"*{query.strip()}*"):
                    if any(part in (".git", "__pycache__", "node_modules", ".venv") for part in p.parts):
                        continue
                    if p.is_file():
                        results.append({"name": p.name, "path": str(p.relative_to(BASE_DIR))})
                    if len(results) >= 50:
                        break
        except Exception:
            pass
        recent_files: list[dict] = []
        try:
            recent_files = [
                {"name": f.name, "size": f.stat().st_size}
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )[:10]
            ]
        except Exception:
            pass
        return {"results": results, "recent_files": recent_files}

    def _module_knowledge(self, query: str = "", note: str = "") -> dict:
        """JARVIS Brain — a thin read-only wrapper over the existing
        core.headless.obsidian.ObsidianVault (no new retrieval system:
        list_notes()/search_notes()/read_note() are the same methods the
        obsidian LLM tool already uses). Distinct from _module_files:
        this is specifically the Obsidian knowledge vault (default
        knowledge/JARVIS Brain/), not a general filesystem search.
        Every field below comes straight from the vault — an unconfigured
        vault, an empty vault, or a not-found note is reported honestly,
        never fabricated."""
        from core.headless.obsidian import ObsidianVault
        vault = ObsidianVault()
        vault_path = vault.status().get("path")
        if not vault.is_configured():
            return {
                "configured": False, "vault_path": vault_path,
                "summary": "No JARVIS Brain vault configured.", "notes": [],
            }
        if note:
            content = vault.read_note(note)
            found = content is not None
            return {
                "configured": True, "vault_path": vault_path,
                "note": {"path": note, "content": content, "found": found},
                "summary": f"Reading {note}" if found else f"{note!r} not found in the JARVIS Brain.",
            }
        if query.strip():
            results = vault.search_notes(query)
            return {
                "configured": True, "vault_path": vault_path,
                "query": query, "results": results,
                "summary": f'{len(results)} note(s) match "{query}".',
            }
        notes = vault.list_notes()
        return {
            "configured": True, "vault_path": vault_path,
            "notes": notes,
            "summary": f"{len(notes)} note(s) in the JARVIS Brain." if notes else "The JARVIS Brain vault is empty.",
        }

    def _module_reports(self) -> dict:
        report_files: list[dict] = []
        try:
            reports_dir = BASE_DIR / "data" / "reports"
            if reports_dir.is_dir():
                report_files = [{"name": f.name} for f in sorted(reports_dir.iterdir(), reverse=True)[:20]]
        except Exception:
            pass
        return {"system_status": get_system_status(), "report_files": report_files}

    def _module_email(self) -> dict:
        try:
            status = google_auth.get_credential_status()
        except Exception as e:
            status = {"authorized": False, "credential_file": "unknown", "error": str(e)}
        return {"configured": bool(status.get("authorized")), "status": status}

    def _module_calendar(self) -> dict:
        try:
            status = google_auth.get_credential_status()
        except Exception as e:
            status = {"authorized": False, "credential_file": "unknown", "error": str(e)}
        return {"configured": bool(status.get("authorized")), "status": status}

    def _overview_payload(self) -> dict:
        root = nucleus_hierarchy.get_hierarchy_root()
        hierarchy_children = list(root.get("children", []))
        modules = []
        for domain in hierarchy_children:
            module_id = "deals" if domain["id"] == "ddf" else domain["id"]
            modules.append({"id": module_id, "name": domain.get("name", domain["id"])})
        # Live-computed communications placeholder overlay applied to the
        # hierarchy tree the 3D scene actually renders from — see
        # _overlay_communications_placeholder's docstring.
        hierarchy = dict(root)
        hierarchy["children"] = [
            {**d, "children": self._overlay_communications_placeholder(d.get("children", []))}
            if d["id"] == "communications" else d
            for d in hierarchy_children
        ]
        try:
            strategic_objective = strategic_obj.get_objective_status()
        except Exception:
            strategic_objective = {}
        return {
            "focus": self._nucleus_id if self._nucleus_id != "jarvis" else "core",
            "modules": modules,
            "summary": {"module_count": len(modules)},
            "strategic_objective": strategic_objective,
            "hierarchy": hierarchy,
        }

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        # ── /3d spatial command center ───────────────────────────────────
        # /3d/api/* and /3d/ws accept three credentials — see .env.example's
        # JARVIS_API_TOKEN comment, which already documented the pairing-key
        # acceptance below as intended but it was never actually wired in:
        def _3d_auth(req: Request) -> bool:
            """Accepts: (1) the JARVIS_API_TOKEN Bearer auth used by /3d/api/*
            clients and tests, (2) the desktop app's own pairing-key/PIN
            session token (self._tokens — the same credential /api/command
            already accepts), so main.py's raw DashboardServer — which never
            mounts /ui and typically has no JARVIS_API_TOKEN set — has a
            working browser path to /3d at all, or (3) the same browser
            session cookie /ui already sets on login — so a person who's
            simply logged into the normal JARVIS interface can click through
            to /3d and it just works, with no separate login step and no
            token ever appearing in a URL or needing to be typed in twice."""
            from core.headless import config as headless_config
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if tok and headless_config.API_TOKEN and tok == headless_config.API_TOKEN:
                return True
            if tok and tok in self._tokens:
                return True
            from core.headless.ui import _session_valid, COOKIE_NAME
            return _session_valid(req.cookies.get(COOKIE_NAME))

        _THREE_D_DIR = STATIC_DIR / "3d"

        @app.get("/3d", response_class=HTMLResponse)
        async def three_d_page(req: Request):
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return HTMLResponse((_THREE_D_DIR / "index.html").read_text(encoding="utf-8"))

        @app.get("/3d/sw.js")
        async def three_d_service_worker(req: Request):
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            # Served at /3d/sw.js (top-level scope), NOT /3d/assets/sw.js —
            # a service worker's default scope is the directory it's served
            # from, so this must sit at /3d/ to cover /3d/* rather than
            # only /3d/assets/*.
            return FileResponse(str(_THREE_D_DIR / "sw.js"), media_type="application/javascript")

        @app.get("/3d/assets/{asset_path:path}")
        async def three_d_assets(asset_path: str, req: Request):
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = (_THREE_D_DIR / asset_path).resolve()
            if _THREE_D_DIR.resolve() not in safe.parents and safe != _THREE_D_DIR.resolve():
                return JSONResponse({"error": "Not found"}, status_code=404)
            if not safe.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            media_type = None
            if safe.suffix == ".js":
                media_type = "application/javascript"
            elif safe.suffix == ".json":
                media_type = "application/json"
            elif safe.suffix == ".svg":
                media_type = "image/svg+xml"
            return FileResponse(str(safe), media_type=media_type)

        @app.get("/3d/api/overview")
        async def three_d_overview(req: Request):
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse(self._overview_payload())

        @app.get("/3d/api/module/{module_id}")
        async def three_d_module(module_id: str, req: Request, query: str = "", note: str = ""):
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            node = nucleus_hierarchy.get_hierarchy_node(module_id) or {"id": module_id, "name": module_id}
            children = nucleus_hierarchy.get_hierarchy_children(module_id)
            path = nucleus_hierarchy.get_hierarchy_path(module_id)
            data = self._module_data(module_id, query, note)
            if module_id == "communications":
                data.setdefault("children", self._overlay_communications_placeholder(children))
            else:
                data.setdefault("node", node)
                data.setdefault("children", children)
                data.setdefault("path", path)
            return JSONResponse({"module": {"id": module_id, "name": node.get("name", module_id)}, "data": data})

        @app.post("/3d/api/command")
        async def three_d_command(req: Request):
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body."}, status_code=400)
            action = body.get("action", "")
            try:
                if action == "navigate":
                    result = self.apply_navigation(body.get("nav_action", "status"), body.get("nucleus_id", ""))
                    asyncio.create_task(self._broadcast_3d(result))
                elif action == "system_status":
                    result = get_system_status()
                elif action == "chat":
                    text = (body.get("text") or "").strip()
                    if not text:
                        return JSONResponse({"ok": False, "error": "No text provided."}, status_code=400)
                    history = body.get("history") or []
                    result = await self._handle_3d_chat(text, history)
                elif action == "speak":
                    text = (body.get("text") or "").strip()
                    if not text:
                        return JSONResponse({"ok": False, "error": "No text provided."}, status_code=400)
                    from core.headless.ui import synthesize_reply_audio
                    result = synthesize_reply_audio(text)
                elif action == "approve_task":
                    from actions.agent_orchestrator import orchestrator as agent_orchestrator
                    task_id = (body.get("task_id") or "").strip()
                    if not task_id:
                        return JSONResponse({"ok": False, "error": "No task_id provided."}, status_code=400)
                    try:
                        task = agent_orchestrator.approve_task(task_id)
                    except KeyError as e:
                        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
                    result = task.to_public_dict()
                    asyncio.create_task(self._broadcast_3d({
                        "type": "activity", "source": "approval", "kind": "approved",
                        "message": f"Approved: {result.get('description', task_id)}", "ts": time.time(),
                    }))
                    asyncio.create_task(self._broadcast_3d({"type": "jarvis_state", "state": "success", "label": "Task approved", "ts": time.time()}))
                elif action == "reject_task":
                    from actions.agent_orchestrator import orchestrator as agent_orchestrator
                    task_id = (body.get("task_id") or "").strip()
                    if not task_id:
                        return JSONResponse({"ok": False, "error": "No task_id provided."}, status_code=400)
                    try:
                        task = agent_orchestrator.reject_task(task_id)
                    except KeyError as e:
                        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
                    result = task.to_public_dict()
                    asyncio.create_task(self._broadcast_3d({
                        "type": "activity", "source": "approval", "kind": "rejected",
                        "message": f"Denied: {result.get('description', task_id)}", "ts": time.time(),
                    }))
                    asyncio.create_task(self._broadcast_3d({"type": "jarvis_state", "state": "idle", "label": "Task denied", "ts": time.time()}))
                else:
                    return JSONResponse({"ok": False, "error": f"Unknown command action: {action!r}"}, status_code=400)
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
            return JSONResponse({"ok": True, "result": result})

        @app.get("/3d/api/approvals")
        async def three_d_approvals(req: Request):
            """Real pending-approval queue for the 3D approval center — the
            same AgentOrchestrator PENDING_APPROVAL tasks /ui/api/tasks
            already exposes (cookie-only), surfaced here so a
            pairing-token/Bearer 3D session can read it too. 'reason' is
            the task's real description (AgentTask has no separate reason
            field — see orchestrator_api.py); 'risk' is the agent's actual
            permission_level (always EXECUTE for anything reaching this
            state) rather than an invented numeric score."""
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from actions.agent_orchestrator import orchestrator as agent_orchestrator, TaskStatus
            pending = [t for t in agent_orchestrator.list_tasks() if t.status == TaskStatus.PENDING_APPROVAL]
            pending.sort(key=lambda t: t.created_ts, reverse=True)
            out = []
            for t in pending:
                agent = agent_orchestrator.get_agent(t.agent_id)
                d = t.to_public_dict()
                d["agent_name"] = agent.name if agent else t.agent_id
                d["system"] = agent.nucleus_id if agent else "unknown"
                d["risk"] = agent.permission_level.value if agent else "unknown"
                out.append(d)
            return JSONResponse({"approvals": out})

        @app.get("/3d/api/activity")
        async def three_d_activity(req: Request, limit: int = 30):
            """Real recent-activity history for the 3D feed to load on
            open, instead of resetting empty every time the page loads —
            reuses status_api.activity() (agent events + audit log +
            proactive triggers, already time-sorted) directly rather than
            re-implementing a second activity feed."""
            if not _3d_auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core.headless import status_api
            return JSONResponse(status_api.activity(limit=limit))

        @app.websocket("/3d/ws")
        async def three_d_ws(websocket: WebSocket, token: str = ""):
            from core.headless import config as headless_config
            from core.headless.ui import _session_valid, COOKIE_NAME
            tok = token.strip()
            token_ok = bool(tok) and bool(headless_config.API_TOKEN) and tok == headless_config.API_TOKEN
            # Pairing-key/PIN session token — same credential /api/command
            # already accepts — matches _3d_auth above (see its docstring).
            pairing_ok = bool(tok) and tok in self._tokens
            # A same-origin browser tab already sends the /ui session
            # cookie automatically on the websocket handshake, same as it
            # would on any other same-origin request — no separate login
            # needed, matching the HTTP routes' _3d_auth above.
            cookie_ok = _session_valid(websocket.cookies.get(COOKIE_NAME))
            if not (token_ok or pairing_ok or cookie_ok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._3d_ws_clients.add(websocket)
            try:
                await websocket.send_json(self.apply_navigation("status"))
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "navigate":
                        result = self.apply_navigation(data.get("action", "status"), data.get("nucleus_id", ""))
                        await self._broadcast_3d(result)
            except WebSocketDisconnect:
                pass
            finally:
                self._3d_ws_clients.discard(websocket)

        # ── Twilio webhooks ───────────────────────────────────────────────
        # The only routes in this file meant to receive traffic from the
        # open internet (a real Twilio number's webhooks) — every one is
        # signature-verified before anything is recorded or broadcast.
        # See docs/DASHBOARD_SECURITY.md.

        async def _reject_unless_signed(req: Request, form: dict) -> JSONResponse | None:
            if not _verify_twilio_signature(req, form):
                return JSONResponse({"error": "Invalid or missing Twilio signature."}, status_code=403)
            return None

        @app.post("/twilio/voice")
        async def twilio_voice(req: Request):
            form = dict((await req.form()))
            rejected = await _reject_unless_signed(req, form)
            if rejected is not None:
                return rejected
            twilio._log(
                direction="inbound", kind="call", sid=form.get("CallSid"),
                from_number=form.get("From"), to_number=form.get("To"),
                status=form.get("CallStatus"),
            )
            asyncio.create_task(self._broadcast_3d({
                "type": "notification",
                "text": f"Incoming call from {form.get('From', 'unknown')}",
                "ts": time.time(),
            }))
            return Response(content=twilio.voicemail_twiml(), media_type="application/xml")

        @app.post("/twilio/sms")
        async def twilio_sms(req: Request):
            form = dict((await req.form()))
            rejected = await _reject_unless_signed(req, form)
            if rejected is not None:
                return rejected
            twilio._log(
                direction="inbound", kind="sms", sid=form.get("MessageSid"),
                from_number=form.get("From"), to_number=form.get("To"), body=form.get("Body"),
            )
            asyncio.create_task(self._broadcast_3d({
                "type": "notification",
                "text": f"New SMS from {form.get('From', 'unknown')}: {(form.get('Body') or '')[:80]}",
                "ts": time.time(),
            }))
            return JSONResponse({"ok": True})

        @app.post("/twilio/status")
        async def twilio_status(req: Request):
            form = dict((await req.form()))
            rejected = await _reject_unless_signed(req, form)
            if rejected is not None:
                return rejected
            sid = form.get("MessageSid") or form.get("CallSid")
            if sid:
                twilio.update_by_sid(sid, status=form.get("MessageStatus") or form.get("CallStatus"))
            return JSONResponse({"ok": True})

        @app.post("/twilio/transcription")
        async def twilio_transcription(req: Request):
            form = dict((await req.form()))
            rejected = await _reject_unless_signed(req, form)
            if rejected is not None:
                return rejected
            sid = form.get("CallSid")
            if sid:
                twilio.update_by_sid(sid, transcription=form.get("TranscriptionText"))
            return JSONResponse({"ok": True})

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
