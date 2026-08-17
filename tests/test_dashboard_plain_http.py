import asyncio
import socket

from dashboard.server import DashboardServer, HTTP_PORT
from dashboard import server as srv


def test_http_port_is_two_above_primary_port():
    assert HTTP_PORT == srv.PORT + 2


# ── wiring: serve() only starts the plain-HTTP listener when it's actually
#    needed (PORT itself is HTTPS-only) — no redundant extra port otherwise ──

def test_serve_schedules_both_alias_and_plain_http_when_ssl_enabled(monkeypatch):
    dash = DashboardServer()
    monkeypatch.setattr(dash, "_ssl_enabled", lambda: True)
    monkeypatch.setattr(srv, "_ensure_network_access", lambda *a, **k: None)
    # This test is about serve()'s SCHEDULING logic, not real port
    # availability (see tests/test_dashboard_startup_reliability.py for
    # that) — keep it hermetic regardless of what's actually bound on
    # this machine right now.
    monkeypatch.setattr(srv, "is_port_free", lambda *a, **k: True)

    called = {"alias": False, "http_plain": False}

    async def fake_alias():
        called["alias"] = True

    async def fake_http_plain():
        called["http_plain"] = True

    monkeypatch.setattr(dash, "_serve_alias", fake_alias)
    monkeypatch.setattr(dash, "_serve_http_plain", fake_http_plain)

    class _FakeServer:
        def __init__(self, cfg):
            pass

        async def serve(self):
            await asyncio.sleep(0.05)   # let the scheduled tasks actually run

    monkeypatch.setattr(srv.uvicorn, "Server", _FakeServer)

    asyncio.run(dash.serve())

    assert called["alias"] is True
    assert called["http_plain"] is True


def test_serve_does_not_start_plain_http_when_ssl_disabled(monkeypatch):
    # PORT itself is already plain HTTP in this case — a second plain-HTTP
    # listener would be a redundant open port for no benefit.
    dash = DashboardServer()
    monkeypatch.setattr(dash, "_ssl_enabled", lambda: False)
    monkeypatch.setattr(srv, "_ensure_network_access", lambda *a, **k: None)
    monkeypatch.setattr(srv, "is_port_free", lambda *a, **k: True)

    called = {"http_plain": False}

    async def fake_http_plain():
        called["http_plain"] = True

    monkeypatch.setattr(dash, "_serve_http_plain", fake_http_plain)

    class _FakeServer:
        def __init__(self, cfg):
            pass

        async def serve(self):
            await asyncio.sleep(0.05)

    monkeypatch.setattr(srv.uvicorn, "Server", _FakeServer)

    asyncio.run(dash.serve())

    assert called["http_plain"] is False


# ── real end-to-end: actually bind the socket and make a real plain-HTTP
#    request against it — the same class of failure ("empty reply") this
#    whole change exists to fix wouldn't be caught by a mocked test ──────────

def test_serve_http_plain_actually_answers_plain_http_requests(monkeypatch):
    import requests

    monkeypatch.setattr(srv, "_ensure_network_access", lambda *a, **k: None)
    dash = DashboardServer()

    async def run():
        task = asyncio.create_task(dash._serve_http_plain())
        try:
            for _ in range(50):   # up to ~5s for uvicorn to bind
                await asyncio.sleep(0.1)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    if s.connect_ex(("127.0.0.1", HTTP_PORT)) == 0:
                        break
            else:
                raise AssertionError("plain HTTP server never started listening")

            resp = await asyncio.to_thread(
                requests.get, f"http://127.0.0.1:{HTTP_PORT}/3d", timeout=5
            )
            return resp
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert "jarvis-orb" in resp.text
    assert "spatial-stage" in resp.text
