"""Business-module plug-in architecture (Lee's autonomous-CEO/COS spec,
Section NINTH: "create a clean extensible business-module architecture so
another company can be plugged into the CEO operating loop without
rebuilding the whole system").

BusinessModule is the uniform shape actions/ceo_operating_cycle.py iterates
over instead of hand-wiring each business's signals into the cycle
directly. Adding a new business — or deepening an existing stub into a
real implementation — means writing one gather() method and registering it
in BUSINESS_MODULES below; the cycle itself never changes.

Two modules are genuinely REAL today, and deliberately thin wrappers
around already-existing, already-tested data sources rather than new
logic (per Lee's instruction not to rebuild what already works):
  * BuildProModule   -> actions/buildpro_data.py (via buildpro_intelligence,
                        which actions/executive_brief.py already calls)
  * DDFModule        -> actions/daily_deal_finders.py

CareerRocketModule and AirbnbModule are explicit, honest NOT_IMPLEMENTED
stubs. Grepping this codebase turns up no CareerRocket data layer (no
careerrocket_data.py equivalent to buildpro_data.py — just a label used by
email_classification.py/nucleus_hierarchy.py) and zero references to
Airbnb anywhere. Registering them here — rather than fabricating a
"working" implementation — is what actually satisfies Section NINTH: the
cycle already has a slot for them, and get_situational_picture() already
reports them as not-yet-implemented instead of silently omitting them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ModuleSignal:
    kind: str              # "opportunity" | "risk" | "followup" | "info"
    title: str
    detail: str = ""
    severity: int = 1      # 1 low .. 3 high — same scale priorities_engine uses
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "detail": self.detail,
                "severity": self.severity, "data": self.data}


class BusinessModule(Protocol):
    id: str
    name: str
    implemented: bool

    def gather(self) -> list[ModuleSignal]:
        """Real (never fabricated) signals for this business, as of right
        now. An unimplemented module returns []; its `implemented=False`
        is what the cycle/report actually check, not an empty list, which
        could otherwise be mistaken for 'implemented, nothing going on'."""
        ...


@dataclass
class BuildProModule:
    id: str = "buildpro"
    name: str = "BuildPro Recruiting"
    implemented: bool = True

    def gather(self) -> list[ModuleSignal]:
        from actions import buildpro_data as bd
        try:
            candidate_followups = bd.list_candidates_needing_followup(limit=10)
            client_followups = bd.list_clients_needing_followup(limit=10)
            unmatched_jobs = bd.list_unmatched_open_jobs(limit=10)
        except Exception as exc:
            return [ModuleSignal("risk", "BuildPro data unavailable", str(exc), severity=2)]

        signals: list[ModuleSignal] = []
        for c in candidate_followups:
            signals.append(ModuleSignal(
                "followup", f"Candidate follow-up due: {c.get('name') or c.get('email') or c.get('id')}",
                "No contact recorded within the follow-up window.", severity=1,
                data={"candidate_id": c.get("id")},
            ))
        for c in client_followups:
            signals.append(ModuleSignal(
                "followup", f"Client follow-up due: {c.get('name') or c.get('id')}",
                "No contact recorded within the follow-up window.", severity=1,
                data={"client_id": c.get("id")},
            ))
        for j in unmatched_jobs:
            signals.append(ModuleSignal(
                "opportunity", f"Open job with no strong match: {j.get('title') or j.get('id')}",
                "Sourcing/prospecting may be needed.", severity=2,
                data={"job_id": j.get("id")},
            ))
        return signals


@dataclass
class DDFModule:
    id: str = "ddf"
    name: str = "Daily Deal Finders"
    implemented: bool = True

    def gather(self) -> list[ModuleSignal]:
        from actions import daily_deal_finders as ddf
        from actions import ddf_discovery
        try:
            trending = ddf.get_trending_deals(limit=5)
            high_ticket = ddf.select_daily_high_ticket_picks(limit=2)
            todays = ddf.get_todays_deals(limit=200)
        except Exception as exc:
            return [ModuleSignal("risk", "DDF data unavailable", str(exc), severity=2)]

        signals: list[ModuleSignal] = []
        if not todays:
            signals.append(ModuleSignal(
                "risk", "No deals discovered today",
                "Product pipeline is empty for today — discovery may be starved."
                + ("" if ddf_discovery.is_configured() else " (No product-data API key configured — see ddf_discovery.py.)"),
                severity=2,
            ))
        if high_ticket:
            signals.append(ModuleSignal(
                "opportunity", f"{len(high_ticket)} high-ticket pick(s) ready",
                "; ".join(p.get("name", "") for p in high_ticket), severity=1,
                data={"product_ids": [p.get("product_id") for p in high_ticket]},
            ))
        if trending:
            signals.append(ModuleSignal(
                "info", f"{len(trending)} product(s) currently trending",
                "; ".join(p.get("name", "") for p in trending), severity=1,
                data={"product_ids": [p.get("product_id") for p in trending]},
            ))
        return signals


@dataclass
class CareerRocketModule:
    id: str = "careerrocket"
    name: str = "CareerRocket Pro"
    implemented: bool = False

    def gather(self) -> list[ModuleSignal]:
        return [ModuleSignal(
            "risk", "CareerRocket has no operating workflow yet",
            "No dedicated data layer exists (unlike BuildPro's buildpro_data.py) — "
            "only a classification label. See remediation order.", severity=1,
        )]


@dataclass
class AirbnbModule:
    id: str = "airbnb"
    name: str = "Airbnb / Other Operations"
    implemented: bool = False

    def gather(self) -> list[ModuleSignal]:
        return [ModuleSignal(
            "info", "Airbnb is not wired into JARVIS",
            "Zero references anywhere in this codebase — registered here only so it "
            "has a slot to plug into if/when Lee decides to activate it.", severity=1,
        )]


BUSINESS_MODULES: dict[str, "BusinessModule"] = {
    "buildpro": BuildProModule(),
    "ddf": DDFModule(),
    "careerrocket": CareerRocketModule(),
    "airbnb": AirbnbModule(),
}


def gather_all() -> dict[str, dict[str, Any]]:
    """One snapshot across every registered module — implemented or not.
    Never raises: a module whose gather() itself blows up is reported as a
    risk signal for that module rather than aborting the whole snapshot."""
    out: dict[str, dict[str, Any]] = {}
    for module_id, module in BUSINESS_MODULES.items():
        try:
            signals = module.gather() if module.implemented else module.gather()
        except Exception as exc:
            signals = [ModuleSignal("risk", f"{module.name} module raised an error", str(exc), severity=2)]
        out[module_id] = {
            "name": module.name,
            "implemented": module.implemented,
            "signals": [s.to_dict() for s in signals],
        }
    return out
