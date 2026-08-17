"""main.py's "buildpro_matching" voice tool — add_candidate (deduplicated,
local write, ungated) / score / match_job / match_candidate / top_matches
wiring into actions/buildpro_data.py + actions/buildpro_matching.py.

No live network calls anywhere here — buildpro_data/buildpro_matching are
purely local (SQLite), so these are monkeypatched the same way as every
other tool test this session, not because anything here is remote, but to
keep the dispatcher tests isolated from real DB state.

Every response phrasing is checked to never claim an objective ranking or
an automated hiring decision, per this prompt's explicit requirement.
"""
import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_live(name="jarvis_main_buildpro_matching"):
    main = load_module(name, "main.py")
    live = object.__new__(main.JarvisLive)
    return main, live


def _ui_stub():
    return type("UIStub", (), {
        "muted": False,
        "set_state": lambda self, s: None,
        "write_log": lambda self, m: None,
    })()


def _make_fc(**args):
    return type("FC", (), {"id": "call-1", "name": "buildpro_matching", "args": args})()


def _run(coro):
    return asyncio.run(coro)


def _live(main):
    live = object.__new__(main.JarvisLive)
    live.ui = _ui_stub()
    live._dashboard = None
    live._loop = None
    return live


def test_tool_declared_for_gemini():
    main, _ = _new_live()
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert "buildpro_matching" in names


def test_tool_description_disclaims_objectivity_and_hiring_decisions():
    main, _ = _new_live()
    tool = next(t for t in main.TOOL_DECLARATIONS if t["name"] == "buildpro_matching")
    desc = tool["description"].lower()
    assert "not an objective" in desc or "not objective" in desc
    assert "never a hiring decision" in desc or "not a hiring decision" in desc
    assert "human" in desc


# ── add_candidate (deduplicated, local write, ungated) ───────────────

def test_add_candidate_without_name_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.buildpro_data, "upsert_candidate",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="add_candidate")))
    assert called["n"] == 0
    assert "name" in response.response["result"].lower()


def test_add_candidate_forwards_fields_and_reports_action(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_upsert(name, email="", **fields):
        captured.update(name=name, email=email, fields=fields)
        return (1, "created")

    monkeypatch.setattr(main.buildpro_data, "upsert_candidate", _fake_upsert)

    response = _run(live._execute_tool(_make_fc(
        action="add_candidate", name="Jane Doe", email="jane@example.com",
        title="Electrician", years_experience=5,
    )))

    assert captured["name"] == "Jane Doe"
    assert captured["email"] == "jane@example.com"
    assert captured["fields"]["title"] == "Electrician"
    assert captured["fields"]["years_experience"] == 5
    assert "created" in response.response["result"].lower()


def test_add_candidate_reports_updated_on_dedup(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_data, "upsert_candidate", lambda name, email="", **f: (1, "updated"))

    response = _run(live._execute_tool(_make_fc(action="add_candidate", name="Jane Doe", email="jane@example.com")))
    assert "updated" in response.response["result"].lower()


# ── score (informational, never automated decision) ──────────────────

def test_score_without_ids_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.buildpro_matching, "score_match",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="score")))
    assert called["n"] == 0


def test_score_reports_missing_candidate_or_job(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_data, "get_candidate", lambda cid: None)
    monkeypatch.setattr(main.buildpro_data, "get_job", lambda jid: {"id": 1})

    response = _run(live._execute_tool(_make_fc(action="score", candidate_id=1, job_id=1)))
    assert "couldn't find" in response.response["result"].lower()


def test_score_includes_rationale_and_disclaims_objectivity(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_data, "get_candidate", lambda cid: {"id": cid, "title": "Electrician"})
    monkeypatch.setattr(main.buildpro_data, "get_job", lambda jid: {"id": jid, "title": "Electrician"})
    monkeypatch.setattr(main.buildpro_matching, "score_match", lambda c, j: {
        "score": 85.0, "rationale": "Title matches exactly.", "factors": {},
    })

    response = _run(live._execute_tool(_make_fc(action="score", candidate_id=1, job_id=2)))
    result = response.response["result"]
    assert "85" in result
    assert "Title matches exactly" in result
    assert "not an objective ranking" in result.lower()


def test_score_reports_none_score_honestly(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_data, "get_candidate", lambda cid: {"id": cid})
    monkeypatch.setattr(main.buildpro_data, "get_job", lambda jid: {"id": jid})
    monkeypatch.setattr(main.buildpro_matching, "score_match", lambda c, j: {
        "score": None, "rationale": "Insufficient data.", "factors": {},
    })

    response = _run(live._execute_tool(_make_fc(action="score", candidate_id=1, job_id=2)))
    assert "not enough shared data" in response.response["result"].lower()


# ── match_job / match_candidate ───────────────────────────────────────

def test_match_job_without_job_id_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.buildpro_matching, "generate_matches_for_job",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="match_job")))
    assert called["n"] == 0


def test_match_job_reports_counts_and_disclaims_hiring_decisions(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_matching, "generate_matches_for_job", lambda job_id, min_score=None: [
        {"candidate_id": 1, "match_id": 1, "score": 90.0, "rationale": "x", "stored": True},
        {"candidate_id": 2, "match_id": None, "score": 40.0, "rationale": "y", "stored": False},
    ])

    response = _run(live._execute_tool(_make_fc(action="match_job", job_id=1)))
    result = response.response["result"]
    assert "Scored 2 candidate" in result
    assert "1 stored" in result
    assert "not hiring decisions" in result.lower()


def test_match_job_forwards_min_score(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake(job_id, min_score=None):
        captured["min_score"] = min_score
        return []

    monkeypatch.setattr(main.buildpro_matching, "generate_matches_for_job", _fake)
    _run(live._execute_tool(_make_fc(action="match_job", job_id=1, min_score=70)))
    assert captured["min_score"] == 70.0


def test_match_job_missing_job_raises_value_error_is_reported(monkeypatch):
    main, _ = _new_live()
    live = _live(main)

    def _raise(job_id, min_score=None):
        raise ValueError(f"No job with id {job_id}")

    monkeypatch.setattr(main.buildpro_matching, "generate_matches_for_job", _raise)
    response = _run(live._execute_tool(_make_fc(action="match_job", job_id=999)))
    assert "No job with id 999" in response.response["result"]


def test_match_candidate_without_candidate_id_is_refused(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    called = {"n": 0}
    monkeypatch.setattr(main.buildpro_matching, "generate_matches_for_candidate",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    response = _run(live._execute_tool(_make_fc(action="match_candidate")))
    assert called["n"] == 0


def test_match_candidate_reports_counts(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_matching, "generate_matches_for_candidate", lambda cid, min_score=None: [
        {"job_id": 1, "match_id": 1, "score": 90.0, "rationale": "x", "stored": True},
    ])

    response = _run(live._execute_tool(_make_fc(action="match_candidate", candidate_id=1)))
    result = response.response["result"]
    assert "Scored this candidate against 1 open job" in result
    assert "1 stored" in result


# ── top_matches ────────────────────────────────────────────────────────

def test_top_matches_reports_none_found(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    monkeypatch.setattr(main.buildpro_data, "list_matches", lambda **k: [])

    response = _run(live._execute_tool(_make_fc(action="top_matches", job_id=1)))
    assert "no stored matches" in response.response["result"].lower()


def test_top_matches_forwards_ids_and_limit(monkeypatch):
    main, _ = _new_live()
    live = _live(main)
    captured = {}

    def _fake_list(candidate_id=None, job_id=None, limit=50):
        captured.update(candidate_id=candidate_id, job_id=job_id, limit=limit)
        return [{"candidate_id": 1, "job_id": 2, "match_score": 88.0}]

    monkeypatch.setattr(main.buildpro_data, "list_matches", _fake_list)

    response = _run(live._execute_tool(_make_fc(action="top_matches", job_id=2, limit=5)))
    assert captured["job_id"] == 2
    assert captured["candidate_id"] is None
    assert captured["limit"] == 5
    assert "88" in response.response["result"]


def test_unknown_buildpro_matching_action_reports_clearly():
    main, _ = _new_live()
    live = _live(main)

    response = _run(live._execute_tool(_make_fc(action="hire_them")))
    assert "unknown buildpro_matching action" in response.response["result"].lower()
