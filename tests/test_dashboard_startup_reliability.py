"""dashboard/server.py's port pre-flight check (Prompt 19).

Verifies the actual bug this closes: uvicorn's own bind-failure path calls
sys.exit(), which — raised inside a never-awaited asyncio Task — propagates
through Task.__step's special-cased SystemExit handling and kills the
WHOLE JARVIS process, not just the dashboard. These tests bind a real
localhost socket to simulate "port already in use," then confirm
DashboardServer.serve()/._serve_alias()/._serve_http_plain() detect it and
return cleanly with a clear message instead of ever reaching uvicorn — no
uvicorn server, and therefore no persistent service, is ever started here.
"""
import asyncio
import socket

import pytest

import dashboard.server as server_mod
from dashboard.server import DashboardServer


def _bind_a_real_port() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_serve_skips_gracefully_when_the_main_port_is_already_bound(monkeypatch, capsys):
    holder, busy_port = _bind_a_real_port()
    try:
        monkeypatch.setattr(server_mod, "PORT", busy_port)
        monkeypatch.setattr(server_mod, "HTTP_PORT", busy_port + 2)
        server = DashboardServer()

        asyncio.run(server.serve())   # must return cleanly, never raise/exit

        out = capsys.readouterr().out
        assert f"Port {busy_port} is already in use" in out
        assert "skipping" in out.lower()
    finally:
        holder.close()


def test_serve_alias_skips_gracefully_when_its_port_is_already_bound(monkeypatch, capsys):
    holder, busy_port = _bind_a_real_port()
    try:
        monkeypatch.setattr(server_mod, "PORT", busy_port - 1)   # _serve_alias uses PORT + 1
        server = DashboardServer()

        asyncio.run(server._serve_alias())

        out = capsys.readouterr().out
        assert f"Port {busy_port} is already in use" in out
    finally:
        holder.close()


def test_serve_http_plain_skips_gracefully_when_its_port_is_already_bound(monkeypatch, capsys):
    holder, busy_port = _bind_a_real_port()
    try:
        monkeypatch.setattr(server_mod, "HTTP_PORT", busy_port)
        server = DashboardServer()

        asyncio.run(server._serve_http_plain())

        out = capsys.readouterr().out
        assert f"Port {busy_port} is already in use" in out
    finally:
        holder.close()


def test_report_port_conflict_never_raises_and_logs(tmp_path, capsys):
    # tests/conftest.py's autouse _isolate_lifecycle_logger fixture already
    # points core.startup's logger at this test's own tmp_path/logs.
    server_mod._report_port_conflict(9999, "test server", exit_code=3)

    out = capsys.readouterr().out
    assert "Port 9999" in out
    assert "exit code 3" in out
    assert (tmp_path / "logs" / "jarvis.log").exists()
