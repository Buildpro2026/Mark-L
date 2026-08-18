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
import json
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
    from fastapi.staticfiles import StaticFiles
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
HTTP_PORT   = PORT + 2   # always-plain-HTTP fallback, only started when PORT itself is HTTPS-only
MAX_UPLOAD_MB = 500

from core.startup import is_port_free, get_logger as _get_lifecycle_logger
from core.headless import config as headless_config


def _report_port_conflict(port: int, what: str, exit_code=None) -> None:
    """Clear, non-crashing recovery message for a port that's already
    bound — the most common cause is a second JARVIS instance still
    running (or slow to fully exit) from a previous launch."""
    suffix = f" (uvicorn exit code {exit_code})" if exit_code is not None else ""
    msg = (
        f"[Dashboard] Port {port} is already in use{suffix} — skipping the {what} this run. "
        "This usually means another JARVIS instance is already running. If not, another "
        "application is using this port, or a previous JARVIS process didn't fully exit — "
        "check Task Manager for a leftover python.exe, close it, and restart."
    )
    print(msg)
    try:
        _get_lifecycle_logger().info(msg)
    except Exception:
        pass

try:
    from actions.daily_deal_finders import get_top_products
    from actions.file_controller import file_controller
    from actions.integrations import build_provider_status_payload, get_oauth_setup_hint, get_approval_gate
    from actions.nucleus_hierarchy import get_hierarchy_root, get_hierarchy_children, get_hierarchy_path, get_hierarchy_node
    from actions.open_app import open_app
    from actions.system_monitor import get_system_status
    from actions.agent_orchestrator import orchestrator as agent_orchestrator
    from actions import twilio_integration as twilio
    from actions import business_intelligence as biz_intel
    from actions import opportunity_engine as opp_engine
    from actions import buildpro_data
    from actions.strategic_objective import get_objective_status
except Exception:
    get_top_products = None
    file_controller = None
    build_provider_status_payload = None
    get_oauth_setup_hint = None
    get_approval_gate = None
    get_hierarchy_root = None
    get_hierarchy_children = None
    get_hierarchy_path = None
    get_hierarchy_node = None
    open_app = None
    get_system_status = None
    agent_orchestrator = None
    twilio = None
    biz_intel = None
    opp_engine = None
    buildpro_data = None
    get_objective_status = None


def _communications_configured() -> bool:
    """Whether Twilio is actually configured right now. Backs the dynamic
    placeholder overlay below — actions/twilio_integration.py is real,
    working code, so whether its category nodes render as live or as
    placeholders in the 3D UI must reflect current credential state, not a
    value hardcoded once in config/nucleus_config.json that would otherwise
    stay stale forever after credentials are added."""
    if twilio is None:
        return False
    try:
        return twilio.get_status()["state"] != "NOT_CONFIGURED"
    except Exception:
        return False


def _overlay_communications_placeholder(nodes: list) -> None:
    """Mutates `nodes` in place, setting 'placeholder' on any Communications
    category node (comm-phone/comm-sms/comm-calls/comm-contacts/
    comm-notifications) based on live Twilio-configured state. Applied
    wherever the hierarchy's communications children are served — see
    three_d_overview() and three_d_module()."""
    if not nodes:
        return
    configured = _communications_configured()
    for node in nodes:
        if isinstance(node, dict) and str(node.get("id", "")).startswith("comm-"):
            node["placeholder"] = not configured


def _verify_twilio_signature(req, form: dict) -> bool:
    """True only for a request that's genuinely from Twilio, signed with
    this account's real auth_token (X-Twilio-Signature, HMAC-SHA1 over the
    request URL + sorted form params — Twilio's documented algorithm).
    Refuses outright (no legitimate source exists) if Twilio isn't
    configured at all. Never raises — any unexpected shape fails closed
    (False), never open."""
    if twilio is None:
        return False
    try:
        auth_token = twilio._load_twilio_config().get("auth_token", "")
        if not auth_token:
            return False
        signature = req.headers.get("x-twilio-signature", "")
        if not signature:
            return False
        data = str(req.url)
        for key in sorted(form.keys()):
            data += key + str(form[key])
        computed = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


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


# ── Three.js (auto-download once, served locally) — same pattern as CryptoJS ──
_THREEJS_VERSION    = "0.160.0"
_THREEJS_VENDOR_DIR = STATIC_DIR / "3d" / "vendor"
_THREEJS_FILES = {
    "three.module.js": f"https://unpkg.com/three@{_THREEJS_VERSION}/build/three.module.js",
    "OrbitControls.js": f"https://unpkg.com/three@{_THREEJS_VERSION}/examples/jsm/controls/OrbitControls.js",
}


def _ensure_threejs_vendor() -> None:
    missing = {name: url for name, url in _THREEJS_FILES.items()
               if not (_THREEJS_VENDOR_DIR / name).exists()}
    if not missing:
        return
    try:
        import urllib.request
        _THREEJS_VENDOR_DIR.mkdir(parents=True, exist_ok=True)
        for name, url in missing.items():
            print(f"[Dashboard] Downloading {name} (one-time setup)…")
            urllib.request.urlretrieve(url, str(_THREEJS_VENDOR_DIR / name))
        print("[Dashboard] Three.js cached — the 3D command center will serve it locally from now on.")
    except Exception as e:
        print(f"[Dashboard] Three.js download failed: {e}")
        print("[Dashboard] The 3D command center will not render until dashboard/static/3d/vendor/ is populated.")


_ensure_threejs_vendor()


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
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        # ── 3D spatial command center: shared nav state + its own ws channel.
        # Was unauthenticated (local-trust) — closed per the J1 audit finding
        # that this let anyone on the LAN launch arbitrary applications via
        # /3d/api/command's open_app action with zero credentials. Now gated
        # by the same _auth()/_token_valid() check as the rest of the API
        # surface — see /3d/api/overview, /3d/api/module, /3d/api/command,
        # and /3d/ws below.
        self._nav_clients: set[WebSocket] = set()
        self._nucleus_id                  = "jarvis"
        self._nucleus_back_stack: list[str] = []
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

    # ── 3D spatial command center: navigation state ─────────────────────────

    def apply_navigation(self, action: str, nucleus_id: str = "") -> dict:
        """Single source of truth for "what Nucleus is the 3D command center
        currently showing" — mutated by both the voice path (main.py calls
        this directly) and mouse clicks (frontend posts to /3d/api/command),
        so the two stay in sync and 'what am I looking at' is always correct.
        """
        action = (action or "open").strip().lower()
        if action == "open" and nucleus_id:
            if self._nucleus_id and self._nucleus_id != nucleus_id:
                self._nucleus_back_stack.append(self._nucleus_id)
            self._nucleus_id = nucleus_id
        elif action == "back":
            self._nucleus_id = self._nucleus_back_stack.pop() if self._nucleus_back_stack else "jarvis"
        elif action == "home":
            self._nucleus_back_stack.clear()
            self._nucleus_id = "jarvis"
        # "status" (or anything else) reports current state without changing it

        node = get_hierarchy_node(self._nucleus_id) if get_hierarchy_node is not None else None
        return {
            "type": "navigate",
            "action": action,
            "nucleus_id": self._nucleus_id,
            "name": (node.get("name") if node else self._nucleus_id),
            "ts": time.time(),
        }

    async def broadcast_nav(self, msg: dict) -> None:
        """Push a navigate/jarvis_state event to every open /3d/ws client
        (the spatial command center page) — a separate channel from the
        phone-pairing /ws, but gated by the same token check (see the
        /3d/ws handler's query-param auth, since a browser can't set a
        custom header on a WebSocket handshake)."""
        dead: set[WebSocket] = set()
        for ws in list(self._nav_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._nav_clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return _token_valid(tok)

        def _token_valid(tok: str) -> bool:
            """A valid credential is any of:
            1. A live pairing-session token (self._tokens — the phone QR/key
               flow, in-memory, lost on process restart by design).
            2. The static JARVIS_API_TOKEN itself (core/headless/config.py)
               — a persistent pre-shared secret for non-interactive callers.
            3. A stateless, HMAC-signed session token minted by /login for a
               browser that authenticated with (2) — see core.headless.ui's
               _new_session()/_session_valid(). This is what the cloud login
               flow actually hands the browser: it survives a Render
               restart (no server-side state to lose, same reasoning as
               core/headless/ui.py's own session cookie) without the
               browser having to hold the raw API token indefinitely.
            Used for both header-based auth (_auth, above) and the
            query-param auth /3d/ws, /ws, /ws/phone-audio, /uploads/{filename}
            need (browsers can't set custom WebSocket/redirect headers)."""
            if not tok:
                return False
            if tok in self._tokens:
                return True
            if headless_config.API_TOKEN and hmac.compare_digest(tok, headless_config.API_TOKEN):
                return True
            from core.headless.ui import _session_valid as _ui_session_valid
            return _ui_session_valid(tok)

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

        @app.get("/3d", response_class=HTMLResponse)
        async def three_d_command_center():
            return FileResponse(str(STATIC_DIR / "3d" / "index.html"))

        # Served at the top-level /3d/sw.js path (not under /3d/assets) so its
        # default service-worker scope is "/3d/*" — a worker served from
        # /3d/assets/sw.js would only control /3d/assets/*, not the app itself.
        @app.get("/3d/sw.js")
        async def three_d_service_worker():
            return FileResponse(str(STATIC_DIR / "3d" / "sw.js"), media_type="application/javascript")

        # Serves app.js and vendor/three.module.js + vendor/OrbitControls.js
        # at /3d/assets/... — the spatial scene's static JS, not API data.
        app.mount("/3d/assets", StaticFiles(directory=str(STATIC_DIR / "3d")), name="3d-assets")

        @app.get("/3d/api/overview")
        async def three_d_overview(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            deals = []
            if get_top_products is not None:
                try:
                    deals = get_top_products(limit=3)
                except Exception:
                    deals = []

            hierarchy = get_hierarchy_root() if get_hierarchy_root is not None else None
            if hierarchy is not None:
                for domain in hierarchy.get("children", []):
                    if domain.get("id") == "communications":
                        _overlay_communications_placeholder(domain.get("children", []))
                        break
            modules = [
                {
                    "id": "core",
                    "title": "Core",
                    "desc": "Central Jarvis intelligence",
                    "actions": ["Listen", "Think", "Act"],
                    "status": "Listening and coordinating",
                },
                {
                    "id": "buildpro",
                    "title": "BuildPro",
                    "desc": "Recruiting, matches, follow-ups",
                    "actions": ["Candidates", "Jobs", "Clients"],
                    "status": "Ready for candidate review",
                },
                {
                    "id": "deals",
                    "title": "Daily Deals",
                    "desc": "Product discovery and affiliate posts",
                    "actions": ["Discover", "Score", "Draft"],
                    "status": f"{len(deals)} active deal(s) in queue" if deals else "No deals yet",
                },
                {
                    "id": "email",
                    "title": "Email",
                    "desc": "Business communications",
                    "actions": ["Inbox", "Prioritize", "Approve"],
                    "status": "Ready to triage messages",
                },
                {
                    "id": "calendar",
                    "title": "Calendar",
                    "desc": "Interviews and meetings",
                    "actions": ["Schedule", "Remind", "Track"],
                    "status": "Agenda ready",
                },
                {
                    "id": "files",
                    "title": "Files",
                    "desc": "Workspace and attachments",
                    "actions": ["Open", "Search", "Review"],
                    "status": "Workspace accessible",
                },
                {
                    "id": "reports",
                    "title": "Reports",
                    "desc": "Executive updates and status",
                    "actions": ["Morning brief", "Health", "History"],
                    "status": "System summary available",
                },
            ]

            if hierarchy is not None:
                seen_ids = {module.get("id") for module in modules if module.get("id")}
                hierarchy_nodes = [
                    {
                        "id": hierarchy.get("id", "jarvis"),
                        "title": hierarchy.get("name", "Jarvis"),
                        "desc": "Central nucleus for all domains",
                        "actions": ["Navigate", "Expand", "Return"],
                        "status": "Central intelligence available",
                    },
                    *[
                        {
                            "id": child.get("id"),
                            "title": child.get("name"),
                            "desc": f"{child.get('kind', 'domain')} nucleus",
                            "actions": ["Expand", "Inspect", "Focus"],
                            "status": "Ready",
                        }
                        for child in hierarchy.get("children", [])
                        if child.get("id") not in seen_ids
                    ],
                ]
                modules.extend(hierarchy_nodes)

            objective = None
            if get_objective_status is not None:
                try:
                    objective = get_objective_status()
                except Exception:
                    objective = None

            return JSONResponse({
                "focus": "core",
                "summary": {
                    "module_count": len(modules),
                    "status": "operational",
                    "active_focus": "Core",
                    "deal_count": len(deals),
                },
                "hierarchy": hierarchy,
                "modules": modules,
                "strategic_objective": objective,
            })

        @app.get("/3d/api/module/{module_id}")
        async def three_d_module(module_id: str, req: Request, query: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            module = None
            data = {}

            # Hierarchy context is attached first, for ANY module_id that
            # resolves to a real Nucleus — the spatial UI needs node/children
            # /path to render child objects regardless of what domain-specific
            # data (if any) gets layered on below.
            if get_hierarchy_node is not None:
                hierarchy_node = get_hierarchy_node(module_id)
                if hierarchy_node is not None:
                    data["node"] = hierarchy_node
                    data["children"] = get_hierarchy_children(module_id) if get_hierarchy_children is not None else []
                    data["path"] = get_hierarchy_path(module_id) if get_hierarchy_path is not None else []
                    _overlay_communications_placeholder(data["children"])

            if module_id in ("deals", "ddf"):
                if get_top_products is not None:
                    try:
                        data["top_products"] = get_top_products(limit=3)
                    except Exception as exc:
                        data["top_products"] = []
                        data["error"] = str(exc)
                data["summary"] = "Daily Deal Finder data is available for navigation"
                if biz_intel is not None:
                    try:
                        data["business_intelligence"] = biz_intel.summary(business="ddf")
                    except Exception as exc:
                        data["business_intelligence"] = {"error": str(exc)}
                if opp_engine is not None:
                    try:
                        data["top_opportunities"] = opp_engine.rank_opportunities(business="ddf", limit=3)
                    except Exception as exc:
                        data["top_opportunities"] = []
            elif module_id == "files":
                if file_controller is not None:
                    try:
                        data["results"] = file_controller(parameters={"action": "find", "path": "home", "name": query or "", "max_results": 8})
                        recent = file_controller(parameters={"action": "list", "path": "downloads"})
                        data["recent_files"] = recent
                    except Exception as exc:
                        data["error"] = str(exc)
                else:
                    data.update({"results": "File controller unavailable", "recent_files": "No recent files"})
            elif module_id == "reports":
                system_status = {}
                report_files = []
                if get_system_status is not None:
                    try:
                        system_status = get_system_status()
                    except Exception as exc:
                        system_status = {"error": str(exc)}
                try:
                    report_files = [
                        p.name for p in BASE_DIR.glob("**/*report*.txt")
                    ][:6]
                except Exception:
                    report_files = []
                data.update({
                    "system_status": system_status,
                    "report_files": report_files,
                    "summary": "Recent system and business reports are available from this view",
                })
            elif module_id == "email":
                # Real connection state comes from actions/google_auth.py's cached
                # OAuth token (config/google/token.json), not a "google_credentials"
                # config/api_keys.json key — that key is never written by the real
                # auth flow, so checking it always reported not_connected even when
                # Gmail was genuinely authorized. See build_provider_status_payload().
                provider_status = build_provider_status_payload() if build_provider_status_payload is not None else {"email": {"status": "not_connected"}}
                configured = provider_status.get("email", {}).get("status") == "connected"
                detail = (
                    "Gmail is authorized; outbound sends remain approval-gated"
                    if configured else
                    "No email credentials are currently configured"
                )
                data.update({
                    "configured": configured,
                    "status": "Email workflow is ready; outbound sends require explicit approval",
                    "detail": detail,
                    "summary": "Inbox status will appear here once mail infrastructure is configured",
                    "providers": provider_status.get("email", {}),
                })
            elif module_id == "calendar":
                provider_status = build_provider_status_payload() if build_provider_status_payload is not None else {"calendar": {"status": "not_connected"}}
                configured = provider_status.get("calendar", {}).get("status") == "connected"
                detail = (
                    "Google Calendar is authorized; event changes remain approval-gated"
                    if configured else
                    "No calendar credentials are currently configured"
                )
                data.update({
                    "configured": configured,
                    "status": "Calendar view is ready; event changes require explicit approval",
                    "detail": detail,
                    "summary": "Upcoming appointments will appear here once calendar infrastructure is configured",
                    "providers": provider_status.get("calendar", {}),
                    "approval": get_approval_gate("create_event") if get_approval_gate is not None else {"requires_approval": True},
                })
            elif module_id == "communications":
                if twilio is not None:
                    try:
                        status = twilio.get_status()
                        history = twilio.get_history(limit=10)
                        missed = twilio.get_missed_calls(limit=5)
                    except Exception as exc:
                        status = {"state": "ERROR", "detail": str(exc)}
                        history, missed = [], []
                    configured = status["state"] != "NOT_CONFIGURED"
                    data.update({
                        "configured": configured,
                        "status": status["state"],
                        "detail": status["detail"],
                        "summary": (
                            "Twilio is configured — real calls/SMS are possible from this Nucleus."
                            if configured else
                            "Twilio is not configured yet — Phone/SMS/Calls remain structural placeholders "
                            "until credentials are added to config/api_keys.json."
                        ),
                        "history": history,
                        "missed_calls": missed,
                        "channels": {
                            "phone":         {"status": status["state"], "detail": "Outbound/inbound calls via Twilio Voice"},
                            "sms":           {"status": status["state"], "detail": "Outbound/inbound SMS via Twilio Messaging"},
                            "calls":         {"status": "live" if configured else "placeholder", "detail": f"{len(history)} recent record(s)"},
                            "contacts":      {"status": "placeholder", "detail": "No contacts database configured — numbers must be stated directly"},
                            "notifications": {"status": "placeholder", "detail": "Notification routing not yet connected"},
                        },
                    })
                else:
                    data.update({
                        "configured": False,
                        "status": "ERROR",
                        "summary": "Twilio integration module failed to import.",
                        "channels": {},
                    })
            elif module_id == "system":
                if get_system_status is not None:
                    try:
                        data.update(get_system_status())
                    except Exception as exc:
                        data["error"] = str(exc)
                if agent_orchestrator is not None:
                    try:
                        data["agents"] = agent_orchestrator.summary()
                    except Exception as exc:
                        data["agents"] = {"error": str(exc)}
                if get_objective_status is not None:
                    try:
                        data["strategic_objective"] = get_objective_status()
                    except Exception as exc:
                        data["strategic_objective"] = {"error": str(exc)}
                if biz_intel is not None:
                    try:
                        data["business_intelligence"] = biz_intel.summary()
                    except Exception as exc:
                        data["business_intelligence"] = {"error": str(exc)}
            elif module_id in {"buildpro", "careerrocket", "personal"}:
                # Hierarchy node/children/path already attached above — this
                # branch exists so these ids get a friendly summary too, plus
                # (Phase 9) a business-scoped BI/opportunity snapshot for the
                # two real businesses here (personal isn't a tracked business;
                # "ddf" is handled in the "deals"/"ddf" branch above instead,
                # since it already needs its own top_products handling).
                data.setdefault("summary", f"{module_id} nucleus is ready for branch navigation")
                business_key = {"buildpro": "buildpro", "careerrocket": "careerrocket"}.get(module_id)
                if business_key:
                    if biz_intel is not None:
                        try:
                            data["business_intelligence"] = biz_intel.summary(business=business_key)
                        except Exception as exc:
                            data["business_intelligence"] = {"error": str(exc)}
                    if opp_engine is not None:
                        try:
                            data["top_opportunities"] = opp_engine.rank_opportunities(business=business_key, limit=3)
                        except Exception as exc:
                            data["top_opportunities"] = []
                if module_id == "buildpro" and buildpro_data is not None:
                    try:
                        data["buildpro_recruiting"] = buildpro_data.summary()
                        data["buildpro_followups"] = {
                            "candidates": buildpro_data.list_candidates_needing_followup(limit=10),
                            "clients": buildpro_data.list_clients_needing_followup(limit=10),
                        }
                    except Exception as exc:
                        data["buildpro_recruiting"] = {"error": str(exc)}
            elif module_id == "candidates":
                if buildpro_data is not None:
                    try:
                        data["results"] = buildpro_data.list_candidates(limit=25)
                        data["summary"] = f"{len(data['results'])} candidate(s) on file."
                    except Exception as exc:
                        data["results"] = []
                        data["error"] = str(exc)
                else:
                    data["results"] = []
                    data["summary"] = "BuildPro data foundation unavailable."
            elif module_id == "clients":
                if buildpro_data is not None:
                    try:
                        data["results"] = buildpro_data.list_clients(limit=25)
                        data["summary"] = f"{len(data['results'])} client(s) on file."
                    except Exception as exc:
                        data["results"] = []
                        data["error"] = str(exc)
                else:
                    data["results"] = []
                    data["summary"] = "BuildPro data foundation unavailable."
            elif module_id == "jobs":
                if buildpro_data is not None:
                    try:
                        data["results"] = buildpro_data.list_jobs(limit=25)
                        data["summary"] = f"{len(data['results'])} job(s) on file."
                    except Exception as exc:
                        data["results"] = []
                        data["error"] = str(exc)
                else:
                    data["results"] = []
                    data["summary"] = "BuildPro data foundation unavailable."
            elif module_id == "prospects":
                if buildpro_data is not None:
                    try:
                        data["results"] = buildpro_data.list_clients(status="prospect", limit=25)
                        data["summary"] = f"{len(data['results'])} prospect(s) identified."
                    except Exception as exc:
                        data["results"] = []
                        data["error"] = str(exc)
                else:
                    data["results"] = []
                    data["summary"] = "BuildPro data foundation unavailable."
            elif module_id == "matches":
                if buildpro_data is not None:
                    try:
                        data["results"] = buildpro_data.top_matches(limit=25)
                        data["summary"] = f"{len(data['results'])} candidate/job match(es) scored."
                    except Exception as exc:
                        data["results"] = []
                        data["error"] = str(exc)
                else:
                    data["results"] = []
                    data["summary"] = "BuildPro data foundation unavailable."
            elif module_id == "core":
                data["summary"] = "Core intelligence is active and ready for action"
            else:
                data.setdefault("summary", f"{module_id} module is ready for navigation")

            module = {
                "id": module_id,
                "title": module_id.replace("-", " ").title(),
                "desc": "Module ready for live operations",
            }
            return JSONResponse({"module": module, "data": data})

        @app.post("/3d/api/command")
        async def three_d_command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            action = str(body.get("action") or "").strip().lower()
            result = {"ok": False, "result": "Unsupported action"}
            if action == "system_status" and get_system_status is not None:
                try:
                    result = {"ok": True, "result": get_system_status()}
                except Exception as exc:
                    result = {"ok": False, "result": str(exc)}
            elif action == "deals" and get_top_products is not None:
                try:
                    result = {"ok": True, "result": {"top_products": get_top_products(limit=5)}}
                except Exception as exc:
                    result = {"ok": False, "result": str(exc)}
            elif action == "open_app":
                app_name = str(body.get("app_name") or "").strip()
                if app_name and open_app is not None:
                    try:
                        result = {"ok": True, "result": open_app(parameters={"app_name": app_name})}
                    except Exception as exc:
                        result = {"ok": False, "result": str(exc)}
                else:
                    result = {"ok": False, "result": "No app provided"}
            elif action == "navigate":
                # Shared by voice (main.py calls self.apply_navigation directly
                # — same process, no need to loop back through HTTP) and mouse
                # clicks (the frontend posts here) so both stay in sync.
                nav_action = str(body.get("nav_action") or "open").strip().lower()
                nucleus_id = str(body.get("nucleus_id") or "").strip()
                event = self.apply_navigation(nav_action, nucleus_id)
                asyncio.create_task(self.broadcast_nav(event))
                result = {"ok": True, "result": event}
            else:
                result = {"ok": False, "result": f"Unsupported action: {action}"}
            return JSONResponse(result)

        @app.websocket("/3d/ws")
        async def three_d_ws(websocket: WebSocket):
            """Live push channel for the 3D command center: navigate events
            (voice or mouse-originated) and jarvis_state updates. Requires
            the same session/static token as /3d/api/* — passed as a query
            param (?token=...) since a browser can't set a custom
            Authorization header on a WebSocket handshake. Closes the J1
            finding that this channel had no auth at all."""
            tok = websocket.query_params.get("token", "")
            if not _token_valid(tok):
                await websocket.close(code=4401)
                return
            await websocket.accept()
            self._nav_clients.add(websocket)
            try:
                node = get_hierarchy_node(self._nucleus_id) if get_hierarchy_node is not None else None
                await websocket.send_json({
                    "type": "navigate",
                    "action": "status",
                    "nucleus_id": self._nucleus_id,
                    "name": (node.get("name") if node else self._nucleus_id),
                    "ts": time.time(),
                })
            except Exception:
                pass
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "navigate":
                        event = self.apply_navigation(
                            str(data.get("action") or "open"),
                            str(data.get("nucleus_id") or ""),
                        )
                        asyncio.create_task(self.broadcast_nav(event))
            except WebSocketDisconnect:
                pass
            finally:
                self._nav_clients.discard(websocket)

        # ── Twilio inbound webhooks ──────────────────────────────────────────
        # Configure these as the Voice/SMS/Status-callback URLs on the Twilio
        # phone number once Lee has an account (see actions/twilio_integration.py
        # for the NOT_CONFIGURED/CONFIGURED/CONNECTED/ERROR states — these
        # routes work today; they simply have nothing to receive until a real
        # Twilio number points at this server's public URL).
        #
        # SECURITY: these are the only routes in this file that receive
        # traffic from the open internet by design (a real Twilio number's
        # webhooks aren't LAN-local like the rest of /3d/* — see
        # _verify_twilio_signature below). Before this fix, any of them
        # accepted unsigned POSTs from anyone who found the URL, letting an
        # attacker inject fake "incoming call"/"SMS" notifications (shown to
        # the user) and fake history rows — standard Twilio signature
        # verification (X-Twilio-Signature, HMAC-SHA1 keyed with the real
        # auth_token) closes that without affecting genuine Twilio traffic.

        @app.post("/twilio/voice")
        async def twilio_voice(req: Request):
            form = await req.form()
            if not _verify_twilio_signature(req, dict(form)):
                return Response(content="", status_code=403)
            from_number = str(form.get("From", ""))
            to_number = str(form.get("To", ""))
            call_sid = str(form.get("CallSid", ""))
            if twilio is not None:
                try:
                    twilio.record_inbound_call(from_number, to_number, call_sid, status=str(form.get("CallStatus", "ringing")))
                except Exception as exc:
                    print(f"[Twilio] Failed to record inbound call: {exc}")
            asyncio.create_task(self.broadcast({
                "type": "sys", "text": f"Incoming call from {from_number or 'unknown'}.",
            }))
            asyncio.create_task(self.broadcast_nav({
                "type": "notification", "text": f"📞 Incoming call from {from_number or 'unknown'}",
            }))
            twiml = twilio.voicemail_twiml() if twilio is not None else "<Response><Say>Communications unavailable.</Say></Response>"
            return Response(content=twiml, media_type="application/xml")

        @app.post("/twilio/sms")
        async def twilio_sms(req: Request):
            form = await req.form()
            if not _verify_twilio_signature(req, dict(form)):
                return Response(content="", status_code=403)
            from_number = str(form.get("From", ""))
            to_number = str(form.get("To", ""))
            body = str(form.get("Body", ""))
            sid = str(form.get("MessageSid", ""))
            if twilio is not None:
                try:
                    twilio.record_inbound_sms(from_number, to_number, body, sid)
                except Exception as exc:
                    print(f"[Twilio] Failed to record inbound SMS: {exc}")
            asyncio.create_task(self.broadcast({
                "type": "sys", "text": f"SMS from {from_number or 'unknown'}: {body[:80]}",
            }))
            asyncio.create_task(self.broadcast_nav({
                "type": "notification", "text": f"💬 SMS from {from_number or 'unknown'}: {body[:60]}",
            }))
            return Response(content="<Response></Response>", media_type="application/xml")

        @app.post("/twilio/status")
        async def twilio_status(req: Request):
            form = await req.form()
            if not _verify_twilio_signature(req, dict(form)):
                return Response(content="", status_code=403)
            sid = str(form.get("CallSid") or form.get("MessageSid") or "")
            status = str(form.get("CallStatus") or form.get("MessageStatus") or "")
            if twilio is not None and sid and status:
                try:
                    twilio.update_by_sid(sid, status=status)
                except Exception as exc:
                    print(f"[Twilio] Failed to update status for {sid}: {exc}")
            return Response(content="", media_type="application/xml")

        @app.post("/twilio/transcription")
        async def twilio_transcription(req: Request):
            form = await req.form()
            if not _verify_twilio_signature(req, dict(form)):
                return Response(content="", status_code=403)
            sid = str(form.get("CallSid", ""))
            text = str(form.get("TranscriptionText", ""))
            if twilio is not None and sid:
                try:
                    twilio.update_by_sid(sid, transcription=text)
                except Exception as exc:
                    print(f"[Twilio] Failed to record transcription for {sid}: {exc}")
            asyncio.create_task(self.broadcast({
                "type": "sys", "text": f"Voicemail transcribed: {text[:120]}" if text else "Voicemail received.",
            }))
            return Response(content="", media_type="application/xml")

        @app.post("/twilio/recording-status")
        async def twilio_recording_status(req: Request):
            # Acknowledge only — recording URL/duration aren't stored yet
            # (would need a dedicated column; not needed until Twilio is live).
            form = await req.form()
            if not _verify_twilio_signature(req, dict(form)):
                return Response(content="", status_code=403)
            return Response(content="", media_type="application/xml")

        @app.websocket("/twilio/stream")
        async def twilio_media_stream(websocket: WebSocket):
            """Connection point for Twilio's <Stream> media-streams feature —
            NOT wired to anything yet. Real-time bidirectional audio bridging
            into the Gemini Live session (phone call <-> Jarvis conversation)
            is real-time audio architecture that's out of scope for this
            session; this stub exists so the wiring point is real and the gap
            is explicit rather than silently absent."""
            await websocket.accept()
            print("[Twilio] Media stream connected (architecture placeholder — not bridged to Gemini Live yet)")
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            raw     = str(body.get("pin", "")).strip()
            entered = raw.upper()
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
            # No live desktop JARVIS to hand out a 6-char pairing key (the
            # normal case for a cloud-only deployment) — accept the static
            # JARVIS_API_TOKEN directly instead, and hand back a stateless
            # signed session token (see _token_valid above) rather than the
            # raw secret, so the browser never has to hold the actual
            # JARVIS_API_TOKEN beyond this one login POST body.
            if headless_config.API_TOKEN and hmac.compare_digest(raw, headless_config.API_TOKEN):
                from core.headless.ui import _new_session
                tok = _new_session()
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
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
  localStorage.setItem('jarvis_token','{tok}');
  localStorage.setItem('jarvis_key','{key}');
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
            if not _token_valid(tok):
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
            if not _token_valid(tok):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not _token_valid(tok):
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

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        if not is_port_free(PORT + 1):
            _report_port_conflict(PORT + 1, "manual-entry HTTPS alias")
            return
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        try:
            await uvicorn.Server(cfg).serve()
        except SystemExit as e:
            _report_port_conflict(PORT + 1, "manual-entry HTTPS alias", exit_code=e.code)
        except Exception as e:
            print(f"[Dashboard] Unexpected error starting the manual-entry alias on port {PORT + 1}: {e}")

    async def _serve_http_plain(self) -> None:
        """Third server on PORT+2, always plain HTTP — no TLS, ever. A single
        port can't speak both TLS and plaintext at once, so this can't just
        be PORT itself when SSL is active; it exists purely so LAN/iPad
        browsers that don't want to deal with a self-signed cert warning
        still have a working entry point (mainly for /3d, whose JS builds
        API/WebSocket URLs from location.host so it works unmodified from
        any port). The QR-pairing flow and PORT/PORT+1 are untouched by this
        — this is purely additive."""
        if not is_port_free(HTTP_PORT):
            _report_port_conflict(HTTP_PORT, "plain-HTTP fallback")
            return
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, HTTP_PORT)
        cfg = uvicorn.Config(self.app, host="0.0.0.0", port=HTTP_PORT, log_level="warning")
        print(f"[Dashboard] Plain HTTP (no cert warning):  http://{self._ip}:{HTTP_PORT}")
        try:
            await uvicorn.Server(cfg).serve()
        except SystemExit as e:
            _report_port_conflict(HTTP_PORT, "plain-HTTP fallback", exit_code=e.code)
        except Exception as e:
            print(f"[Dashboard] Unexpected error starting the plain-HTTP fallback on port {HTTP_PORT}: {e}")

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Pre-flight port check BEFORE ever handing the port to uvicorn.
        # uvicorn's own bind failure calls sys.exit(), which — raised inside
        # an asyncio Task that's never awaited — propagates through
        # Task.__step's special-cased (KeyboardInterrupt, SystemExit)
        # handling and kills the WHOLE JARVIS process, not just the
        # dashboard. This is exactly what happens when a second JARVIS
        # instance is started while a first is still running. Checking
        # first (and catching SystemExit below as a backstop against any
        # remaining bind-time race) turns that crash into a clear,
        # recoverable message instead.
        if not is_port_free(PORT):
            _report_port_conflict(PORT, "dashboard (remote/phone control)")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())
            # PORT itself is HTTPS-only whenever certs exist (a port can't
            # speak both protocols) — add a plain-HTTP alternative on a
            # separate port so http://<ip>:8000-style access isn't a dead
            # end. When SSL is off, PORT is already plain HTTP and this
            # would just be a redundant duplicate, so only start it here.
            asyncio.create_task(self._serve_http_plain())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        try:
            await uvicorn.Server(cfg).serve()
        except SystemExit as e:
            _report_port_conflict(PORT, "dashboard (remote/phone control)", exit_code=e.code)
        except Exception as e:
            print(f"[Dashboard] Unexpected error starting on port {PORT}: {e}")
