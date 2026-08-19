import json
import time

import pytest

from actions import google_auth


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    google_dir = tmp_path / "google"
    monkeypatch.setattr(google_auth, "GOOGLE_CONFIG_DIR", google_dir)
    monkeypatch.setattr(google_auth, "TOKEN_PATH", google_dir / "token.json")


def _write_client_secret(tmp_dir):
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / "client_secret_test.apps.googleusercontent.com.json"
    path.write_text(json.dumps({
        "installed": {
            "client_id": "fake-client-id", "client_secret": "fake-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }), encoding="utf-8")
    return path


# ── is_configured / get_credential_status — structure-only, no secrets ────

def test_is_configured_false_when_no_credential_file():
    assert google_auth.is_configured() is False


def test_is_configured_true_when_credential_file_present():
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)
    assert google_auth.is_configured() is True


def test_get_credential_status_missing_file():
    status = google_auth.get_credential_status()
    assert status["credential_file"] == "missing"
    assert status["authorized"] is False


def test_get_credential_status_present_not_authorized():
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)
    status = google_auth.get_credential_status()
    assert status["credential_file"] == "present"
    assert status["credential_type"] == "installed"
    assert status["token_cached"] is False
    assert status["authorized"] is False


def test_get_credential_status_detects_malformed_json():
    google_auth.GOOGLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    bad = google_auth.GOOGLE_CONFIG_DIR / "client_secret_bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    status = google_auth.get_credential_status()
    assert status["credential_type"] == "malformed"


def test_get_credential_status_never_includes_secret_values():
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)
    status = google_auth.get_credential_status()
    serialized = json.dumps(status)
    assert "fake-secret" not in serialized
    assert "fake-client-id" not in serialized


# ── get_credentials — never triggers the interactive flow ────────────────

def test_get_credentials_raises_when_not_authorized_does_not_open_browser(monkeypatch):
    """The critical safety property: no token cached -> RuntimeError, and
    the interactive flow is never invoked as a side effect."""
    flow_calls = []
    monkeypatch.setattr(google_auth, "authorize_interactively", lambda: flow_calls.append(1))
    with pytest.raises(RuntimeError, match="not yet authorized"):
        google_auth.get_credentials()
    assert flow_calls == []


def test_get_credentials_returns_valid_cached_credentials(monkeypatch):
    class FakeCreds:
        valid = True
        expired = False
        refresh_token = "rt"

    google_auth.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    google_auth.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: FakeCreds()))

    creds = google_auth.get_credentials()
    assert creds.valid is True


def test_get_credentials_refreshes_expired_token_and_rewrites_cache(monkeypatch):
    class FakeCreds:
        valid = False
        expired = True
        refresh_token = "rt"
        refreshed = False

        def refresh(self, request):
            self.refreshed = True
            self.valid = True

        def to_json(self):
            return json.dumps({"refreshed": True})

    fake = FakeCreds()
    google_auth.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    google_auth.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: fake))

    google_auth.get_credentials()
    assert fake.refreshed is True
    assert json.loads(google_auth.TOKEN_PATH.read_text(encoding="utf-8")) == {"refreshed": True}


def test_get_credentials_raises_when_expired_with_no_refresh_token(monkeypatch):
    class FakeCreds:
        valid = False
        expired = True
        refresh_token = None

    google_auth.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    google_auth.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: FakeCreds()))

    with pytest.raises(RuntimeError, match="re-authorize"):
        google_auth.get_credentials()


# ── build_service — every Gmail/Calendar call must be timeout-bounded ───
# Phase 3 fix: build(..., credentials=creds) alone never sets a timeout —
# httplib2 defaults to socket.getdefaulttimeout(), which is None (block
# forever). A stalled connection could hang indefinitely, and since chat
# messages are processed one at a time, that stalled every later message
# too. This is what actually explained the reported "5 to 10 minute"
# response times, not slow reasoning.

def test_build_service_sets_an_explicit_bounded_timeout(monkeypatch):
    class FakeCreds:
        valid = True
        expired = False
        refresh_token = "rt"

    google_auth.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    google_auth.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: FakeCreds()))

    import httplib2
    captured = {}
    _orig_init = httplib2.Http.__init__

    def _spy_init(self, *a, **k):
        captured.update(k)
        return _orig_init(self, *a, **k)

    monkeypatch.setattr(httplib2.Http, "__init__", _spy_init)

    google_auth.build_service("gmail", "v1")
    assert captured.get("timeout") == google_auth.API_TIMEOUT_SECONDS
    assert captured["timeout"] is not None


# ── verify_google_auth — safe, real-but-cheap check, no flow triggered ───

def test_verify_google_auth_missing_credential_file():
    result = google_auth.verify_google_auth()
    assert result["verified"] is False
    assert "No Google client-secret" in result["detail"]


def test_verify_google_auth_no_token_cached():
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)
    result = google_auth.verify_google_auth()
    assert result["verified"] is False
    assert "not been run yet" in result["detail"]


def test_verify_google_auth_success_makes_one_cheap_real_call(monkeypatch):
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)
    google_auth.TOKEN_PATH.write_text("{}", encoding="utf-8")

    class FakeCreds:
        valid = True
        expired = False
        refresh_token = "rt"

    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: FakeCreds()))

    class FakeProfileCall:
        def execute(self):
            return {"emailAddress": "lee@buildpro.example", "messagesTotal": 42}

    class FakeUsers:
        def getProfile(self, userId):
            return FakeProfileCall()

    class FakeService:
        def users(self):
            return FakeUsers()

    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeService())

    result = google_auth.verify_google_auth()
    assert result["verified"] is True
    assert "lee@buildpro.example" in result["detail"]
    assert result["messages_total"] == 42


def test_verify_google_auth_reports_failed_call_honestly(monkeypatch):
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)
    google_auth.TOKEN_PATH.write_text("{}", encoding="utf-8")

    class FakeCreds:
        valid = True
        expired = False
        refresh_token = "rt"

    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: FakeCreds()))
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: (_ for _ in ()).throw(Exception("token revoked")))

    result = google_auth.verify_google_auth()
    assert result["verified"] is False
    assert "token revoked" in result["detail"]


# ── authorize_interactively — never run for real in tests ────────────────

def test_authorize_interactively_raises_without_credential_file():
    with pytest.raises(RuntimeError, match="No Google client-secret"):
        google_auth.authorize_interactively()


def test_authorize_interactively_never_opens_a_real_browser_in_tests(monkeypatch):
    """Confirms the flow is fully mockable/isolatable — this test proves
    authorize_interactively() only does what InstalledAppFlow tells it to,
    with no hidden network/browser calls of its own."""
    _write_client_secret(google_auth.GOOGLE_CONFIG_DIR)

    class FakeFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return FakeFlow()

        def run_local_server(self, port=0):
            class FakeCreds:
                def to_json(self):
                    return json.dumps({"fake": "token"})
            return FakeCreds()

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", FakeFlow)
    result = google_auth.authorize_interactively()
    assert result["authorized"] is True
    assert json.loads(google_auth.TOKEN_PATH.read_text(encoding="utf-8")) == {"fake": "token"}
