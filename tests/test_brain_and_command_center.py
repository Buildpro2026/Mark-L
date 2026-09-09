"""Typed operating knowledge, and the one structured read of system state.

jarvis_brain retrieves notes, operating_memory records events,
business_intelligence holds lessons — and between them there was nowhere
to put a durable business FACT with a source and a confidence. So JARVIS
could remember that a task failed, but could not accumulate what it
learned by working, and every research run started from zero.
"""
import pytest

from actions import brain_memory as bm


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from actions import operating_memory
    monkeypatch.setattr(operating_memory, "DB_PATH", tmp_path / "om.db")


# ══ KNOWLEDGE IN, RELEVANT KNOWLEDGE OUT ═════════════════════════════════

def test_a_sourced_fact_is_stored_and_retrieved_for_its_subject():
    bm.remember(bm.FACT, "Turner Construction", "Turner is a data centre GC in Phoenix",
                confidence=0.8, source_url="https://example.com/turner")
    found = bm.recall_for("Turner Construction data centre")
    assert found and found[0]["kind"] == bm.FACT
    assert "data centre GC" in found[0]["content"]
    assert found[0]["source_url"] == "https://example.com/turner"


def test_retrieval_is_targeted_not_a_dump():
    bm.remember(bm.FACT, "Turner Construction", "Turner builds data centres",
                confidence=0.8, source_url="https://x/1")
    bm.remember(bm.FACT, "Buffer publishing", "Buffer posts to four channels",
                confidence=0.8, source_url="https://x/2")
    found = bm.recall_for("Turner Construction")
    assert len(found) == 1, "an unrelated memory was returned"


def test_an_unsourced_claim_cannot_become_confident_knowledge():
    # A model-generated claim must not become durable fact by being
    # written down confidently.
    result = bm.remember(bm.FACT, "Acme Corp", "Acme has 5,000 employees",
                         confidence=0.95)
    assert result["confidence"] <= bm.UNSOURCED_CONFIDENCE_CAP
    assert result["sourced"] is False


def test_an_unknown_kind_is_refused_rather_than_stored_unqueryable():
    assert bm.remember("VIBES", "x", "y")["ok"] is False


def test_a_memory_needs_both_a_subject_and_content():
    assert bm.remember(bm.FACT, "", "content")["ok"] is False
    assert bm.remember(bm.FACT, "subject", "")["ok"] is False


def test_kinds_can_be_filtered():
    bm.remember(bm.FACT, "Dallas market", "Dallas has 40 open VP roles",
                confidence=0.7, source_url="https://x/1")
    bm.remember(bm.LESSON, "Dallas market", "Cold outreach in Dallas underperforms",
                confidence=0.7, source="jarvis_outcome")
    assert len(bm.recall_for("Dallas market", kinds=[bm.LESSON])) == 1


def test_context_carries_confidence_and_source_for_the_reader():
    bm.remember(bm.FACT, "Turner", "Turner builds data centres",
                confidence=0.8, source_url="https://example.com/t")
    context = bm.context_for("Turner")
    assert "FACT" in context and "80%" in context and "example.com" in context


def test_a_broken_store_never_breaks_the_work_that_produced_the_memory(monkeypatch):
    from actions import operating_memory
    monkeypatch.setattr(operating_memory, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert bm.remember(bm.FACT, "x", "y", source="s")["ok"] is False   # must not raise


# ══ RESEARCH BECOMES KNOWLEDGE ═══════════════════════════════════════════

def test_observed_findings_become_facts_and_reported_ones_do_not():
    from actions import web_research as wr
    outcome = {
        "ok": True, "question": "Rotary Hammer XR price",
        "results": [{
            "source_url": "https://shop.example/h",
            "fields": {
                "price": wr.finding("price", 429.0, wr.OBSERVED, "https://shop.example/h"),
                "original_price": wr.finding("original_price", 499.0, wr.REPORTED,
                                             "https://shop.example/h"),
                "rating": wr.unknown("rating"),
            },
        }],
    }
    result = bm.learn_from_research(outcome)
    assert result["stored"] == 2, "the unknown field was stored as knowledge"

    facts = bm.recall_for("Rotary Hammer XR price", kinds=[bm.FACT])
    assert facts and "429.0" in facts[0]["content"]
    assert bm.recall_for("Rotary Hammer XR original_price", kinds=[bm.OBSERVATION])


def test_a_research_run_that_read_nothing_teaches_nothing():
    result = bm.learn_from_research({"ok": False, "results": []})
    assert result["stored"] == 0
    assert "nothing to learn" in result["detail"]


# ══ THE DECISION LAYER READS IT ══════════════════════════════════════════

def test_knowledge_reaches_the_decision_layer(monkeypatch):
    from actions import ceo_decision, jarvis_brain, business_intelligence as biz
    from actions import operating_memory
    monkeypatch.setattr(jarvis_brain, "is_available", lambda: False)
    monkeypatch.setattr(biz, "get_lessons_for", lambda *a, **k: [])
    monkeypatch.setattr(operating_memory, "failure_streak", lambda *a, **k: 0)

    bm.remember(bm.LESSON, "HubSpot sync", "HubSpot sync fails when the portal id is unset",
                confidence=0.8, source="jarvis_outcome")

    context = ceo_decision.context_for(
        {"title": "HubSpot sync failing", "source": "hubspot", "kind": "risk"})
    assert context["knowledge"], "typed knowledge never reached the decision layer"
    assert "knowledge" in context["retrieved"]

    scored = ceo_decision.score_item(
        {"title": "HubSpot sync failing", "source": "hubspot", "severity": 3}, context)
    assert any("prior knowledge" in r for r in scored["reasoning"])


# ══ THE COMMAND CENTER CONTRACT ══════════════════════════════════════════

def test_the_snapshot_isolates_a_failing_section(monkeypatch):
    from actions import command_center_state as ccs
    monkeypatch.setitem(ccs.SECTIONS, "approvals",
                        lambda: (_ for _ in ()).throw(RuntimeError("orchestrator down")))
    monkeypatch.setitem(ccs.SECTIONS, "health", lambda: {"gmail": "CONFIGURED"})

    snap = ccs.snapshot(["approvals", "health"])
    assert snap["sections"]["approvals"]["state"] == ccs.UNAVAILABLE
    assert snap["sections"]["health"]["state"] == ccs.OK
    # "no approvals" and "the approvals store is down" must not look the same.
    assert "approvals" in snap["degraded_sections"]
    assert snap["fully_available"] is False


def test_credential_shaped_values_never_reach_the_ui(monkeypatch):
    from actions import command_center_state as ccs
    monkeypatch.setitem(ccs.SECTIONS, "health",
                        lambda: {"hubspot": {"api_key": "pat-na1-real-secret",
                                             "state": "CONFIGURED"}})
    snap = ccs.snapshot(["health"])
    rendered = str(snap)
    assert "pat-na1-real-secret" not in rendered
    assert "[redacted]" in rendered
    assert "CONFIGURED" in rendered


def test_narrowing_sections_does_not_run_the_others(monkeypatch):
    from actions import command_center_state as ccs
    ran = []
    monkeypatch.setitem(ccs.SECTIONS, "buildpro_matches", lambda: ran.append(1) or {})
    monkeypatch.setitem(ccs.SECTIONS, "health", lambda: {"ok": True})
    ccs.snapshot(["health"])
    assert ran == [], "a narrowed view ran the full matching pass"


def test_an_unknown_section_name_is_ignored_not_an_error():
    from actions import command_center_state as ccs
    snap = ccs.snapshot(["not_a_real_section"])
    assert snap["ok"] is True
    assert snap["sections"] == {}


def test_the_endpoint_requires_a_session(monkeypatch):
    from fastapi.testclient import TestClient
    from core.headless import config
    monkeypatch.setattr(config, "API_TOKEN", "test-token-not-a-real-secret")
    from core.headless.app import create_app
    client = TestClient(create_app(start_background_worker=False), base_url="https://testserver")
    assert client.get("/ui/api/command-center").status_code == 401


# ══ LIVE-RESEARCH ROUTING ════════════════════════════════════════════════

@pytest.mark.parametrize("question", [
    "what is the current price of this drill",
    "find the latest construction news",
    "who owns Turner Construction",
    "compare these two products",
])
def test_a_question_needing_live_information_is_routed_to_research(question):
    from actions import web_research as wr
    assert wr.needs_live_research(question)["needed"] is True


@pytest.mark.parametrize("question", [
    "what needs my approval",
    "show me today's matches",
    "what's on my calendar",
])
def test_a_question_about_jarvis_own_state_is_not_sent_to_the_web(question):
    from actions import web_research as wr
    decision = wr.needs_live_research(question)
    assert decision["needed"] is False
    assert "own state" in decision["reason"]


def test_the_routing_decision_names_the_marker_that_caused_it():
    from actions import web_research as wr
    assert "current" in wr.needs_live_research("the current price")["reason"]
