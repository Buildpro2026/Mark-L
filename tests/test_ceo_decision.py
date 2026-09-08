"""The layer that makes memory participate in decisions.

Every piece existed and none of them met. priorities_engine sorted by a
FIXED severity tier — risk=4, approval=3, stale=2, recommendation=1 — so
revenue, deadlines and prior failures never entered the ordering, and an
agent that had failed four mornings running ranked exactly where it did
the first morning. These pin the arrow between RETRIEVE and DECIDE.
"""
import pytest

from actions import ceo_decision as cd


def _item(**kw):
    base = {"kind": "recommendation", "severity": 1, "title": "", "source": "s"}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    from actions import operating_memory, business_intelligence as biz
    monkeypatch.setattr(operating_memory, "DB_PATH", tmp_path / "om.db")
    monkeypatch.setattr(biz, "DB_PATH", tmp_path / "bi.db")


def _no_memory(monkeypatch, streak=0, lessons=(), brain=""):
    from actions import operating_memory, business_intelligence as biz, jarvis_brain
    monkeypatch.setattr(operating_memory, "recall", lambda **k: [])
    monkeypatch.setattr(operating_memory, "failure_streak", lambda *a, **k: streak)
    monkeypatch.setattr(biz, "get_lessons_for", lambda *a, **k: list(lessons))
    monkeypatch.setattr(jarvis_brain, "is_available", lambda: bool(brain))
    monkeypatch.setattr(jarvis_brain, "recall", lambda q, **k: brain)


# ══ RETRIEVAL IS TARGETED, NOT A DUMP ════════════════════════════════════

def test_retrieval_asks_only_about_this_item(monkeypatch):
    asked = []
    from actions import jarvis_brain
    monkeypatch.setattr(jarvis_brain, "is_available", lambda: True)
    monkeypatch.setattr(jarvis_brain, "recall",
                        lambda q, **k: asked.append((q, k)) or "note text")
    _no_memory(monkeypatch)
    monkeypatch.setattr(jarvis_brain, "recall",
                        lambda q, **k: asked.append((q, k)) or "note text")
    monkeypatch.setattr(jarvis_brain, "is_available", lambda: True)

    cd.context_for(_item(title="HubSpot sync failing for BuildPro", source="hubspot"))
    assert asked, "the Brain was never consulted"
    query, kwargs = asked[0]
    assert "HubSpot" in query
    assert kwargs["max_chars"] <= cd.BRAIN_CHARS_PER_ITEM, "unbounded Brain dump"


def test_a_store_that_fails_does_not_prevent_the_others(monkeypatch):
    from actions import jarvis_brain, operating_memory, business_intelligence as biz
    monkeypatch.setattr(jarvis_brain, "is_available",
                        lambda: (_ for _ in ()).throw(RuntimeError("vault gone")))
    monkeypatch.setattr(operating_memory, "recall", lambda **k: [{"summary": "ok"}])
    monkeypatch.setattr(operating_memory, "failure_streak", lambda *a, **k: 0)
    monkeypatch.setattr(biz, "get_lessons_for", lambda *a, **k: [])

    context = cd.context_for(_item(title="x", source="hubspot"))
    assert context["outcomes"], "a broken Brain silenced operating memory"


def test_an_empty_context_is_a_real_answer(monkeypatch):
    _no_memory(monkeypatch)
    context = cd.context_for(_item(title="something new", source="new_source"))
    assert context["retrieved"] == []
    assert context["brain"] == ""


# ══ THE FACTORS THAT WERE MISSING ════════════════════════════════════════

def test_revenue_attached_to_an_item_raises_its_priority(monkeypatch):
    _no_memory(monkeypatch)
    rich = cd.score_item(_item(title="Placement fee", revenue=18000))
    plain = cd.score_item(_item(title="Placement fee"))
    assert rich["priority_score"] > plain["priority_score"]


def test_revenue_is_read_never_estimated(monkeypatch):
    _no_memory(monkeypatch)
    scored = cd.score_item(_item(title="Some task with no money in it"))
    assert any("no revenue attached" in r for r in scored["reasoning"])


def test_a_named_deadline_raises_urgency(monkeypatch):
    _no_memory(monkeypatch)
    urgent = cd.score_item(_item(title="Contract signature overdue"))
    calm = cd.score_item(_item(title="Contract signature"))
    assert urgent["priority_score"] > calm["priority_score"]


def test_time_already_waited_raises_urgency(monkeypatch):
    _no_memory(monkeypatch)
    waited = cd.score_item(_item(title="Approve it", waited_hours=40))
    fresh = cd.score_item(_item(title="Approve it", waited_hours=0.1))
    assert waited["priority_score"] > fresh["priority_score"]


def test_work_already_underway_does_not_outrank_work_that_can_start(monkeypatch):
    _no_memory(monkeypatch)
    running = cd.score_item(_item(title="Sync", in_progress=True))
    ready = cd.score_item(_item(title="Sync"))
    assert ready["priority_score"] > running["priority_score"]


def test_unmet_dependencies_lower_readiness(monkeypatch):
    _no_memory(monkeypatch)
    blocked = cd.score_item(_item(title="Ship it", depends_on=["a", "b"]))
    assert any("dependenc" in r for r in blocked["reasoning"])


def test_a_user_priority_is_honoured(monkeypatch):
    _no_memory(monkeypatch)
    marked = cd.score_item(_item(title="Do this", user_priority=True))
    assert any("Lee marked this" in r for r in marked["reasoning"])


# ══ PAST FAILURE CHANGES THE FUTURE DECISION ═════════════════════════════

def test_a_repeatedly_failing_source_is_demoted_not_promoted(monkeypatch):
    _no_memory(monkeypatch, streak=0)
    healthy = cd.score_item(_item(title="Sync HubSpot", severity=3, source="hubspot"))
    _no_memory(monkeypatch, streak=2)
    failing = cd.score_item(_item(title="Sync HubSpot", severity=3, source="hubspot"))
    assert failing["priority_score"] < healthy["priority_score"]
    assert any("consecutive prior failure" in r for r in failing["reasoning"])


def test_a_persistently_failing_source_escalates_to_a_human(monkeypatch):
    _no_memory(monkeypatch, streak=cd.ESCALATION_STREAK)
    decision = cd.decide(_item(title="Sync", source="hubspot", permission_level="observe"))
    assert decision["disposition"] == cd.REQUIRE_APPROVAL
    assert decision["escalate"] is True
    assert "known-broken" in decision["why"]


def test_prior_lessons_are_noted_in_the_reasoning(monkeypatch):
    _no_memory(monkeypatch, lessons=[{"title": "that approach failed before"}])
    scored = cd.score_item(_item(title="Try it again"))
    assert any("prior lesson" in r for r in scored["reasoning"])


# ══ THE ORDER ACTUALLY CHANGES ═══════════════════════════════════════════

def test_a_valuable_waiting_approval_outranks_a_bare_risk(monkeypatch):
    # Under the old fixed tier a risk (4) ALWAYS beat an approval (3),
    # whatever was attached to it. That is the behaviour being replaced.
    _no_memory(monkeypatch)
    ranked = cd.prioritize([
        {"kind": "risk", "severity": 4, "title": "A note is stale", "source": "a"},
        {"kind": "approval", "severity": 3, "title": "Approve $18,000 placement fee",
         "source": "b", "waited_hours": 30},
    ])
    assert ranked[0]["kind"] == "approval"


def test_ranking_is_deterministic(monkeypatch):
    _no_memory(monkeypatch)
    items = [_item(title=f"item {i}", severity=2) for i in range(5)]
    assert ([i["title"] for i in cd.prioritize(items)]
            == [i["title"] for i in cd.prioritize(list(reversed(items)))])


def test_every_scored_item_explains_itself(monkeypatch):
    _no_memory(monkeypatch)
    scored = cd.score_item(_item(title="Anything", severity=2))
    assert scored["reasoning"], "a priority nobody can explain is one nobody can correct"
    assert 0.0 <= scored["priority_score"] <= 1.0


def test_the_limit_is_respected(monkeypatch):
    _no_memory(monkeypatch)
    assert len(cd.prioritize([_item(title=str(i)) for i in range(30)], limit=5)) == 5


def test_an_empty_list_is_handled(monkeypatch):
    assert cd.prioritize([]) == []
    assert cd.plan([]) == []


# ══ DISPOSITIONS RESPECT THE APPROVAL GATE ═══════════════════════════════

def test_execute_level_work_always_requires_approval(monkeypatch):
    _no_memory(monkeypatch)
    d = cd.decide(_item(title="Send the emails", permission_level="execute"))
    assert d["disposition"] == cd.REQUIRE_APPROVAL


def test_an_item_already_needing_approval_keeps_needing_it(monkeypatch):
    _no_memory(monkeypatch)
    assert cd.decide(_item(kind="approval", title="x"))["disposition"] == cd.REQUIRE_APPROVAL
    assert cd.decide(_item(title="x", requires_approval=True))["disposition"] == cd.REQUIRE_APPROVAL


def test_nothing_is_ever_lowered_below_require_approval(monkeypatch):
    # The gate is not this module's to move: an approval item stays an
    # approval item no matter how healthy or valuable it looks.
    _no_memory(monkeypatch, streak=0)
    d = cd.decide(_item(kind="approval", title="Huge win", revenue=100000,
                        permission_level="observe"))
    assert d["disposition"] == cd.REQUIRE_APPROVAL


def test_read_only_work_runs_unattended(monkeypatch):
    _no_memory(monkeypatch)
    assert cd.decide(_item(title="Check inbox",
                           permission_level="observe"))["disposition"] == cd.OBSERVE


def test_suggest_level_work_prepares_a_proposal(monkeypatch):
    _no_memory(monkeypatch)
    assert cd.decide(_item(title="Draft it",
                           permission_level="suggest"))["disposition"] == cd.PREPARE


def test_a_risk_is_recommended_rather_than_acted_on(monkeypatch):
    _no_memory(monkeypatch)
    assert cd.decide(_item(kind="risk", title="Something looks wrong",
                           permission_level="observe"))["disposition"] == cd.RECOMMEND


def test_every_decision_states_a_reason(monkeypatch):
    _no_memory(monkeypatch)
    for item in (_item(kind="risk"), _item(permission_level="execute"),
                 _item(permission_level="suggest"), _item()):
        assert cd.decide(item)["why"]


def test_plan_attaches_both_a_score_and_a_disposition(monkeypatch):
    _no_memory(monkeypatch)
    planned = cd.plan([_item(title="A thing", severity=3)])
    assert planned[0]["decision"]["disposition"] in cd.DISPOSITIONS
    assert "priority_score" in planned[0]


# ══ LEARNING WRITES BACK WHERE THE NEXT DECISION READS ═══════════════════

def test_a_failure_is_recorded_where_failure_streak_will_find_it(monkeypatch):
    from actions import operating_memory
    recorded = []
    monkeypatch.setattr(operating_memory, "record",
                        lambda *a, **k: recorded.append(k) or 1)
    cd.record_outcome(_item(title="Sync", source="hubspot"), ok=False, detail="401")
    assert recorded and recorded[0]["ok"] is False
    assert recorded[0]["source"] == "hubspot"


def test_a_failure_also_becomes_a_readable_lesson(monkeypatch):
    from actions import operating_memory, business_intelligence as biz
    monkeypatch.setattr(operating_memory, "record", lambda *a, **k: 1)
    lessons = []
    monkeypatch.setattr(biz, "add_entry", lambda **k: lessons.append(k) or 1)
    cd.record_outcome(_item(title="Sync", source="hubspot"), ok=False, detail="401")
    assert lessons and lessons[0]["category"] == "lessons_learned"


def test_a_routine_success_does_not_become_a_lesson(monkeypatch):
    from actions import operating_memory, business_intelligence as biz
    monkeypatch.setattr(operating_memory, "record", lambda *a, **k: 1)
    lessons = []
    monkeypatch.setattr(biz, "add_entry", lambda **k: lessons.append(k) or 1)
    cd.record_outcome(_item(title="Sync", source="hubspot"), ok=True, verified=True)
    assert lessons == [], "filing every success as a lesson makes the store unreadable"


def test_an_unverified_success_is_treated_as_a_lesson(monkeypatch):
    from actions import operating_memory, business_intelligence as biz
    monkeypatch.setattr(operating_memory, "record", lambda *a, **k: 1)
    lessons = []
    monkeypatch.setattr(biz, "add_entry", lambda **k: lessons.append(k) or 1)
    cd.record_outcome(_item(title="Sync", source="x"), ok=True, verified=False)
    assert lessons


def test_a_store_failure_never_breaks_the_action_that_produced_it(monkeypatch):
    from actions import operating_memory, business_intelligence as biz
    monkeypatch.setattr(operating_memory, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(biz, "add_entry",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("db down")))
    result = cd.record_outcome(_item(title="x"), ok=False)   # must not raise
    assert result["ok"] is True
    assert result["recorded_to"] == []


# ══ THE CEO CYCLE USES IT ════════════════════════════════════════════════

def test_the_cycle_prioritises_through_the_decision_layer(monkeypatch):
    from actions import ceo_operating_cycle as cycle, priorities_engine
    monkeypatch.setattr(priorities_engine, "get_todays_priorities",
                        lambda **k: [{"kind": "risk", "severity": 4, "title": "A",
                                      "source": "a"}])
    _no_memory(monkeypatch)
    out = cycle._prioritize()
    assert out and "priority_score" in out[0]
    assert "decision" in out[0]


def test_a_broken_decision_layer_still_yields_priorities(monkeypatch):
    from actions import ceo_operating_cycle as cycle, priorities_engine, ceo_decision
    raw = [{"kind": "risk", "severity": 4, "title": "A", "source": "a"}]
    monkeypatch.setattr(priorities_engine, "get_todays_priorities", lambda **k: raw)
    monkeypatch.setattr(ceo_decision, "plan",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cycle._prioritize() == raw
