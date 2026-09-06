"""actions/agreement_signing.py — the self-hosted e-signature flow for the
representation agreement (candidate-intake chain, 2026-08-19). Covers the
storage/signing logic; tests/test_agreement_routes.py covers the HTTP page.
"""
from actions import agreement_signing as sig


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(sig, "DB_PATH", tmp_path / "test_agreements.db")


def test_create_pending_agreement_returns_a_usable_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")
    assert token
    agreement = sig.get_agreement(token)
    assert agreement is not None
    assert agreement["candidate_name"] == "Jane Doe"
    assert agreement["candidate_email"] == "jane@example.com"
    assert agreement["signed_ts"] is None
    # The agreement text actually shown is snapshotted at creation time.
    assert agreement["agreement_text"] == sig.AGREEMENT_TEXT


def test_get_agreement_returns_none_for_unknown_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert sig.get_agreement("does-not-exist") is None


def test_sign_agreement_records_name_ip_and_timestamp(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")

    result = sig.sign_agreement(token, "Jane M. Doe", "203.0.113.5")
    assert result["ok"] is True
    assert result["already_signed"] is False
    assert result["signed_name"] == "Jane M. Doe"

    agreement = sig.get_agreement(token)
    assert agreement["signed_name"] == "Jane M. Doe"
    assert agreement["signer_ip"] == "203.0.113.5"
    assert agreement["signed_ts"] is not None


def test_sign_agreement_rejects_unknown_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = sig.sign_agreement("nope", "Jane Doe", "203.0.113.5")
    assert result["ok"] is False
    assert "expired" in result["detail"] or "Unknown" in result["detail"]


def test_sign_agreement_rejects_blank_name(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")
    result = sig.sign_agreement(token, "   ", "203.0.113.5")
    assert result["ok"] is False


def test_signing_twice_does_not_overwrite_the_original_signature(monkeypatch, tmp_path):
    # The record of what was first agreed to, and when, must not change
    # after the fact — a second signing attempt just confirms it.
    _isolate(monkeypatch, tmp_path)
    token = sig.create_pending_agreement(1, "Jane Doe", "jane@example.com")
    sig.sign_agreement(token, "Jane Doe", "203.0.113.5")
    first_ts = sig.get_agreement(token)["signed_ts"]

    second = sig.sign_agreement(token, "Someone Else", "198.51.100.9")
    assert second["ok"] is True
    assert second["already_signed"] is True
    assert second["signed_name"] == "Jane Doe"

    agreement = sig.get_agreement(token)
    assert agreement["signed_name"] == "Jane Doe"
    assert agreement["signer_ip"] == "203.0.113.5"
    assert agreement["signed_ts"] == first_ts
