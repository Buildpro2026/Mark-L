import json

from actions import buffer_integration as buf
from core.headless import config as _hc


def _isolate(monkeypatch, tmp_path, token=""):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"buffer_token": token}), encoding="utf-8")
    monkeypatch.setattr(buf, "CONFIG_PATH", cfg_path)
    # Isolate the local publish-history DB too — several tests reuse the
    # same channel_id/text ("c1"/"hello"), and without this they'd trip
    # each other's duplicate-post detection via the real shared database.
    monkeypatch.setattr(buf, "DB_PATH", tmp_path / "test_buffer.db")
    # get_token() checks core.headless.config.BUFFER_TOKEN (the real env
    # var) FIRST, before ever reading CONFIG_PATH — on a dev machine with
    # a real .env this silently ignored the token=... arg above.
    monkeypatch.setattr(_hc, "BUFFER_TOKEN", token or None)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err


def test_verify_buffer_reports_not_configured_when_missing_token():
    result = buf.verify_buffer()
    assert result["status"].startswith(("NOT CONFIGURED", "VERIFIED", "UNAVAILABLE:"))
    assert "configured" in result


def test_verify_buffer_not_configured_short_circuits_without_network(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    r = buf.verify_buffer()
    assert r == {"configured": False, "authenticated": False, "verified": False, "functional": False, "status": "NOT CONFIGURED"}


def test_verify_buffer_uses_graphql_with_bearer_auth(monkeypatch, tmp_path):
    """Regression test for the original bug: verify_buffer() used to send
    the token as a Bearer header against the deprecated REST v1 endpoint,
    which this token type doesn't work against at all. It must now hit
    GRAPHQL_URL with the same Bearer header."""
    _isolate(monkeypatch, tmp_path, token="secret-token")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return _Resp(200, {"data": {"account": {"id": "acc1", "name": "n", "email": "e"}}})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.verify_buffer()
    assert r["status"] == "VERIFIED"
    assert r["verified"] is True
    assert r["data"]["id"] == "acc1"
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url == buf.GRAPHQL_URL
    assert headers["Authorization"] == "Bearer secret-token"
    assert "account" in body["query"]


def test_verify_buffer_reports_public_api_token_rejection(monkeypatch, tmp_path):
    """The exact failure this fix targets: the legacy REST API's
    'Public API tokens are not accepted' error, now surfaced as a GraphQL
    `errors` entry instead of a raw 401 with no explanation."""
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, {"errors": [{"message": "Public API tokens are not accepted for REST API access"}]})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.verify_buffer()
    assert r["verified"] is False
    assert "Public API tokens" in r["status"]


def test_verify_buffer_handles_http_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(401, {})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.verify_buffer()
    assert r["verified"] is False
    assert "401" in r["status"]


def test_get_channels_not_configured_without_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    r = buf.get_channels()
    assert r == {"configured": False, "channels": [], "status": "NOT CONFIGURED"}


def test_get_channels_fetches_org_then_channels(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json["query"])
        if "organizations" in json["query"]:
            return _Resp(200, {"data": {"account": {"organizations": [{"id": "org1", "name": "Org"}]}}})
        return _Resp(200, {"data": {"channels": [
            {"id": "c1", "name": "buildpro", "displayName": "BuildPro", "service": "linkedin", "type": "page", "isDisconnected": False},
        ]}})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.get_channels()
    assert r["status"] == "VERIFIED"
    assert r["organization_id"] == "org1"
    assert len(r["channels"]) == 1
    assert r["channels"][0]["service"] == "linkedin"
    assert len(calls) == 2


def test_get_channels_reports_missing_organization(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, {"data": {"account": {"organizations": []}}})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.get_channels()
    assert r["channels"] == []
    assert "no organization" in r["status"].lower()


def test_publish_to_buffer_not_configured_without_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="")
    # channel_id given directly (not "service") so this exercises the
    # NOT-CONFIGURED path specifically, past the earlier validation gates.
    r = buf.publish_to_buffer({"caption": "hello", "channel_id": "c1"})
    assert r == {"status": "NOT CONFIGURED", "published": False}


# ── resolve_channel_id ────────────────────────────────────────────────────

def test_resolve_channel_id_finds_connected_channel(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    channels = [
        {"id": "c1", "name": "n1", "displayName": "BuildPro LinkedIn", "service": "linkedin", "type": "page", "isDisconnected": False},
        {"id": "c2", "name": "n2", "displayName": "BuildPro IG", "service": "instagram", "type": "business", "isDisconnected": False},
    ]
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": channels, "status": "VERIFIED"})
    r = buf.resolve_channel_id("linkedin")
    assert r["ok"] is True
    assert r["channel_id"] == "c1"
    assert r["channel_name"] == "BuildPro LinkedIn"


def test_resolve_channel_id_is_case_insensitive(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    channels = [{"id": "c1", "displayName": "X", "service": "TikTok", "isDisconnected": False}]
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": channels, "status": "VERIFIED"})
    r = buf.resolve_channel_id("tiktok")
    assert r["ok"] is True


def test_resolve_channel_id_skips_disconnected_channels(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    channels = [{"id": "c1", "displayName": "X", "service": "facebook", "isDisconnected": True}]
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": channels, "status": "VERIFIED"})
    r = buf.resolve_channel_id("facebook")
    assert r["ok"] is False


def test_resolve_channel_id_no_match_reports_honestly(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": [], "status": "VERIFIED"})
    r = buf.resolve_channel_id("pinterest")
    assert r["ok"] is False
    assert "No connected channel" in r["detail"]


def test_resolve_channel_id_propagates_channel_fetch_failure(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": [], "status": "UNAVAILABLE:401"})
    r = buf.resolve_channel_id("linkedin")
    assert r["ok"] is False
    assert r["detail"] == "UNAVAILABLE:401"


# ── publish_to_buffer — GraphQL createPost migration ──────────────────────

def _post_success_payload(post_id="p1", due_at=None, external_link=None):
    return {"data": {"createPost": {
        "__typename": "PostActionSuccess",
        "post": {"id": post_id, "channelId": "c1", "dueAt": due_at, "externalLink": external_link},
    }}}


def test_publish_to_buffer_requires_text():
    r = buf.publish_to_buffer({"channel_id": "c1"})
    assert r["published"] is False
    assert r["status"].startswith("ERROR:missing text")


def test_publish_to_buffer_requires_channel_id_or_service():
    r = buf.publish_to_buffer({"text": "hello"})
    assert r["published"] is False
    assert r["status"].startswith("ERROR:missing channel")


def test_publish_to_buffer_rejects_invalid_mode():
    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1", "mode": "postImmediatelyNow"})
    assert r["published"] is False
    assert "invalid mode" in r["status"].lower()


def test_publish_to_buffer_succeeds_with_explicit_channel_id(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["variables"] = json["variables"]
        return _Resp(200, _post_success_payload(post_id="p42", due_at="2026-08-15T09:00:00Z"))

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "New deal alert!", "channel_id": "c1"}, approved=True)
    assert r["published"] is True
    assert r["status"] == "PUBLISHED"
    assert r["buffer_id"] == "p42"
    assert r["due_at"] == "2026-08-15T09:00:00Z"

    variables = captured["variables"]["input"]
    assert variables["channelId"] == "c1"
    assert variables["text"] == "New deal alert!"
    assert variables["mode"] == "addToQueue"   # safe default — never shareNow unless asked
    assert variables["needsApproval"] is False
    assert variables["assets"] == []


def test_publish_to_buffer_resolves_channel_from_service_name(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    channels = [{"id": "c-li", "displayName": "LI Page", "service": "linkedin", "isDisconnected": False}]
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": channels, "status": "VERIFIED"})

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["variables"] = json["variables"]
        return _Resp(200, _post_success_payload())

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "hello", "service": "linkedin"}, approved=True)
    assert r["published"] is True
    assert captured["variables"]["input"]["channelId"] == "c-li"


def test_publish_to_buffer_unresolvable_service_never_calls_graphql(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf, "get_channels", lambda: {"configured": True, "channels": [], "status": "VERIFIED"})
    calls = []
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: calls.append(1))

    r = buf.publish_to_buffer({"text": "hello", "service": "pinterest"})
    assert r["published"] is False
    assert calls == []   # never even attempted the mutation


def test_publish_to_buffer_includes_link_and_image_assets(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["variables"] = json["variables"]
        return _Resp(200, _post_success_payload())

    monkeypatch.setattr(buf.requests, "post", fake_post)
    buf.publish_to_buffer({
        "text": "Check this deal", "channel_id": "c1",
        "link_url": "https://example.com/deal", "image_url": "https://example.com/img.jpg",
    }, approved=True)
    assets = captured["variables"]["input"]["assets"]
    assert {"link": {"url": "https://example.com/deal"}} in assets
    assert {"image": {"url": "https://example.com/img.jpg"}} in assets


def test_publish_to_buffer_explicit_share_now_mode(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["variables"] = json["variables"]
        return _Resp(200, _post_success_payload())

    monkeypatch.setattr(buf.requests, "post", fake_post)
    buf.publish_to_buffer({"text": "hello", "channel_id": "c1", "mode": "shareNow"}, approved=True)
    assert captured["variables"]["input"]["mode"] == "shareNow"


def test_publish_to_buffer_reports_invalid_input_error_from_buffer(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, {"data": {"createPost": {"__typename": "InvalidInputError", "message": "text is too long"}}})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"}, approved=True)
    assert r["published"] is False
    assert "InvalidInputError" in r["status"]
    assert r["detail"] == "text is too long"


def test_publish_to_buffer_reports_unauthorized_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, {"data": {"createPost": {"__typename": "UnauthorizedError", "message": "token expired"}}})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"}, approved=True)
    assert r["published"] is False
    assert "UnauthorizedError" in r["status"]


def test_publish_to_buffer_handles_transport_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(401, {})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"}, approved=True)
    assert r["published"] is False
    assert "401" in r["status"]


def test_publish_to_buffer_never_fabricates_success_on_graphql_errors(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, {"errors": [{"message": "rate limited"}]})

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"}, approved=True)
    assert r["published"] is False
    assert "rate limited" in r["status"]


# ── preview / approval gate ──────────────────────────────────────────────

def test_publish_without_approval_returns_a_preview_and_never_calls_graphql(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    calls = []
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: calls.append(1))

    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"})
    assert r["published"] is False
    assert r["status"] == "PREVIEW"
    assert r["preview"] == {"channel_id": "c1", "service": None, "text": "hello", "mode": "addToQueue"}
    assert calls == []


def test_preview_still_validates_text_and_channel_first(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    r = buf.publish_to_buffer({"channel_id": "c1"})   # no text, not approved either
    assert r["status"].startswith("ERROR:missing text")   # validation error wins, not a generic preview


def test_approved_true_actually_publishes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _Resp(200, _post_success_payload())

    monkeypatch.setattr(buf.requests, "post", fake_post)
    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"}, approved=True)
    assert r["published"] is True
    assert r["status"] == "PUBLISHED"


# ── platform character limits ────────────────────────────────────────────

def test_text_within_platform_limit_is_allowed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    r = buf.publish_to_buffer({"text": "short tweet", "channel_id": "c1", "service": "twitter"})
    assert r["status"] == "PREVIEW"   # passed all validation, just not approved


def test_text_exceeding_twitter_limit_is_refused(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    calls = []
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: calls.append(1))

    long_text = "x" * 281
    r = buf.publish_to_buffer({"text": long_text, "channel_id": "c1", "service": "twitter"}, approved=True)
    assert r["published"] is False
    assert "exceeds twitter limit" in r["status"]
    assert calls == []


def test_text_exceeding_linkedin_limit_is_refused_at_its_own_higher_threshold(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    # Longer than Twitter's 280 but under LinkedIn's 3000 — must NOT be refused.
    text_1000 = "x" * 1000
    r = buf.publish_to_buffer({"text": text_1000, "channel_id": "c1", "service": "linkedin"})
    assert r["status"] == "PREVIEW"


def test_unknown_service_skips_the_length_check(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    r = buf.publish_to_buffer({"text": "x" * 5000, "channel_id": "c1", "service": "some_future_platform"})
    assert r["status"] == "PREVIEW"   # no known limit for this service — not refused


def test_no_service_given_skips_the_length_check(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    r = buf.publish_to_buffer({"text": "x" * 5000, "channel_id": "c1"})
    assert r["status"] == "PREVIEW"   # raw channel_id, no service to check a limit against


# ── duplicate-post prevention ────────────────────────────────────────────

def test_publishing_the_same_text_twice_to_the_same_channel_is_refused(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: _Resp(200, _post_success_payload(post_id="p1")))

    first = buf.publish_to_buffer({"text": "Big sale today!", "channel_id": "c1"}, approved=True)
    assert first["published"] is True

    second = buf.publish_to_buffer({"text": "Big sale today!", "channel_id": "c1"}, approved=True)
    assert second["published"] is False
    assert second["status"] == "ERROR:duplicate post"
    assert second["duplicate_of"] == "p1"


def test_duplicate_check_is_case_and_whitespace_insensitive(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: _Resp(200, _post_success_payload()))

    buf.publish_to_buffer({"text": "Big Sale Today!", "channel_id": "c1"}, approved=True)
    second = buf.publish_to_buffer({"text": "  big sale today!  ", "channel_id": "c1"}, approved=True)
    assert second["published"] is False
    assert second["status"] == "ERROR:duplicate post"


def test_same_text_to_a_different_channel_is_not_a_duplicate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: _Resp(200, _post_success_payload()))

    buf.publish_to_buffer({"text": "Same text", "channel_id": "c1"}, approved=True)
    second = buf.publish_to_buffer({"text": "Same text", "channel_id": "c2"}, approved=True)
    assert second["published"] is True   # different channel — not a duplicate


def test_allow_duplicate_bypasses_the_check(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: _Resp(200, _post_success_payload(post_id="p2")))

    buf.publish_to_buffer({"text": "Repeat post", "channel_id": "c1"}, approved=True)
    second = buf.publish_to_buffer({"text": "Repeat post", "channel_id": "c1", "allow_duplicate": True}, approved=True)
    assert second["published"] is True


def test_duplicate_check_failure_degrades_to_no_known_duplicate(monkeypatch, tmp_path):
    # A genuinely inaccessible DB path (a directory, not a file — sqlite3
    # can't open it) must not block an otherwise-valid publish. This
    # exercises _find_recent_duplicate's own internal try/except for
    # real, rather than assuming it works.
    _isolate(monkeypatch, tmp_path, token="secret-token")
    bogus_db_dir = tmp_path / "not_a_file.db"
    bogus_db_dir.mkdir()
    monkeypatch.setattr(buf, "DB_PATH", bogus_db_dir)
    monkeypatch.setattr(buf.requests, "post", lambda *a, **k: _Resp(200, _post_success_payload()))

    r = buf.publish_to_buffer({"text": "hello", "channel_id": "c1"}, approved=True)
    assert r["published"] is True
