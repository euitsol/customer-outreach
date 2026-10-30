# Architecture (for developers)

## Code map

```
app/
  config.py            env settings (defaults; runtime settings live in the DB)
  db.py                engine, Base (naming convention), JSON/BigInt types, init_db()
  models.py            every table (CRM core, engagement, outreach, platform)
  migrations/          versioned schema migrations (runner, ops helpers, v0001, v0002, ...)
  clock.py             utcnow() - naive UTC everywhere
  settings.py          runtime settings table (delays, limits, window, footer...) + timezone helpers

  crm/                 CRM domain services (no Flask here)
    normalize.py       email / phone (E.164) / domain canonical forms
    audit.py           Actor + audit_log writes and diffs
    people.py          upsert, identities (contact points), tags, consent, timeline, merge
    companies.py       get_or_create by domain / name, update, delete
    deals.py           pipelines, create / move / update, board, forecast
    activities.py      notes / calls / meetings, tasks
    conversations.py   inbox threads, inbound email & WhatsApp, outbound send (email / WhatsApp)
    custom_fields.py   definitions + typed coercion of values into `attributes`
    apikeys.py         create / verify / revoke hashed API keys
    actions.py         use cases shared by the REST API and the MCP server (validated, JSON out)
    serialize.py       record -> dict

  channels/            delivery adapters
    __init__.py        channel account lookup / status
    whatsapp.py        Meta Cloud API: send text / template, webhook verify / signature / parse
  mailer.py            SMTP: build MIME, tracking (pixel, links, unsubscribe), deliver
  services.py          outreach domain: enroll, scheduling, queue / retry / skip, suppression, unsubscribe
  sender.py            render + send one campaign stage, bounce handling, link into the inbox
  merge.py             {{merge_fields}} context (person, company, custom fields, campaign variables)
  queries.py           list filters and statistics for campaign pages
  importer.py          CSV/XLSX parsing, column mapping, campaign or CRM-only import, export
  worker.py            sending loop (pick manual queue, then due enrollments)
  reply_checker.py     IMAP loop (replies, bounces, inbox logging)
  mcp_server.py        MCP server (stdio / streamable HTTP + bearer auth)
  manage.py            CLI (status, reset, seed, create-user, create-api-key)
  help/                in-app documentation: <module>.{en,bn}.md pages + loader / endpoint -> page map
  web.py               Flask app, auth guard, CSRF, template filters, blueprints
  views/               one blueprint per area (people, companies, deals, tasks, inbox, enrollments,
                       campaigns, outbox, imports, admin, workspace, help, api, webhooks, tracking, dashboard)
templates/, static/    server-rendered pages; app.js is progressive enhancement only
tests/                 pytest suite (SQLite by default, Postgres via TEST_DATABASE_URL)
```

Layering: **views / API / MCP → crm.actions (API & MCP) → crm.* & services → models**. Views never
contain business rules that the API or MCP would also need; those live in services so every entry
point behaves the same (dedupe, consent, audit, WhatsApp window...).

## Key flows

**Campaign send** (`worker.run_cycle`): settings (paused? window? daily cap?) → `pick_manual` (queued
messages) else `pick_auto` (due enrollment in an active campaign that is sendable, not suppressed, email
consent OK, not blocked by retries) → `sender.send_stage` renders merge fields, adds tracking, delivers
over SMTP → on success: message `sent`, enrollment stage advances, `events(sent)`, conversation message
linked to the person's email thread, next due time recomputed → sleep a random delay.

**Reply / bounce** (`reply_checker.handle_message`): own mail ignored; bounces mark enrollments, contact
point and suppression; auto-replies ignored; replies match by `In-Reply-To/References` → sent
`messages.message_id`, else by sender address → enrollments `replied` + event; the email body (quotes
stripped) is logged in the person's inbox thread (idempotent on Message-ID).

**WhatsApp inbound** (`views/webhooks` → `conversations.process_whatsapp_webhook`): signature check →
each message/status stored in `webhook_events` (unique) → message: find person by phone (or create),
add WhatsApp contact point, append to thread, stop that person's sequences → status: update
sent / delivered / read / failed on the outbound message.

**One-to-one send** (`conversations.send`): consent / do-not-contact / suppression checks → email
(threaded to last inbound Message-ID) or WhatsApp (24h window or template) → recorded as a
conversation message (failed attempts are recorded too) → audit.

**Import**: parse → map → analyse (dry run) → apply: `people.upsert` (match by any known email) →
company by website domain / name → enroll (campaign mode) → per-stage overrides → recompute schedule.

## Conventions

* All writes that matter go through a service and record an `audit.Actor` (user / api / mcp / system).
* Identities are normalised before storage; uniqueness constraints do the de-duplication.
* Naive UTC datetimes in the DB; convert only for display / input (`settings.to_local`,
  `parse_local_input`).
* No business logic in templates; JS only enhances (forms work without it).
* Flash messages are written for end users; errors raised by services are user-presentable
  (`ValueError` messages surface in the UI, API and MCP).
* Commit in the view / API / MCP layer, not in services (exceptions: recording a failed send attempt).

## Adding things

* **A field on a record**: usually a *custom field* (no code). If it must be queried / constrained, add
  a column: model + `v000N` migration (`ops.add_column`) + forms / serializer.
* **A new channel (SMS, Telegram, ...)**: `app/channels/<name>.py` (send + webhook parse/verify), a
  branch in `conversations.send`, inbound recording like `record_whatsapp_inbound`, a webhook route,
  a `channel_accounts` row in `migrations.ensure_defaults`, Integrations card, tests.
* **A new MCP tool / API endpoint**: add the use case to `crm/actions.py`, then a thin wrapper in
  `mcp_server.py` and/or `views/api.py`; test in `tests/test_mcp.py` / `tests/test_crm.py`.
* **A schema change**: see [DATABASE.md](DATABASE.md#migrations); keep
  `test_migrated_schema_matches_fresh_schema` green.

* **A new page / module**: add `app/help/<slug>.en.md` and `<slug>.bn.md`, register the slug in
  `MODULES` in `app/help/__init__.py` (menu group, main endpoint, endpoints it documents). The Help
  button in `base.html` then appears on its pages. `tests/test_help.py` fails if a page has no help or a
  language is missing. Pages are `# Title`, a one-line summary paragraph, then `##` sections; link to
  other guides as `/help/<slug>`; indent nested lists by 4 spaces.

## Testing

```bash
.venv/bin/python -m pytest                                   # SQLite
TEST_DATABASE_URL=postgresql+psycopg2://u:p@host/empty .venv/bin/python -m pytest   # Postgres
```
`tests/test_app.py` (campaign end-to-end with mocked SMTP), `test_crm.py` (CRM UI flows, WhatsApp
webhooks, REST API, users, custom fields, CRM import), `test_mcp.py` (tools, permissions, HTTP auth),
`test_migrations.py` (old schemas upgrade; migrated == fresh), `test_manage.py` (CLI),
`test_help.py` (every module documented in English and Bengali, Help links on every page).
