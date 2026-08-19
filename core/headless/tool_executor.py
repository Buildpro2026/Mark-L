"""Shared tool dispatch — the actual logic behind every JARVIS tool call,
extracted from main.py's JarvisLive._execute_tool so it can run without a
desktop UI, an audio stack, or a live Gemini session.

main.py's JarvisLive still owns _execute_tool as the entry point Gemini
Live calls into, but for every tool NOT in tool_registry.SESSION_ONLY_TOOLS
it now just builds a ToolContext and delegates to ToolExecutor.execute()
below — the exact same code that used to live inline in main.py, unchanged
in behavior. This is why every action-module import here mirrors main.py's:
tests monkeypatch the shared module objects (e.g. actions.gmail_integration
itself), not a name inside main.py's namespace, so it doesn't matter which
file holds the `from actions import X` — both point at the same module.

SESSION_ONLY_TOOLS (screen_process, close_camera, shutdown_jarvis,
navigate_command_center) stay in main.py, unchanged, because they need a
real camera/screen/live Gemini session/embedded dashboard — faking that
headlessly would mean pretending a capability exists that doesn't.
save_memory is handled here too (memory writes are genuinely headless-safe)
even though main.py keeps its own fast-path early return for it, to avoid
a UI flicker during an interactive voice turn.
"""
from __future__ import annotations

import json

from actions.file_processor import file_processor
from actions.flight_finder import flight_finder
from actions.open_app import open_app
from actions.weather_report import weather_action
from actions.send_message import send_message
from actions.reminder import reminder
from actions.youtube_video import youtube_video
from actions.desktop import desktop_control
from actions.browser_control import browser_control
from actions.file_controller import file_controller
from actions.code_helper import code_helper
from actions.dev_agent import dev_agent
from actions.web_search import web_search as web_search_action
from actions.computer_control import computer_control
from actions.computer_settings import computer_settings
from actions.game_updater import game_updater
from actions.system_monitor import get_system_status
from actions.agent_orchestrator import orchestrator as agent_orchestrator
from actions import background_monitor
from actions import strategic_objective
from actions import twilio_integration as twilio
from actions import gmail_integration
from actions import calendar_integration
from actions import airtable_integration
from actions import hubspot_integration
from actions import buffer_integration
from actions import buildpro_data
from actions import buildpro_matching
from actions import google_auth
from actions import business_intelligence as biz_intel
from actions import opportunity_engine as opp_engine
from actions import decision_engine
from actions import audit_log
from actions import proactive as proactive_module
from actions import cloud_bridge
from memory.memory_manager import update_memory
from memory import config_manager

# Module-object imports (above), not `from X import func`, on purpose: tests
# monkeypatch e.g. `main.get_proactive_enabled` — a name binding private to
# main.py's own namespace — or the shared module object itself (e.g.
# `main.gmail_integration.send_email`, which IS the same object as
# `gmail_integration.send_email` here, since Python caches modules in
# sys.modules). Binding a function name directly here would read the
# original, unpatched function instead of whatever a test replaced it with.
# Calling through the module object at call time always sees the current
# (possibly monkeypatched) attribute.

from core.headless.context import ToolContext
from core.headless.tool_registry import SESSION_ONLY_TOOLS

import asyncio
from datetime import datetime


class UnknownToolError(Exception):
    pass


class ToolExecutor:
    """One instance per JarvisLive session (desktop) or per headless
    request (API) — holds only a ToolContext, no session/UI state of its
    own. `execute()` mirrors _execute_tool's old dispatch exactly, tool
    for tool, minus the four SESSION_ONLY_TOOLS."""

    def __init__(self, ctx: ToolContext | None = None):
        self.ctx = ctx or ToolContext()

    async def execute(self, name: str, args: dict) -> str:
        if name in SESSION_ONLY_TOOLS:
            raise UnknownToolError(
                f"'{name}' requires a live desktop/voice session and isn't available here."
            )

        ctx = self.ctx
        loop = asyncio.get_event_loop()
        result = "Done."

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                return "ok"
            return "Nothing to save — need both a key and a value."

        elif name == "open_app":
            r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=ctx.ui))
            result = r or f"Opened {args.get('app_name')}."

        elif name == "weather_report":
            r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=ctx.ui))
            result = r or "Weather delivered."

        elif name == "browser_control":
            r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=ctx.ui))
            result = r or "Done."

        elif name == "file_controller":
            r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=ctx.ui))
            result = r or "Done."

        elif name == "send_message":
            r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=ctx.ui, session_memory=None))
            result = r or f"Message sent to {args.get('receiver')}."

        elif name == "reminder":
            r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=ctx.ui))
            result = r or "Reminder set."

        elif name == "youtube_video":
            r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=ctx.ui))
            result = r or "Done."

        elif name == "computer_settings":
            r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=ctx.ui))
            result = r or "Done."

        elif name == "desktop_control":
            r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=ctx.ui))
            result = r or "Done."

        elif name == "code_helper":
            r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=ctx.ui, speak=ctx.speak))
            result = r or "Done."

        elif name == "dev_agent":
            r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=ctx.ui, speak=ctx.speak))
            result = r or "Done."

        elif name == "web_search":
            r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=ctx.ui))
            result = r or "Done."
            _mode = args.get("mode", "search")
            if r and not r.startswith("No results") and not r.startswith("Search failed"):
                _query = args.get("query") or ", ".join(args.get("items", []))
                _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                ctx.ui.show_content(_label, r)

        elif name == "file_processor":
            if not args.get("file_path") and getattr(ctx.ui, "current_file", None):
                args["file_path"] = ctx.ui.current_file
            r = await loop.run_in_executor(
                None,
                lambda: file_processor(parameters=args, player=ctx.ui, speak=ctx.speak)
            )
            result = r or "Done."

        elif name == "computer_control":
            r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=ctx.ui))
            result = r or "Done."

        elif name == "game_updater":
            r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=ctx.ui, speak=ctx.speak))
            result = r or "Done."

        elif name == "flight_finder":
            r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=ctx.ui))
            result = r or "Done."

        elif name == "system_status":
            r = await loop.run_in_executor(None, get_system_status)
            result = str(r)

        elif name == "manage_monitor":
            action = args.get("action", "").lower().strip()
            topic  = args.get("topic", "").strip()
            if action == "add" and topic:
                result = await asyncio.to_thread(background_monitor.add_monitor, topic)
            elif action == "remove" and topic:
                result = await asyncio.to_thread(background_monitor.remove_monitor, topic)
            elif action == "list":
                topics = await asyncio.to_thread(background_monitor.list_monitors)
                result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
            else:
                result = "Specify action (add/remove/list) and a topic."

        elif name == "strategic_objective":
            action = (args.get("action") or "status").strip().lower()
            if action == "log_revenue":
                try:
                    amount = float(args.get("amount"))
                except (TypeError, ValueError):
                    amount = None
                if not amount or amount <= 0:
                    result = "Give me a specific positive revenue amount to log."
                else:
                    updated = await loop.run_in_executor(None, lambda: strategic_objective.log_revenue(amount, args.get("note", "")))
                    result = (
                        f"Logged ${amount:,.0f}. Cumulative revenue is now "
                        f"${updated['cumulative_revenue_usd']:,.0f} of the "
                        f"${updated['target_amount_usd']:,.0f} goal ({updated['progress_pct']}%)."
                    )
            else:
                s = await loop.run_in_executor(None, strategic_objective.get_objective_status)
                result = (
                    f"${s['cumulative_revenue_usd']:,.0f} of the ${s['target_amount_usd']:,.0f} goal "
                    f"({s['progress_pct']}%). Stretch deadline {s['stretch_deadline']}, "
                    f"committed deadline {s['committed_deadline']}."
                )

        elif name == "agent_orchestrator":
            action = (args.get("action") or "list").strip().lower()
            agent_id = args.get("agent_id", "")
            try:
                if action == "list":
                    agents = agent_orchestrator.list_agents()
                    result = "; ".join(
                        f"{a.name} ({a.status.value}, {a.permission_level.value})" for a in agents
                    ) or "No agents registered."
                elif action == "status":
                    agent = agent_orchestrator.get_agent(agent_id)
                    result = f"{agent.name}: {agent.status.value}" if agent else f"Unknown agent: {agent_id}"
                elif action == "start":
                    agent = await loop.run_in_executor(None, agent_orchestrator.start_agent, agent_id)
                    result = f"{agent.name} started."
                elif action == "stop":
                    agent = await loop.run_in_executor(None, agent_orchestrator.stop_agent, agent_id)
                    result = f"{agent.name} stopped."
                elif action == "assign":
                    task = await loop.run_in_executor(
                        None, lambda: agent_orchestrator.assign_task(agent_id, args.get("task", ""))
                    )
                    if task.status.value == "pending_approval":
                        result = (
                            f"Task queued for {agent_id} — it needs your approval before it runs "
                            f"(EXECUTE-level agent). Task id: {task.id}"
                        )
                    elif task.status.value == "done":
                        summary = task.result.get("summary") if isinstance(task.result, dict) else task.result
                        # "done" means the handler ran and honestly reported an
                        # outcome in task.result — NOT that the underlying
                        # action succeeded. An EXECUTE-level handler (e.g.
                        # buildpro_email_responder) can complete normally while
                        # reporting a real send/write failure inside that
                        # result dict. Don't say "Task completed" over a
                        # result that says otherwise.
                        succeeded = True
                        if isinstance(task.result, dict):
                            for key in ("sent", "ok", "success"):
                                if key in task.result:
                                    succeeded = bool(task.result[key])
                                    break
                        result = (
                            f"Task completed: {summary}" if succeeded
                            else f"Task did not succeed: {summary}"
                        )
                    elif task.status.value == "failed":
                        result = f"Task failed: {task.error}"
                    else:
                        result = f"Task queued: {task.id}"
                elif action == "approve":
                    task = await loop.run_in_executor(None, agent_orchestrator.approve_task, args.get("task_id", ""))
                    result = f"Approved. Status: {task.status.value}."
                elif action == "reject":
                    task = await loop.run_in_executor(None, agent_orchestrator.reject_task, args.get("task_id", ""))
                    result = "Task rejected."
                elif action == "results":
                    results = agent_orchestrator.get_results(agent_id)
                    result = str(results) if results else "No results yet."
                else:
                    result = f"Unknown agent_orchestrator action: {action}"
            except KeyError as e:
                result = str(e)
            except PermissionError as e:
                result = str(e)

        elif name == "communications":
            caction = (args.get("action") or "status").strip().lower()
            to = (args.get("to") or "").strip()
            if caction == "status":
                s = await loop.run_in_executor(None, twilio.get_status)
                result = f"Communications: {s['state']}. {s['detail']}"
            elif caction == "check_connection":
                s = await loop.run_in_executor(None, twilio.check_connection)
                result = f"Communications: {s['state']}. {s['detail']}"
            elif caction == "history":
                rows = await loop.run_in_executor(None, twilio.get_history)
                if not rows:
                    result = "No call or SMS history yet."
                else:
                    result = "; ".join(
                        f"{r['direction']} {r['kind']} {r.get('to_number') or r.get('from_number')} ({r['status']})"
                        for r in rows[:8]
                    )
            elif caction == "missed_calls":
                rows = await loop.run_in_executor(None, twilio.get_missed_calls)
                result = (
                    f"{len(rows)} missed call(s): " + "; ".join(r.get("from_number", "unknown") for r in rows)
                    if rows else "No missed calls."
                )
            elif caction == "lookup_contact":
                if not to:
                    result = "Give me a name or number to look up."
                else:
                    r = await loop.run_in_executor(None, twilio.lookup_contact, to)
                    result = f"Resolved to {r['number']}" if r["resolved"] else r["detail"]
            elif caction == "send_sms":
                body = (args.get("body") or "").strip()
                if not to or not body:
                    result = "I need both a recipient and message text to send an SMS."
                else:
                    r = await loop.run_in_executor(None, lambda: twilio.send_sms(to, body))
                    result = (
                        f"Text sent to {to}." if r["ok"]
                        else f"Couldn't send the text ({r['state']}): {r['detail']}"
                    )
                    audit_log.record(
                        "send_sms", execution_status="succeeded" if r["ok"] else "failed",
                        result=r, error=None if r["ok"] else r.get("detail"),
                        external_system="twilio", reference_id=r.get("sid"),
                    )
            elif caction == "call":
                if not to:
                    result = "Who should I call?"
                else:
                    message = args.get("message", "")
                    r = await loop.run_in_executor(None, lambda: twilio.place_call(to, message))
                    result = (
                        f"Calling {to} now." if r["ok"]
                        else f"Couldn't place the call ({r['state']}): {r['detail']}"
                    )
                    audit_log.record(
                        "place_call", execution_status="succeeded" if r["ok"] else "failed",
                        result=r, error=None if r["ok"] else r.get("detail"),
                        external_system="twilio", reference_id=r.get("sid"),
                    )
            else:
                result = f"Unknown communications action: {caction}"

        elif name == "gmail":
            gaction = (args.get("action") or "status").strip().lower()
            if gaction == "status":
                s = await loop.run_in_executor(None, google_auth.get_credential_status)
                if s.get("authorized"):
                    result = "Gmail is connected and authorized."
                elif s.get("credential_file") == "missing":
                    result = "Gmail isn't set up — no Google client-secret file found."
                else:
                    result = "Gmail credentials exist but aren't authorized yet — the one-time Google sign-in hasn't been completed."
            elif gaction == "list":
                query = (args.get("query") or "").strip()
                max_results = int(args.get("max_results") or 10)
                r = await loop.run_in_executor(None, lambda: gmail_integration.list_messages(query, max_results))
                if not r["ok"]:
                    result = f"Couldn't read Gmail ({r.get('state')}): {r.get('detail')}"
                elif not r["messages"]:
                    result = "No matching messages."
                else:
                    result = "; ".join(
                        f"{m.get('sender', 'unknown')} — {m.get('subject', '(no subject)')}"
                        for m in r["messages"][:8]
                    )
            elif gaction == "draft":
                to      = (args.get("to") or "").strip()
                subject = (args.get("subject") or "").strip()
                body    = (args.get("body") or "").strip()
                if not to or not body:
                    result = "I need a recipient and message body to draft an email."
                else:
                    r = await loop.run_in_executor(None, lambda: gmail_integration.create_draft(to, subject, body))
                    result = (
                        f"Draft saved to {to}." if r["ok"]
                        else f"Couldn't create the draft ({r.get('state')}): {r.get('detail')}"
                    )
            elif gaction == "send":
                to      = (args.get("to") or "").strip()
                subject = (args.get("subject") or "").strip()
                body    = (args.get("body") or "").strip()
                if not to or not body:
                    result = "I need both a recipient and message content to send an email."
                else:
                    r = await loop.run_in_executor(
                        None, lambda: gmail_integration.send_email(to, subject, body, approved=True)
                    )
                    result = (
                        f"Email sent to {to}." if r["ok"]
                        else f"Couldn't send the email ({r.get('state')}): {r.get('detail')}"
                    )
                    audit_log.record(
                        "gmail_send", execution_status="succeeded" if r["ok"] else "failed",
                        result={"to": to, "subject": subject}, error=None if r["ok"] else r.get("detail"),
                        external_system="gmail", reference_id=r.get("message_id"),
                    )
            else:
                result = f"Unknown gmail action: {gaction}"

        elif name == "calendar":
            calaction = (args.get("action") or "status").strip().lower()
            if calaction == "status":
                s = await loop.run_in_executor(None, google_auth.get_credential_status)
                if s.get("authorized"):
                    result = "Calendar is connected and authorized."
                elif s.get("credential_file") == "missing":
                    result = "Calendar isn't set up — no Google client-secret file found."
                else:
                    result = "Calendar credentials exist but aren't authorized yet — the one-time Google sign-in hasn't been completed."
            elif calaction == "list":
                max_results = int(args.get("max_results") or 10)
                r = await loop.run_in_executor(None, lambda: calendar_integration.list_upcoming_events(max_results))
                if not r["ok"]:
                    result = f"Couldn't read the calendar ({r.get('state')}): {r.get('detail')}"
                elif not r["events"]:
                    result = "No upcoming events."
                else:
                    result = "; ".join(
                        f"{e.get('summary', '(no title)')} at {e.get('start')}"
                        for e in r["events"][:8]
                    )
            elif calaction == "create":
                summary   = (args.get("summary") or "").strip()
                start_iso = (args.get("start_iso") or "").strip()
                end_iso   = (args.get("end_iso") or "").strip()
                if not summary or not start_iso or not end_iso:
                    result = "I need a title, start time, and end time to create an event."
                else:
                    ignore_conflicts = bool(args.get("ignore_conflicts") or False)
                    r = await loop.run_in_executor(None, lambda: calendar_integration.create_event(
                        summary, start_iso, end_iso,
                        description=args.get("description", "") or "",
                        location=args.get("location", "") or "",
                        approved=True, ignore_conflicts=ignore_conflicts,
                    ))
                    if r["ok"]:
                        result = f"Event '{summary}' created."
                    elif r.get("state") == "CONFLICT":
                        conflict_names = ", ".join(c.get("summary") or "an event" for c in r.get("conflicts", []))
                        result = f"That time conflicts with: {conflict_names}. Ask if they want it scheduled anyway."
                    else:
                        result = f"Couldn't create the event ({r.get('state')}): {r.get('detail')}"
                    audit_log.record(
                        "calendar_create", execution_status="succeeded" if r["ok"] else "failed",
                        result={"summary": summary, "start_iso": start_iso}, error=None if r["ok"] else r.get("detail"),
                        external_system="google_calendar", reference_id=r.get("event_id"),
                    )
            elif calaction == "update":
                event_id = (args.get("event_id") or "").strip()
                if not event_id:
                    result = "I need the event id to update."
                else:
                    fields = {}
                    for key in ("summary", "description", "location", "start_iso", "end_iso"):
                        if args.get(key):
                            fields[key] = args[key]
                    if not fields:
                        result = "I need at least one thing to change."
                    else:
                        r = await loop.run_in_executor(
                            None, lambda: calendar_integration.update_event(event_id, approved=True, **fields)
                        )
                        result = (
                            "Event updated." if r["ok"]
                            else f"Couldn't update the event ({r.get('state')}): {r.get('detail')}"
                        )
                        audit_log.record(
                            "calendar_update", execution_status="succeeded" if r["ok"] else "failed",
                            result={"event_id": event_id, "fields": list(fields.keys())},
                            error=None if r["ok"] else r.get("detail"),
                            external_system="google_calendar", reference_id=event_id,
                        )
            else:
                result = f"Unknown calendar action: {calaction}"

        elif name == "airtable":
            aaction    = (args.get("action") or "status").strip().lower()
            base_id    = (args.get("base_id") or "").strip()
            table_name = (args.get("table_name") or "").strip()
            if aaction == "status":
                s = airtable_integration.get_status()
                result = "Airtable is connected." if s["configured"] else "Airtable isn't configured — no token set."
            elif aaction == "list":
                if not base_id or not table_name:
                    result = "I need a base id and table name to read from Airtable."
                else:
                    max_records = int(args.get("max_records") or 25)
                    r = await loop.run_in_executor(None, lambda: airtable_integration.list_records(
                        base_id, table_name, max_records,
                        filter_by_formula=args.get("filter_by_formula", "") or "",
                    ))
                    if not r["ok"]:
                        result = f"Couldn't read Airtable ({r.get('state')}): {r.get('detail')}"
                    elif not r["records"]:
                        result = "No matching records."
                    else:
                        result = "; ".join(
                            f"{rec.get('id')}: {rec.get('fields')}" for rec in r["records"][:8]
                        )
            elif aaction in ("create", "update"):
                if not base_id or not table_name:
                    result = "I need a base id and table name for Airtable."
                else:
                    raw_fields = args.get("fields")
                    try:
                        fields = json.loads(raw_fields) if isinstance(raw_fields, str) and raw_fields else (
                            raw_fields if isinstance(raw_fields, dict) else None
                        )
                    except Exception:
                        fields = None
                    if not fields or not isinstance(fields, dict):
                        result = "I need the fields to set, matching the table's real column names."
                    elif aaction == "create":
                        r = await loop.run_in_executor(
                            None, lambda: airtable_integration.create_record(base_id, table_name, fields, approved=True)
                        )
                        result = (
                            f"Record created in {table_name}." if r["ok"]
                            else f"Couldn't create the record ({r.get('state')}): {r.get('detail')}"
                        )
                        audit_log.record(
                            "airtable_create", execution_status="succeeded" if r["ok"] else "failed",
                            result={"base_id": base_id, "table_name": table_name}, error=None if r["ok"] else r.get("detail"),
                            external_system="airtable", reference_id=r.get("record_id"),
                        )
                    else:
                        record_id = (args.get("record_id") or "").strip()
                        if not record_id:
                            result = "I need the record id to update."
                        else:
                            r = await loop.run_in_executor(
                                None, lambda: airtable_integration.update_record(base_id, table_name, record_id, fields, approved=True)
                            )
                            result = (
                                "Record updated." if r["ok"]
                                else f"Couldn't update the record ({r.get('state')}): {r.get('detail')}"
                            )
                            audit_log.record(
                                "airtable_update", execution_status="succeeded" if r["ok"] else "failed",
                                result={"base_id": base_id, "table_name": table_name}, error=None if r["ok"] else r.get("detail"),
                                external_system="airtable", reference_id=record_id,
                            )
            else:
                result = f"Unknown airtable action: {aaction}"

        elif name == "hubspot":
            haction = (args.get("action") or "status").strip().lower()
            if haction == "status":
                result = "HubSpot is connected." if hubspot_integration.is_configured() else "HubSpot isn't configured — no token set."
            elif haction in ("list_contacts", "list_companies"):
                limit = int(args.get("limit") or 20)
                fn = hubspot_integration.get_contacts if haction == "list_contacts" else hubspot_integration.get_companies
                r = await loop.run_in_executor(None, lambda: fn(limit=limit))
                if not r["ok"]:
                    result = f"Couldn't read HubSpot ({r.get('state')}): {r.get('detail')}"
                elif not r["results"]:
                    result = "No records found."
                else:
                    result = "; ".join(
                        f"{rec.get('id')}: {rec.get('properties')}" for rec in r["results"][:8]
                    )
            elif haction in ("search_contacts", "search_companies"):
                query = (args.get("query") or "").strip()
                if not query:
                    result = "I need a search term."
                else:
                    limit = int(args.get("limit") or 20)
                    fn = hubspot_integration.search_contacts if haction == "search_contacts" else hubspot_integration.search_companies
                    r = await loop.run_in_executor(None, lambda: fn(query, limit=limit))
                    if not r["ok"]:
                        result = f"Couldn't search HubSpot ({r.get('state')}): {r.get('detail')}"
                    elif not r["results"]:
                        result = "No matching records."
                    else:
                        result = "; ".join(
                            f"{rec.get('id')}: {rec.get('properties')}" for rec in r["results"][:8]
                        )
            elif haction in ("upsert_contact", "upsert_company"):
                raw_props = args.get("properties")
                try:
                    properties = json.loads(raw_props) if isinstance(raw_props, str) and raw_props else (
                        raw_props if isinstance(raw_props, dict) else {}
                    )
                except Exception:
                    properties = None
                if not properties or not isinstance(properties, dict):
                    result = "I need the properties to set, as field name/value pairs."
                elif haction == "upsert_contact":
                    email = (args.get("email") or "").strip()
                    if not email:
                        result = "I need the contact's email to add or update them."
                    else:
                        r = await loop.run_in_executor(
                            None, lambda: hubspot_integration.upsert_contact(email, properties, approved=True)
                        )
                        result = (
                            f"Contact {r.get('action')}." if r["ok"]
                            else f"Couldn't write the contact ({r.get('state')}): {r.get('detail')}"
                        )
                        audit_log.record(
                            "hubspot_upsert_contact", execution_status="succeeded" if r["ok"] else "failed",
                            result={"email": email, "action": r.get("action")}, error=None if r["ok"] else r.get("detail"),
                            external_system="hubspot", reference_id=r.get("id"),
                        )
                else:
                    company_name = (args.get("company_name") or "").strip()
                    if not company_name:
                        result = "I need the company's name to add or update it."
                    else:
                        r = await loop.run_in_executor(
                            None, lambda: hubspot_integration.upsert_company(company_name, properties, approved=True)
                        )
                        result = (
                            f"Company {r.get('action')}." if r["ok"]
                            else f"Couldn't write the company ({r.get('state')}): {r.get('detail')}"
                        )
                        audit_log.record(
                            "hubspot_upsert_company", execution_status="succeeded" if r["ok"] else "failed",
                            result={"company_name": company_name, "action": r.get("action")}, error=None if r["ok"] else r.get("detail"),
                            external_system="hubspot", reference_id=r.get("id"),
                        )
            else:
                result = f"Unknown hubspot action: {haction}"

        elif name == "social_post":
            saction = (args.get("action") or "status").strip().lower()
            if saction == "status":
                s = await loop.run_in_executor(None, buffer_integration.verify_buffer)
                result = f"Buffer: {s['status']}." if s["configured"] else "Buffer isn't configured — no token set."
            elif saction in ("preview", "publish"):
                text = (args.get("text") or "").strip()
                if not text:
                    result = "I need the post content."
                else:
                    post = {
                        "text": text,
                        "channel_id": args.get("channel_id") or None,
                        "service": args.get("service") or None,
                        "link_url": args.get("link_url") or None,
                        "image_url": args.get("image_url") or None,
                        "mode": args.get("mode") or "addToQueue",
                        "allow_duplicate": bool(args.get("allow_duplicate") or False),
                    }
                    approve_publish = (saction == "publish")
                    r = await loop.run_in_executor(
                        None, lambda: buffer_integration.publish_to_buffer(post, approved=approve_publish)
                    )
                    if r["published"]:
                        result = f"Posted. Buffer id {r.get('buffer_id')}."
                    elif r["status"] == "PREVIEW":
                        p = r["preview"]
                        result = (
                            f"Preview — would post to channel {p['channel_id']} "
                            f"({p['service'] or 'unspecified platform'}), mode {p['mode']}: \"{p['text']}\". "
                            "Ask the user to confirm, then call 'publish' to actually post it."
                        )
                    else:
                        result = f"Couldn't {saction} the post ({r['status']}): {r.get('detail', '')}"
                    if saction == "publish":   # preview is read-only, never audited as a consequential action
                        audit_log.record(
                            "buffer_publish", execution_status="succeeded" if r["published"] else "failed",
                            result={"text": text[:120]}, error=None if r["published"] else r.get("detail"),
                            external_system="buffer", reference_id=r.get("buffer_id"),
                        )
            else:
                result = f"Unknown social_post action: {saction}"

        elif name == "buildpro_matching":
            bmaction = (args.get("action") or "").strip().lower()
            if bmaction == "add_candidate":
                cand_name = (args.get("name") or "").strip()
                if not cand_name:
                    result = "I need the candidate's name."
                else:
                    fields = {}
                    for key in ("title", "specialty", "skills", "location"):
                        if args.get(key):
                            fields[key] = args[key]
                    if args.get("years_experience") is not None:
                        fields["years_experience"] = int(args["years_experience"])
                    cand_id, action_taken = await loop.run_in_executor(
                        None, lambda: buildpro_data.upsert_candidate(cand_name, email=args.get("email", "") or "", **fields)
                    )
                    result = f"Candidate {action_taken} (id {cand_id})."
            elif bmaction == "score":
                candidate_id = args.get("candidate_id")
                job_id = args.get("job_id")
                if candidate_id is None or job_id is None:
                    result = "I need both a candidate id and a job id to score a match."
                else:
                    candidate = await loop.run_in_executor(None, lambda: buildpro_data.get_candidate(int(candidate_id)))
                    job = await loop.run_in_executor(None, lambda: buildpro_data.get_job(int(job_id)))
                    if not candidate or not job:
                        result = "I couldn't find that candidate and/or job."
                    else:
                        outcome = buildpro_matching.score_match(candidate, job)
                        score_txt = "not enough shared data to score" if outcome["score"] is None else f"{outcome['score']}/100"
                        result = f"Match score: {score_txt}. This is a rule-based estimate, not an objective ranking — review before acting: {outcome['rationale']}"
            elif bmaction in ("match_job", "match_candidate"):
                min_score = args.get("min_score")
                min_score = float(min_score) if min_score is not None else None
                try:
                    if bmaction == "match_job":
                        job_id = args.get("job_id")
                        if job_id is None:
                            result = "I need a job id."
                        else:
                            results = await loop.run_in_executor(
                                None, lambda: buildpro_matching.generate_matches_for_job(int(job_id), min_score=min_score)
                            )
                            stored = [r for r in results if r["stored"]]
                            result = f"Scored {len(results)} candidate(s) against this job, {len(stored)} stored. These are rule-based estimates, not hiring decisions — review each rationale before acting."
                    else:
                        candidate_id = args.get("candidate_id")
                        if candidate_id is None:
                            result = "I need a candidate id."
                        else:
                            results = await loop.run_in_executor(
                                None, lambda: buildpro_matching.generate_matches_for_candidate(int(candidate_id), min_score=min_score)
                            )
                            stored = [r for r in results if r["stored"]]
                            result = f"Scored this candidate against {len(results)} open job(s), {len(stored)} stored. These are rule-based estimates, not hiring decisions — review each rationale before acting."
                except ValueError as exc:
                    result = str(exc)
            elif bmaction == "top_matches":
                candidate_id = args.get("candidate_id")
                job_id = args.get("job_id")
                limit = int(args.get("limit") or 10)
                matches = await loop.run_in_executor(
                    None, lambda: buildpro_data.list_matches(
                        candidate_id=int(candidate_id) if candidate_id is not None else None,
                        job_id=int(job_id) if job_id is not None else None,
                        limit=limit,
                    )
                )
                if not matches:
                    result = "No stored matches found."
                else:
                    result = "; ".join(
                        f"candidate {m['candidate_id']} / job {m['job_id']}: {m['match_score']}"
                        for m in matches[:limit]
                    )
            else:
                result = f"Unknown buildpro_matching action: {bmaction}"

        elif name == "proactive_settings":
            paction = (args.get("action") or "status").strip().lower()
            if paction == "status":
                enabled = await asyncio.to_thread(config_manager.get_proactive_enabled)
                quiet_hours = await asyncio.to_thread(config_manager.get_proactive_quiet_hours)
                parts = [f"Proactive check-ins are {'enabled' if enabled else 'disabled'}."]
                if ctx.proactive.is_snoozed():
                    mins = round(ctx.proactive.snoozed_remaining_secs() / 60)
                    parts.append(f"Currently snoozed for about {mins} more minute(s).")
                if quiet_hours:
                    parts.append(f"Quiet hours: {quiet_hours[0]}:00–{quiet_hours[1]}:00.")
                result = " ".join(parts)
            elif paction == "enable":
                await asyncio.to_thread(config_manager.save_proactive_enabled, True)
                result = "Proactive check-ins enabled."
            elif paction == "disable":
                await asyncio.to_thread(config_manager.save_proactive_enabled, False)
                result = "Proactive check-ins disabled."
            elif paction == "snooze":
                minutes = float(args.get("minutes") or 60)
                ctx.proactive.snooze(minutes * 60)
                result = f"Proactive check-ins snoozed for {int(minutes)} minute(s)."
            elif paction == "history":
                limit = int(args.get("limit") or 10)
                entries = await asyncio.to_thread(proactive_module.get_recent_triggers, limit)
                if not entries:
                    result = "No proactive check-ins recorded yet."
                else:
                    result = "; ".join(
                        f"{datetime.fromtimestamp(e['triggered_ts']).strftime('%b %d %I:%M %p')} ({e['focus_area']})"
                        for e in entries
                    )
            else:
                result = f"Unknown proactive_settings action: {paction}"

        elif name == "business_intelligence":
            baction = (args.get("action") or "list").strip().lower()
            business = args.get("business", "general")
            try:
                if baction == "log":
                    entry_id = await loop.run_in_executor(
                        None, lambda: biz_intel.add_entry(
                            args.get("category", "research"), business,
                            args.get("title", "Untitled"), args.get("content", ""),
                        )
                    )
                    result = f"Logged to {business} business intelligence (entry #{entry_id})."
                elif baction == "list":
                    entries = await loop.run_in_executor(
                        None, lambda: biz_intel.list_entries(args.get("category"), business, 10)
                    )
                    result = "; ".join(e["title"] for e in entries) or "Nothing logged yet."
                elif baction == "lessons":
                    lessons = await loop.run_in_executor(None, lambda: biz_intel.get_lessons_for(business))
                    result = "; ".join(l["content"] for l in lessons) or f"No lessons logged for {business} yet."
                elif baction == "record_outcome":
                    r = await loop.run_in_executor(
                        None, lambda: biz_intel.record_outcome(
                            business, args.get("plan", ""), args.get("result", ""),
                            float(args.get("revenue") or 0), float(args.get("cost") or 0),
                            args.get("lesson", ""), args.get("recommendation", ""),
                        )
                    )
                    result = f"Outcome recorded (#{r['outcome_id']})."
                elif baction == "summary":
                    s = await loop.run_in_executor(None, lambda: biz_intel.summary(business))
                    result = f"{s['total_entries']} entries logged for {business}; ${s['total_revenue_usd']:,.0f} tracked revenue."
                else:
                    result = f"Unknown business_intelligence action: {baction}"
            except ValueError as e:
                result = str(e)

        elif name == "opportunity_engine":
            oaction = (args.get("action") or "rank").strip().lower()
            business = args.get("business", "general")
            try:
                if oaction == "add":
                    score_fields = {
                        k: args[k] for k in (
                            "revenue_potential", "time_to_revenue", "probability", "cost",
                            "capital_required", "scalability", "automation_potential",
                            "competition", "risk", "opportunity_cost", "alignment",
                        ) if k in args
                    }
                    r = await loop.run_in_executor(
                        None, lambda: opp_engine.add_opportunity(
                            business, args.get("opp_type", "quick_cash"),
                            args.get("title", "Untitled opportunity"), args.get("description", ""),
                            **score_fields,
                        )
                    )
                    result = f"Opportunity #{r['id']} logged — score {r['score']}/100."
                elif oaction in ("rank", "list"):
                    fn = opp_engine.rank_opportunities if oaction == "rank" else opp_engine.list_opportunities
                    opps = await loop.run_in_executor(None, lambda: fn(args.get("opp_type"), business, 10))
                    result = "; ".join(f"{o['title']} ({o['score']}/100)" for o in opps) or "No opportunities logged yet."
                elif oaction == "update_status":
                    opp_id = args.get("opportunity_id")
                    if not opp_id:
                        result = "Which opportunity? Give me its id."
                    else:
                        r = await loop.run_in_executor(
                            None, lambda: opp_engine.update_status(int(opp_id), args.get("status", "active"))
                        )
                        result = f"Opportunity #{r['id']} marked {r['status']}."
                else:
                    result = f"Unknown opportunity_engine action: {oaction}"
            except ValueError as e:
                result = str(e)

        elif name == "ceo_decision":
            daction = (args.get("action") or "propose").strip().lower()
            business = args.get("business", "general")
            try:
                if daction == "propose":
                    r = await loop.run_in_executor(
                        None, lambda: decision_engine.propose_decision(
                            business, args.get("title", "Untitled decision"), args.get("analysis", ""),
                            args.get("alternatives", ""), args.get("recommendation", ""),
                            args.get("upside", ""), args.get("downside", ""),
                            bool(args.get("requires_authorization", True)),
                        )
                    )
                    obj = r["objective"]
                    result = (
                        f"Decision #{r['decision_id']} proposed. Currently ${obj['cumulative_revenue_usd']:,.0f} "
                        f"of ${obj['target_amount_usd']:,.0f} toward the objective ({obj['progress_pct']}%)."
                    )
                elif daction == "authorize":
                    decision_id = args.get("decision_id")
                    if not decision_id:
                        result = "Which decision should I authorize? Give me its id."
                    else:
                        r = await loop.run_in_executor(None, lambda: decision_engine.authorize_decision(int(decision_id)))
                        result = f"Decision #{r['decision_id']} authorized."
                elif daction == "record_outcome":
                    decision_id = args.get("decision_id")
                    if not decision_id:
                        result = "Which decision's outcome should I record? Give me its id."
                    else:
                        r = await loop.run_in_executor(
                            None, lambda: decision_engine.record_decision_outcome(
                                int(decision_id), args.get("result", ""),
                                float(args.get("revenue") or 0), float(args.get("cost") or 0),
                                args.get("lesson", ""), args.get("recommendation", ""),
                            )
                        )
                        result = f"Outcome recorded for decision #{decision_id} (#{r['outcome_id']})."
                else:
                    result = f"Unknown ceo_decision action: {daction}"
            except ValueError as e:
                result = str(e)

        elif name == "cloud_status":
            caction = (args.get("action") or "status").strip().lower()
            if caction == "status":
                r = await loop.run_in_executor(None, cloud_bridge.get_status)
                if "_bridge_error" in r:
                    result = f"Couldn't reach cloud JARVIS: {r['_bridge_error']}"
                else:
                    agents = (r.get("orchestrator") or {}).get("agents") or []
                    pending = (r.get("orchestrator") or {}).get("pending_approval_count", 0)
                    result = f"Cloud JARVIS is up — {len(agents)} agents registered, {pending} pending approval(s)."
            elif caction == "brief":
                r = await loop.run_in_executor(None, cloud_bridge.get_brief)
                result = r.get("_bridge_error") or json.dumps(r)[:800]
            elif caction == "activity":
                limit = int(args.get("limit") or 10)
                r = await loop.run_in_executor(None, lambda: cloud_bridge.get_activity(limit))
                if "_bridge_error" in r:
                    result = f"Couldn't reach cloud JARVIS: {r['_bridge_error']}"
                else:
                    items = r.get("activity") or []
                    result = "; ".join(a.get("message", a.get("kind", "")) for a in items) or "No recent cloud activity."
            elif caction == "run_agent":
                r = await loop.run_in_executor(
                    None, lambda: cloud_bridge.run_cloud_agent(args.get("agent_id", ""), args.get("description", ""))
                )
                if "_bridge_error" in r:
                    result = f"Couldn't run that on cloud JARVIS: {r['_bridge_error']}"
                else:
                    result = f"Cloud task {r.get('id', '?')} for {r.get('agent_id', '?')} is now {r.get('status', 'unknown')}."
            else:
                result = f"Unknown cloud_status action: {caction}"

        else:
            raise UnknownToolError(f"Unknown tool: {name}")

        return result
