"""Daily Deal Finders: one objective in, a truthful execution record out.

The bug these guard: core/headless/ui.py caps a tool chain at
_MAX_TOOL_CALL_ROUNDS = 4 while this pipeline needs about eight, so
"find today's best deal and post it" always exhausted the loop and fell
through to "try breaking the request into smaller steps" — making the user
the orchestrator. The workflow now decomposes and executes in one call.

The other half of these tests is honesty. A run that finds nothing, or
stops at the approval gate, must SAY so — never report a product that does
not exist or a post that was never sent.
"""
import pytest

from actions import ddf_workflow as wf


def _stub(monkeypatch, *, configured=False, discovered=0, tracked=None,
          rank=None, post=None, publish_ok=True, buffer_ok=True):
    from actions import ddf_discovery, daily_deal_finders as ddf, buffer_integration as buf
    monkeypatch.setattr(ddf_discovery, "is_configured", lambda: configured)
    monkeypatch.setattr(ddf_discovery, "discover_new_products",
                        lambda **k: {"ok": True, "state": "OK", "saved": discovered})
    monkeypatch.setattr(ddf, "get_top_products", lambda **k: list(tracked or []))
    monkeypatch.setattr(ddf, "rank_products", lambda c: list(rank if rank is not None else c))
    monkeypatch.setattr(ddf, "get_product", lambda pid: next(
        (p for p in (tracked or []) if p.get("id") == pid), None))
    monkeypatch.setattr(ddf, "save_product", lambda p: {"ok": True, "id": p.get("id")})
    monkeypatch.setattr(ddf, "prepare_post", lambda p: post if post is not None else {"text": f"Deal: {p['name']}"})
    monkeypatch.setattr(ddf, "advance_to_published",
                        lambda pid, approved=False: {"ok": publish_ok, "status": "published" if approved else "approved_pending_publish",
                                                     "detail": None if publish_ok else "lifecycle failed"})
    monkeypatch.setattr(buf, "publish_to_buffer",
                        lambda post, approved=False: {"ok": buffer_ok, "id": "buf-1"} if buffer_ok
                        else {"ok": False, "detail": "buffer rejected"})


PRODUCT = {"id": "p-1", "name": "Rotary Hammer", "price": 429.0,
           "affiliate_url": "https://example.com/aff/p-1", "retailer": "amazon"}


# ══ AUTONOMOUS DECOMPOSITION ═════════════════════════════════════════════

def test_a_whole_objective_runs_without_asking_the_user_to_decompose(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT])
    out = wf.run_objective("Find today's best deal for Daily Deal Finders and post it")

    plan = next(s for s in out["steps"] if s["step"] == "decompose")
    assert plan["status"] == wf.OK
    assert "find" in plan["detail"]["plan"] and "publish" in plan["detail"]["plan"]
    # Nothing in the outcome may push the work back to the user.
    assert "smaller" not in out["summary"].lower()
    assert "break" not in out["summary"].lower()


def test_every_pipeline_stage_is_executed_and_recorded(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT])
    out = wf.run_objective()
    steps = [s["step"] for s in out["steps"]]
    for expected in ("decompose", "find", "evaluate", "affiliate_link",
                     "catalog", "content", "publish_lifecycle"):
        assert expected in steps, f"{expected} never ran"


def test_each_step_records_the_tool_and_arguments_it_used(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT])
    out = wf.run_objective()
    catalog = next(s for s in out["steps"] if s["step"] == "catalog")
    assert catalog["tool"].startswith("daily_deal_finders.")
    assert catalog["args"]["product_id"] == "p-1"
    evaluate = next(s for s in out["steps"] if s["step"] == "evaluate")
    assert evaluate["result_count"] == 1


# ══ FALLBACK ═════════════════════════════════════════════════════════════

def test_an_unconfigured_search_source_falls_back_to_the_catalogue(monkeypatch):
    _stub(monkeypatch, configured=False, tracked=[PRODUCT])
    out = wf.run_objective()

    find = next(s for s in out["steps"] if s["step"] == "find")
    assert find["status"] == wf.SKIPPED
    assert "PRODUCT_DATA_API_KEY" in str(find["detail"])
    fallback = next(s for s in out["steps"] if s["step"] == "find_fallback")
    assert fallback["status"] == wf.OK and fallback["result_count"] == 1
    assert out["selected_product"]["id"] == "p-1", "the fallback candidate was not used"


def test_a_live_search_returning_nothing_still_falls_back(monkeypatch):
    _stub(monkeypatch, configured=True, discovered=0, tracked=[PRODUCT])
    out = wf.run_objective()
    assert next(s for s in out["steps"] if s["step"] == "find")["status"] == wf.SKIPPED
    assert out["selected_product"]["id"] == "p-1"


def test_a_search_that_raises_is_recorded_and_the_run_continues(monkeypatch):
    from actions import ddf_discovery
    _stub(monkeypatch, configured=True, tracked=[PRODUCT])
    monkeypatch.setattr(ddf_discovery, "discover_new_products",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("provider down")))
    out = wf.run_objective()
    find = next(s for s in out["steps"] if s["step"] == "find")
    assert find["status"] == wf.BLOCKED and "provider down" in find["error"]
    assert out["selected_product"]["id"] == "p-1", "one failing source aborted the run"


# ══ TRUTHFULNESS ═════════════════════════════════════════════════════════

def test_finding_nothing_is_reported_as_nothing(monkeypatch):
    _stub(monkeypatch, tracked=[])
    out = wf.run_objective()
    assert out["selected_product"] is None
    assert out["published"] is False
    assert "no candidate products" in out["summary"].lower()


def test_a_run_never_claims_a_post_it_did_not_send(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT])
    out = wf.run_objective()            # approved defaults to False
    assert out["published"] is False
    assert "not published" in out["summary"].lower()
    social = next(s for s in out["steps"] if s["step"] == "social_publish")
    assert social["status"] == wf.BLOCKED
    assert "approval" in social["error"].lower()


def test_a_missing_affiliate_link_blocks_rather_than_inventing_one(monkeypatch):
    bad = {"id": "p-2", "name": "No Link Product"}
    _stub(monkeypatch, tracked=[bad])
    out = wf.run_objective()
    aff = next(s for s in out["steps"] if s["step"] == "affiliate_link")
    assert aff["status"] == wf.BLOCKED
    assert out["published"] is False
    assert next(s for s in out["steps"] if s["step"] == "content")["status"] == wf.SKIPPED


def test_the_exact_blocked_dependency_is_identified(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT], publish_ok=False)
    out = wf.run_objective(approved=True)
    assert out["blocked"], "a failed publish left no blocked record"
    assert any("lifecycle" in b["error"] for b in out["blocked"])


# ══ APPROVAL BOUNDARY ════════════════════════════════════════════════════

def test_publishing_requires_approval(monkeypatch):
    published = {"n": 0}
    from actions import buffer_integration as buf
    _stub(monkeypatch, tracked=[PRODUCT])
    monkeypatch.setattr(buf, "publish_to_buffer",
                        lambda post, approved=False: published.__setitem__("n", published["n"] + 1) or {"ok": True})
    wf.run_objective()
    assert published["n"] == 0, "content was published without approval"


def test_an_approved_run_completes_the_publish(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT])
    out = wf.run_objective(approved=True)
    assert out["published"] is True
    assert "published" in out["summary"].lower()
    assert next(s for s in out["steps"] if s["step"] == "social_publish")["status"] == wf.OK


def test_a_failed_social_publish_is_not_reported_as_success(monkeypatch):
    _stub(monkeypatch, tracked=[PRODUCT], buffer_ok=False)
    out = wf.run_objective(approved=True)
    assert out["published"] is False
    social = next(s for s in out["steps"] if s["step"] == "social_publish")
    assert social["status"] == wf.BLOCKED and "buffer rejected" in social["error"]


def test_the_model_facing_tool_cannot_pass_approval(monkeypatch):
    """The executor must never forward an approval flag from the model —
    a natural-language request is not a human approval."""
    import inspect, re
    from core.headless import tool_executor
    src = inspect.getsource(tool_executor)
    block = src[src.index('if daction == "run_workflow":'):]
    block = block[:block.index('elif daction == "add_product":')]
    # Strip comments — the prose there explains the rule, it does not break it.
    code = "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"approved\s*=", code), (
        "the workflow dispatch passes an approval flag"
    )
    assert "args.get(\"approved\")" not in code and "args.get('approved')" not in code, (
        "the workflow dispatch reads approval from model-supplied arguments"
    )


# ══ LOGGING ══════════════════════════════════════════════════════════════

def test_the_run_is_logged_to_operating_memory(monkeypatch):
    from actions import operating_memory as mem
    _stub(monkeypatch, tracked=[PRODUCT])
    wf.run_objective("Find today's best deal and post it")
    entries = mem.recall(source="ddf_workflow", limit=1)
    assert entries, "the DDF run was never recorded"
    assert entries[0]["data"]["published"] is False
    assert any(s["step"] == "catalog" for s in entries[0]["data"]["steps"])


def test_a_logging_failure_does_not_break_the_run(monkeypatch):
    from actions import operating_memory as mem
    _stub(monkeypatch, tracked=[PRODUCT])
    monkeypatch.setattr(mem, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    out = wf.run_objective()
    assert out["ok"] is True and out["selected_product"]["id"] == "p-1"
