"""MCP server: connect Claude to the CRM.

    python -m app.mcp_server            streamable HTTP on MCP_HOST:MCP_PORT, path /mcp
                                        (every request needs `Authorization: Bearer <api key>`)
    python -m app.mcp_server --stdio    stdio, for Claude Desktop / Claude Code on the same machine
                                        (key from OUTREACH_API_KEY; optional but recommended)

Create keys in the panel under Workspace > API keys & Claude. A key's scopes limit what Claude
may do: read (search / view), write (create & update records, enroll), send (email / WhatsApp
people, start or pause campaigns). Every change is written to the audit log as "mcp: <key name>".
"""
import argparse
import os
import sys

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import config
from app.crm import actions, apikeys, audit
from app.db import SessionLocal, init_db

INSTRUCTIONS = """\
You are connected to the user's CRM (people, companies, deals, tasks, notes, email campaigns and a
shared email/WhatsApp inbox).

Working rules:
- Look before you write: use `search` or `get_person` / `get_company` before creating records, so you
  don't create duplicates. People are unique by email; companies by domain.
- Prefer updating existing records over creating new ones, and log what you did with `log_activity`
  (kind "note") when you research or summarise something about a person, company or deal.
- `send_message` and `set_campaign_status` reach real people. Only call them when the user has asked
  for that specific message or action; show the user the final text first unless they said not to.
- WhatsApp only allows free text within 24 hours of the person's last message; otherwise use an
  approved template (`template` argument). `get_conversation` tells you whether the window is open.
- Dates/times are ISO 8601 in UTC (e.g. 2026-11-03T09:30).
"""

server = MCPServer(name="Outreach CRM", instructions=INSTRUCTIONS, version="1.0")

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
OUTWARD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


# ---------------------------------------------------------------------- identity & execution

def _bearer(ctx):
    headers = None
    try:
        headers = ctx.headers if ctx is not None else None
    except Exception:          # stdio: no request object
        headers = None
    if headers:
        value = headers.get("authorization") or headers.get("Authorization") or ""
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return (headers.get("x-api-key") or "").strip() or None
    return os.environ.get("OUTREACH_API_KEY") or None


def _identity(session, ctx):
    """(actor, scopes) for this call."""
    raw = _bearer(ctx)
    if raw:
        key = apikeys.verify(session, raw)
        if key is None:
            raise ToolError("The API key is invalid, expired or revoked. Create a new one in Workspace > API keys.")
        return apikeys.actor_for(key, "mcp"), list(key.scopes or [])
    if getattr(ctx, "headers", None):
        raise ToolError("Missing API key.")
    # stdio without a key: a trusted local process (it already has database access)
    scopes = [s.strip() for s in os.environ.get("OUTREACH_MCP_SCOPES", "read,write").split(",") if s.strip()]
    return audit.Actor("mcp", "local", "mcp: local (stdio)"), scopes


def _call(ctx, scope, fn, *args, **kwargs):
    session = SessionLocal()
    try:
        actor, scopes = _identity(session, ctx)
        if scope not in scopes:
            raise ToolError(f"This API key does not have the '{scope}' permission. Ask the user to create a key "
                            f"with '{scope}' in Workspace > API keys if they want Claude to do this.")
        result = fn(session, actor, *args, **kwargs) if scope != "read" else fn(session, *args, **kwargs)
        if scope != "read":
            session.commit()
        return result
    except ToolError:
        session.rollback()
        raise
    except (actions.NotFound, ValueError, PermissionError) as e:
        session.rollback()
        raise ToolError(str(e))
    finally:
        session.close()


async def run(ctx, scope, fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: _call(ctx, scope, fn, *args, **kwargs))


# ---------------------------------------------------------------------- overview & search

@server.tool(annotations=READ)
async def crm_overview(ctx: Context) -> dict:
    """Snapshot of the CRM: number of people/companies, open pipeline value, overdue and due-today tasks,
    unread inbox messages, campaign email performance and active campaigns. Good first call for a briefing."""
    return await run(ctx, "read", actions.overview)


@server.tool(annotations=READ)
async def search(query: str, ctx: Context, limit: int = 10) -> dict:
    """Search people (name, email, phone, job title, company), companies (name, domain) and deals (title)
    at once. Use this before creating anything to avoid duplicates."""
    return await run(ctx, "read", actions.search, query, limit)


# ---------------------------------------------------------------------- people

@server.tool(annotations=READ)
async def list_people(ctx: Context, query: str | None = None, lifecycle_stage: str | None = None,
                      tag: str | None = None, company_id: int | None = None, campaign_id: int | None = None,
                      limit: int = 25, offset: int = 0) -> dict:
    """List people, newest first. Filters: free-text query, lifecycle_stage (subscriber, lead, mql, sql,
    opportunity, customer, evangelist, other), tag, company_id, campaign_id (enrolled in). Paginate with
    limit/offset; `total` is the full match count."""
    return await run(ctx, "read", actions.list_people, query, lifecycle_stage, tag, company_id, campaign_id,
                     None, limit, offset)


@server.tool(annotations=READ)
async def get_person(ctx: Context, person_id: int | None = None, email: str | None = None,
                     timeline_limit: int = 25) -> dict:
    """Full profile of one person (by id or email): identities, company, lifecycle, custom fields, consent,
    campaign enrollments, open tasks, deals, and a timeline of notes, calls, messages and campaign email."""
    if person_id is None and not email:
        raise ToolError("Give person_id or email.")
    return await run(ctx, "read", actions.get_person, person_id, email, timeline_limit)


@server.tool(annotations=WRITE)
async def create_person(ctx: Context, first_name: str | None = None, last_name: str | None = None,
                        email: str | None = None, phone: str | None = None, job_title: str | None = None,
                        company: str | None = None, company_website: str | None = None,
                        lifecycle_stage: str | None = None, lead_status: str | None = None,
                        source: str | None = None, tags: list[str] | None = None,
                        custom_fields: dict | None = None) -> dict:
    """Create a person. Fails if the email already exists (use update_person then). `company` is matched by
    website domain or name and created if new. Phone numbers are normalised to international format."""
    data = {k: v for k, v in dict(first_name=first_name, last_name=last_name, email=email, phone=phone,
                                  job_title=job_title, company=company, company_website=company_website,
                                  lifecycle_stage=lifecycle_stage, lead_status=lead_status, source=source or "claude",
                                  tags=tags, custom_fields=custom_fields).items() if v is not None}
    return await run(ctx, "write", actions.create_person, data)


@server.tool(annotations=WRITE)
async def update_person(person_id: int, ctx: Context, first_name: str | None = None, last_name: str | None = None,
                        email: str | None = None, phone: str | None = None, job_title: str | None = None,
                        company: str | None = None, lifecycle_stage: str | None = None,
                        lead_status: str | None = None, owner_id: int | None = None,
                        do_not_contact: bool | None = None, add_tags: list[str] | None = None,
                        remove_tags: list[str] | None = None, custom_fields: dict | None = None) -> dict:
    """Change fields on a person; only the arguments you pass are changed. email/phone set the primary
    identity (old ones are kept as secondary). company="" detaches them from their company."""
    data = {k: v for k, v in dict(first_name=first_name, last_name=last_name, email=email, phone=phone,
                                  job_title=job_title, company=company, lifecycle_stage=lifecycle_stage,
                                  lead_status=lead_status, owner_id=owner_id, do_not_contact=do_not_contact,
                                  add_tags=add_tags, remove_tags=remove_tags,
                                  custom_fields=custom_fields).items() if v is not None}
    return await run(ctx, "write", actions.update_person, person_id, data)


@server.tool(annotations=DESTRUCTIVE)
async def merge_people(keep_person_id: int, duplicate_person_id: int, ctx: Context) -> dict:
    """Merge a duplicate into another person: identities, campaign enrollments, notes, tasks, conversations,
    deals and tags move to `keep_person_id`, then the duplicate is deleted. Cannot be undone."""
    return await run(ctx, "write", actions.merge_people, keep_person_id, duplicate_person_id)


# ---------------------------------------------------------------------- companies

@server.tool(annotations=READ)
async def list_companies(ctx: Context, query: str | None = None, limit: int = 25, offset: int = 0) -> dict:
    """List companies alphabetically, optionally filtered by name or domain."""
    return await run(ctx, "read", actions.list_companies, query, limit, offset)


@server.tool(annotations=READ)
async def get_company(company_id: int, ctx: Context) -> dict:
    """One company with its people, deals and recent activity."""
    return await run(ctx, "read", actions.get_company, company_id)


@server.tool(annotations=WRITE)
async def create_company(name: str, ctx: Context, website: str | None = None, industry: str | None = None,
                         size: str | None = None, city: str | None = None, country: str | None = None,
                         phone: str | None = None, description: str | None = None, tags: list[str] | None = None,
                         custom_fields: dict | None = None) -> dict:
    """Create a company (fails if one with the same name or website domain exists)."""
    data = {k: v for k, v in dict(name=name, website=website, industry=industry, size=size, city=city,
                                  country=country, phone=phone, description=description, tags=tags,
                                  custom_fields=custom_fields).items() if v is not None}
    return await run(ctx, "write", actions.create_company, data)


@server.tool(annotations=WRITE)
async def update_company(company_id: int, ctx: Context, name: str | None = None, website: str | None = None,
                         industry: str | None = None, size: str | None = None, city: str | None = None,
                         country: str | None = None, phone: str | None = None, description: str | None = None,
                         custom_fields: dict | None = None) -> dict:
    """Change fields on a company; only the arguments you pass are changed."""
    data = {k: v for k, v in dict(name=name, website=website, industry=industry, size=size, city=city,
                                  country=country, phone=phone, description=description,
                                  custom_fields=custom_fields).items() if v is not None}
    return await run(ctx, "write", actions.update_company, company_id, data)


# ---------------------------------------------------------------------- deals

@server.tool(annotations=READ)
async def list_pipelines(ctx: Context) -> list:
    """Deal pipelines with their stages (id, name, kind open/won/lost, win probability)."""
    return await run(ctx, "read", actions.list_pipelines)


@server.tool(annotations=READ)
async def list_deals(ctx: Context, status: str | None = None, pipeline_id: int | None = None,
                     person_id: int | None = None, company_id: int | None = None, query: str | None = None,
                     limit: int = 25, offset: int = 0) -> dict:
    """List deals (most recently updated first) with a summary of the default pipeline. status: open|won|lost."""
    return await run(ctx, "read", actions.list_deals, status, pipeline_id, person_id, company_id, query, limit,
                     offset)


@server.tool(annotations=WRITE)
async def create_deal(title: str, ctx: Context, amount: float | None = None, currency: str | None = None,
                      person_id: int | None = None, company_id: int | None = None, stage: str | None = None,
                      pipeline_id: int | None = None, expected_close_date: str | None = None,
                      source: str | None = None, custom_fields: dict | None = None) -> dict:
    """Create a deal. `stage` is a stage name of the pipeline (default: its first open stage). The person's
    company is used when company_id is omitted."""
    data = {k: v for k, v in dict(title=title, amount=amount, currency=currency, person_id=person_id,
                                  company_id=company_id, stage=stage, pipeline_id=pipeline_id,
                                  expected_close_date=expected_close_date, source=source,
                                  custom_fields=custom_fields).items() if v is not None}
    return await run(ctx, "write", actions.create_deal, data)


@server.tool(annotations=WRITE)
async def update_deal(deal_id: int, ctx: Context, stage: str | None = None, title: str | None = None,
                      amount: float | None = None, currency: str | None = None,
                      expected_close_date: str | None = None, lost_reason: str | None = None,
                      person_id: int | None = None, company_id: int | None = None,
                      custom_fields: dict | None = None) -> dict:
    """Update a deal. Moving it to a won/lost stage (e.g. stage="Won") closes it; pass lost_reason for losses."""
    data = {k: v for k, v in dict(stage=stage, title=title, amount=amount, currency=currency,
                                  expected_close_date=expected_close_date, lost_reason=lost_reason,
                                  person_id=person_id, company_id=company_id,
                                  custom_fields=custom_fields).items() if v is not None}
    return await run(ctx, "write", actions.update_deal, deal_id, data)


# ---------------------------------------------------------------------- notes & tasks

@server.tool(annotations=WRITE)
async def log_activity(body: str, ctx: Context, kind: str = "note", subject: str | None = None,
                       person_id: int | None = None, company_id: int | None = None, deal_id: int | None = None,
                       occurred_at: str | None = None) -> dict:
    """Add a note / call / meeting to the timeline of a person, company or deal (at least one id).
    kind: note | call | meeting | email | whatsapp. Use it to record research, summaries and outcomes."""
    return await run(ctx, "write", actions.log_activity, kind, body, subject, person_id, company_id, deal_id,
                     occurred_at)


@server.tool(annotations=READ)
async def list_tasks(ctx: Context, view: str = "open", person_id: int | None = None, company_id: int | None = None,
                     deal_id: int | None = None, limit: int = 50) -> dict:
    """Tasks. view: open | today (due by end of today) | overdue | done."""
    return await run(ctx, "read", actions.list_tasks, view, person_id, company_id, deal_id, limit)


@server.tool(annotations=WRITE)
async def create_task(title: str, ctx: Context, due_at: str | None = None, priority: str = "normal",
                      notes: str | None = None, person_id: int | None = None, company_id: int | None = None,
                      deal_id: int | None = None, assignee_id: int | None = None) -> dict:
    """Create a follow-up task (priority low | normal | high), optionally linked to a person/company/deal."""
    return await run(ctx, "write", actions.create_task, title, due_at, priority, notes, person_id, company_id,
                     deal_id, assignee_id)


@server.tool(annotations=WRITE)
async def update_task(task_id: int, ctx: Context, status: str | None = None, title: str | None = None,
                      due_at: str | None = None, priority: str | None = None, notes: str | None = None) -> dict:
    """Complete (status="done"), cancel, re-open or edit a task."""
    data = {k: v for k, v in dict(status=status, title=title, due_at=due_at, priority=priority,
                                  notes=notes).items() if v is not None}
    return await run(ctx, "write", actions.update_task, task_id, data)


# ---------------------------------------------------------------------- campaigns

@server.tool(annotations=READ)
async def list_campaigns(ctx: Context, include_archived: bool = False) -> list:
    """Email campaigns with status and headline stats (enrolled, sent, open rate, replies, failures)."""
    return await run(ctx, "read", actions.list_campaigns, include_archived)


@server.tool(annotations=READ)
async def get_campaign(campaign_id: int, ctx: Context) -> dict:
    """One campaign: its sequence stages with per-stage funnel numbers and engagement."""
    return await run(ctx, "read", actions.get_campaign, campaign_id)


@server.tool(annotations=WRITE)
async def enroll_in_campaign(campaign_id: int, ctx: Context, person_ids: list[int] | None = None,
                             emails: list[str] | None = None) -> dict:
    """Add people (by id or email) to a campaign's email sequence. Nothing is sent until the campaign is
    active; then the worker emails them at the configured pace."""
    return await run(ctx, "write", actions.enroll_in_campaign, campaign_id, person_ids, emails)


@server.tool(annotations=OUTWARD)
async def set_campaign_status(campaign_id: int, status: str, ctx: Context) -> dict:
    """Start (status="active"), pause or archive a campaign. Starting it sends real emails to everyone
    enrolled - only do this when the user explicitly asks. Requires the 'send' permission."""
    return await run(ctx, "send", actions.set_campaign_state, campaign_id, status)


# ---------------------------------------------------------------------- inbox

@server.tool(annotations=READ)
async def list_conversations(ctx: Context, status: str = "open", channel: str | None = None,
                             person_id: int | None = None, limit: int = 25) -> dict:
    """Inbox threads. status: open | unread | pending | closed | all. channel: email | whatsapp."""
    return await run(ctx, "read", actions.list_conversations, status, channel, person_id, limit)


@server.tool(annotations=READ)
async def get_conversation(conversation_id: int, ctx: Context, limit: int = 50) -> dict:
    """Messages of one thread (oldest first), incl. campaign emails, and whether the WhatsApp 24h window is open."""
    return await run(ctx, "read", actions.get_conversation, conversation_id, limit)


@server.tool(annotations=OUTWARD)
async def send_message(person_id: int, channel: str, ctx: Context, body: str | None = None,
                       subject: str | None = None, conversation_id: int | None = None, template: str | None = None,
                       template_language: str = "en_US", template_params: list[str] | None = None) -> dict:
    """Send an email or WhatsApp message to a person right now and log it in the inbox. channel: email |
    whatsapp. Email replies continue the last thread when subject is omitted. For WhatsApp outside the 24h
    window pass an approved `template` name (+ template_params). Respects opt-outs and do-not-contact.
    This reaches a real person: only send what the user asked for. Requires the 'send' permission."""
    return await run(ctx, "send", actions.send_message, person_id, channel, body, subject, conversation_id,
                     template, template_language, template_params)


# ---------------------------------------------------------------------- prompts

@server.prompt()
def daily_briefing() -> str:
    """Morning briefing: what needs attention today."""
    return ("Give me my CRM briefing for today. Call crm_overview, list_tasks(view='overdue') and "
            "list_tasks(view='today'), and list_conversations(status='unread'). Summarise: overdue and due "
            "tasks, unread replies (who, what they want), the open pipeline, and campaign health. End with the "
            "3 most valuable next actions. Don't send or change anything.")


@server.prompt()
def account_research(company_or_person: str) -> str:
    """Research an account and record the findings."""
    return (f"Look up '{company_or_person}' with search / get_company / get_person. Summarise who they are, "
            "our history with them (timeline, deals, campaign engagement) and open tasks. Then log a concise "
            "note with log_activity summarising the account and suggest a next step; ask me before creating "
            "tasks or deals.")


# ---------------------------------------------------------------------- HTTP transport

class BearerAuth:
    """ASGI middleware: every request to the MCP endpoint must carry a valid API key."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-api-key", "").strip()

        def check():
            s = SessionLocal()
            try:
                return apikeys.verify(s, raw) is not None
            finally:
                s.close()

        if not raw or not await anyio.to_thread.run_sync(check):
            body = b'{"error":"unauthorized","message":"Send Authorization: Bearer <API key> (Workspace > API keys)."}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b'Bearer realm="outreach-crm"')]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


def http_app():
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    security = None
    if config.MCP_ALLOWED_HOSTS:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=config.MCP_ALLOWED_HOSTS,
                                             allowed_origins=[f"https://{h}" for h in config.MCP_ALLOWED_HOSTS])
    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                     transport_security=security, host=config.MCP_HOST)

    async def healthz(request):
        return JSONResponse({"status": "ok"})
    app.add_route("/healthz", healthz)
    return BearerAuth(app)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.mcp_server")
    p.add_argument("--stdio", action="store_true", help="speak MCP over stdin/stdout")
    p.add_argument("--host", default=config.MCP_HOST)
    p.add_argument("--port", type=int, default=config.MCP_PORT)
    args = p.parse_args(argv)
    init_db()
    if args.stdio:
        server.run("stdio")
        return 0
    import uvicorn
    print(f"MCP server on http://{args.host}:{args.port}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(http_app(), host=args.host, port=args.port, log_level="info", proxy_headers=True,
                forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
