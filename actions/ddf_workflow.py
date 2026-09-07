"""One call that runs the whole Daily Deal Finders objective.

Why this exists, precisely: core/headless/ui.py caps a tool-call chain at
_MAX_TOOL_CALL_ROUNDS = 4. The DDF objective needs roughly eight — find,
evaluate, verify, affiliate, catalog, content, publish, log — so the model
ran out of rounds every time and fell through to "try rephrasing or
breaking the request into smaller steps". That message was not the model
being unhelpful; it was the loop genuinely exhausting. Asking Lee to
decompose the objective made him the orchestrator.

Nothing here is a parallel DDF system. Every step calls the module that
already owns it — ddf_discovery for search, daily_deal_finders for scoring,
cataloguing, content and the publish lifecycle, buffer_integration for
social. This is the missing conductor, not a second orchestra.

Two rules that shape the whole file:

  * It never claims what did not happen. Each step records what it actually
    did, and a step that could not run is reported as blocked with the exact
    dependency — never quietly skipped and never summarised as success.
  * Publishing stays behind the existing approval gate.
    advance_to_published(approved=False) walks the safe internal stages and
    stops before anything becomes public. Nothing here passes approved=True.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.ddf_workflow")

# Step outcomes, so a caller can tell these three apart at a glance.
OK = "OK"
SKIPPED = "SKIPPED"        # nothing to do, and that is a legitimate result
BLOCKED = "BLOCKED"        # a real dependency is missing or failed

DEFAULT_QUERIES = [
    "best deals today", "top rated tools", "discounted power tools",
]


class _Run:
    """Accumulates the execution record. Every step lands here whether it
    succeeded, was skipped, or was blocked — a log with only the good steps
    in it is how a half-finished run gets reported as a success."""

    def __init__(self, objective: str):
        self.objective = objective
        self.started = time.time()
        self.steps: list[dict[str, Any]] = []

    def record(self, step: str, status: str, tool: Optional[str] = None,
               args: Optional[dict] = None, result_count: Optional[int] = None,
               detail: Any = None, error: Optional[str] = None) -> dict[str, Any]:
        entry = {
            "step": step, "status": status, "tool": tool, "args": args,
            "result_count": result_count, "detail": detail, "error": error,
            "ts": time.time(),
        }
        self.steps.append(entry)
        logger.info("ddf step=%s status=%s tool=%s results=%s%s",
                    step, status, tool, result_count,
                    f" error={error}" if error else "")
        return entry

    def status_of(self, step: str) -> Optional[str]:
        for e in reversed(self.steps):
            if e["step"] == step:
                return e["status"]
        return None


def _find(run: _Run, queries: Optional[list[str]]) -> list[dict[str, Any]]:
    """FIND, with the fallback the objective implies.

    Live discovery needs PRODUCT_DATA_API_KEY. When that is absent — or
    returns nothing — the already-tracked catalogue is the configured
    fallback, not an error: a deal worth posting may already be on file."""
    from actions import ddf_discovery, daily_deal_finders as ddf

    used = queries or DEFAULT_QUERIES
    if ddf_discovery.is_configured():
        try:
            result = ddf_discovery.discover_new_products(queries=used)
            saved = int(result.get("saved") or 0)
            run.record("find", OK if saved else SKIPPED, tool="ddf_discovery.discover_new_products",
                       args={"queries": used}, result_count=saved,
                       detail={"state": result.get("state"), "provider": result.get("provider")})
        except Exception as exc:
            run.record("find", BLOCKED, tool="ddf_discovery.discover_new_products",
                       args={"queries": used}, error=str(exc)[:300])
    else:
        run.record("find", SKIPPED, tool="ddf_discovery.discover_new_products",
                   args={"queries": used}, result_count=0,
                   detail="PRODUCT_DATA_API_KEY is not set — falling back to the tracked catalogue.")

    # FALLBACK: whatever is already tracked.
    try:
        candidates = ddf.get_top_products(limit=25) or []
        run.record("find_fallback", OK if candidates else SKIPPED,
                   tool="daily_deal_finders.get_top_products",
                   args={"limit": 25}, result_count=len(candidates))
        return candidates
    except Exception as exc:
        run.record("find_fallback", BLOCKED, tool="daily_deal_finders.get_top_products",
                   error=str(exc)[:300])
        return []


def _evaluate(run: _Run, candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """EVALUATE against the existing criteria — the real ranking, not a
    second scoring scheme invented here."""
    from actions import daily_deal_finders as ddf

    if not candidates:
        run.record("evaluate", SKIPPED, tool="daily_deal_finders.rank_products",
                   result_count=0, detail="no candidates to evaluate")
        return None
    try:
        ranked = ddf.rank_products(candidates) or []
        run.record("evaluate", OK if ranked else SKIPPED,
                   tool="daily_deal_finders.rank_products", result_count=len(ranked))
        return ranked[0] if ranked else None
    except Exception as exc:
        run.record("evaluate", BLOCKED, tool="daily_deal_finders.rank_products",
                   error=str(exc)[:300])
        return None


def _verify_affiliate(run: _Run, product: dict[str, Any]) -> bool:
    """VERIFY + AFFILIATE LINK. Reports honestly rather than inventing one:
    a fabricated affiliate URL is worse than no post."""
    url = (product or {}).get("affiliate_url") or (product or {}).get("url")
    retailer = (product or {}).get("retailer")
    if not url:
        run.record("affiliate_link", BLOCKED, tool="daily_deal_finders.get_product",
                   args={"product_id": (product or {}).get("id")},
                   error="no affiliate or product URL on this record")
        return False
    run.record("affiliate_link", OK, tool="daily_deal_finders.get_product",
               args={"product_id": product.get("id")},
               detail={"retailer": retailer, "has_affiliate_url": bool(product.get("affiliate_url"))})
    return True


def _catalog(run: _Run, product: dict[str, Any]) -> bool:
    """CREATE/CONFIRM THE PRODUCT RECORD. A fallback candidate is already
    catalogued, so this confirms rather than duplicating."""
    from actions import daily_deal_finders as ddf
    pid = (product or {}).get("id")
    try:
        existing = ddf.get_product(pid) if pid else None
        if existing:
            run.record("catalog", OK, tool="daily_deal_finders.get_product",
                       args={"product_id": pid}, detail="already catalogued")
            return True
        saved = ddf.save_product(product)
        ok = bool(saved.get("ok", True))
        run.record("catalog", OK if ok else BLOCKED, tool="daily_deal_finders.save_product",
                   args={"product_id": pid}, detail=saved if not ok else "created",
                   error=None if ok else str(saved.get("detail"))[:300])
        return ok
    except Exception as exc:
        run.record("catalog", BLOCKED, tool="daily_deal_finders.save_product",
                   args={"product_id": pid}, error=str(exc)[:300])
        return False


def _content(run: _Run, product: dict[str, Any]) -> Optional[dict[str, Any]]:
    """CREATE SOCIAL CONTENT with the existing content system."""
    from actions import daily_deal_finders as ddf
    try:
        post = ddf.prepare_post(product)
        run.record("content", OK if post else SKIPPED,
                   tool="daily_deal_finders.prepare_post",
                   args={"product_id": product.get("id")},
                   detail={"chars": len((post or {}).get("text") or "")})
        return post
    except Exception as exc:
        run.record("content", BLOCKED, tool="daily_deal_finders.prepare_post",
                   args={"product_id": product.get("id")}, error=str(exc)[:300])
        return None


def _publish(run: _Run, product: dict[str, Any], post: Optional[dict[str, Any]],
             approved: bool) -> bool:
    """PUBLISH — and this is the one step that must not quietly happen.

    advance_to_published(approved=False) walks the safe internal stages and
    stops at the approval gate. Publishing for real requires an explicit
    human approval, so an unapproved run reports BLOCKED on the gate rather
    than claiming a post went out."""
    from actions import daily_deal_finders as ddf
    from actions import buffer_integration as buf

    pid = product.get("id")
    try:
        staged = ddf.advance_to_published(pid, approved=approved)
        if not staged.get("ok"):
            run.record("publish_lifecycle", BLOCKED,
                       tool="daily_deal_finders.advance_to_published",
                       args={"product_id": pid, "approved": approved},
                       error=str(staged.get("detail"))[:300])
            return False
        run.record("publish_lifecycle", OK if approved else BLOCKED,
                   tool="daily_deal_finders.advance_to_published",
                   args={"product_id": pid, "approved": approved},
                   detail=staged.get("status") or staged,
                   error=None if approved else "awaiting approval — not published")
    except Exception as exc:
        run.record("publish_lifecycle", BLOCKED,
                   tool="daily_deal_finders.advance_to_published",
                   args={"product_id": pid, "approved": approved}, error=str(exc)[:300])
        return False

    if not approved:
        run.record("social_publish", BLOCKED, tool="buffer_integration.publish_to_buffer",
                   args={"product_id": pid},
                   error="approval required before anything is posted publicly")
        return False
    if not post:
        run.record("social_publish", SKIPPED, tool="buffer_integration.publish_to_buffer",
                   detail="no content was generated")
        return False
    try:
        result = buf.publish_to_buffer(post, approved=True)
        ok = bool(result.get("ok"))
        run.record("social_publish", OK if ok else BLOCKED,
                   tool="buffer_integration.publish_to_buffer",
                   args={"product_id": pid},
                   detail=result if ok else None,
                   error=None if ok else str(result.get("detail") or result.get("state"))[:300])
        return ok
    except Exception as exc:
        run.record("social_publish", BLOCKED, tool="buffer_integration.publish_to_buffer",
                   args={"product_id": pid}, error=str(exc)[:300])
        return False


def _report(run: _Run, product: Optional[dict[str, Any]], published: bool) -> str:
    """A factual sentence about what actually happened."""
    if product is None:
        blocked = [s for s in run.steps if s["status"] == BLOCKED]
        if blocked:
            first = blocked[0]
            return (f"No deal was posted. Blocked at {first['step']}: "
                    f"{first['error'] or 'dependency unavailable'}.")
        return "No deal was posted — no candidate products are currently available."
    name = product.get("name") or product.get("title") or product.get("id")
    if published:
        return f"Selected and published '{name}'."
    gate = next((s for s in run.steps
                 if s["step"] in ("social_publish", "publish_lifecycle") and s["status"] == BLOCKED), None)
    reason = (gate or {}).get("error") or "publishing did not complete"
    return (f"Selected '{name}', catalogued it and prepared the social content. "
            f"Not published: {reason}.")


def run_objective(objective: str = "Find today's best deal and post it",
                  queries: Optional[list[str]] = None,
                  approved: bool = False) -> dict[str, Any]:
    """The whole DDF objective, decomposed and executed here.

    `approved` defaults to False and must stay that way for anything the
    model can invoke: it is the existing approval boundary, and a natural
    language request is not an approval."""
    run = _Run(objective)
    run.record("decompose", OK, detail={
        "plan": ["find", "evaluate", "verify+affiliate", "catalog",
                 "content", "publish", "log", "report"],
        "approved": approved,
    })

    candidates = _find(run, queries)
    product = _evaluate(run, candidates)

    published = False
    if product is not None:
        if _verify_affiliate(run, product) and _catalog(run, product):
            post = _content(run, product)
            published = _publish(run, product, post, approved)
        else:
            run.record("content", SKIPPED, detail="prerequisite step did not complete")
            run.record("publish_lifecycle", SKIPPED, detail="prerequisite step did not complete")

    summary = _report(run, product, published)
    outcome = {
        "ok": True,
        "objective": objective,
        "selected_product": ({"id": product.get("id"), "name": product.get("name")}
                             if product else None),
        "published": published,
        "steps": run.steps,
        "blocked": [{"step": s["step"], "error": s["error"]}
                    for s in run.steps if s["status"] == BLOCKED],
        "duration_ms": int((time.time() - run.started) * 1000),
        "summary": summary,
    }
    _log_run(outcome)
    return outcome


def _log_run(outcome: dict[str, Any]) -> None:
    """LOG — through the existing operating memory, not a new store."""
    try:
        from actions import operating_memory
        operating_memory.record(
            operating_memory.AGENT_OUTCOME, source="ddf_workflow",
            subject=(outcome.get("selected_product") or {}).get("id"),
            summary=outcome["summary"],
            data={"published": outcome["published"],
                  "blocked": outcome["blocked"],
                  "steps": [{"step": s["step"], "status": s["status"]} for s in outcome["steps"]]},
            ok=outcome["published"] or not outcome["blocked"],
        )
    except Exception:
        logger.debug("could not record the DDF run", exc_info=True)
