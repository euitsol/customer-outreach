# Guide for AI assistants working on this repo

Outreach CRM: Flask + SQLAlchemy 2 + Postgres 16 (SQLite in tests), server-rendered Jinja, five
containers (web, worker, reply_checker, mcp, db). People-centric CRM + cold-email sequences + shared
email/WhatsApp inbox + REST API + MCP server for Claude.

Read first, in this order: `docs/ARCHITECTURE.md` (code map, flows), `docs/DATABASE.md` (schema,
migrations), then the module you are changing. User-facing behaviour is described in
`docs/USER_GUIDE.md`; API in `docs/API.md`; MCP in `docs/MCP.md`; ops in `docs/OPERATIONS.md`.

## Commands

```bash
.venv/bin/python -m pytest -q                       # full suite (~15 s, SQLite)
TEST_DATABASE_URL=postgresql+psycopg2://u:p@host/EMPTY_DB .venv/bin/python -m pytest -q   # Postgres
docker compose up -d --build                        # run the stack (panel :8091, MCP :8092)
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build   # Mailtrap, no real email
docker compose exec web python -m app.manage status # counts + applied migrations
.venv/bin/python -m pyflakes app tests              # quick static check
```
The venv needs `pip install -r requirements.txt pytest`. `DATABASE_URL` must be set to import `app`
outside tests (any SQLite URL works for experiments).

## Map (where things live)

| Need | File |
|---|---|
| Tables / relationships | `app/models.py` |
| Schema change | new `app/migrations/v000N_name.py` + append to `MIGRATIONS` in `app/migrations/__init__.py` |
| People, identities, tags, consent, merge, timeline | `app/crm/people.py` |
| Companies / deals / tasks & notes / inbox & sending / custom fields | `app/crm/{companies,deals,activities,conversations,custom_fields}.py` |
| Shared use cases for REST + MCP | `app/crm/actions.py` (raise `ValueError` / `actions.NotFound` / `PermissionError`) |
| Campaign logic (enroll, schedule, queue, unsubscribe, suppression) | `app/services.py` |
| Sending one campaign email | `app/sender.py`, `app/mailer.py`, loop in `app/worker.py` |
| IMAP replies / bounces | `app/reply_checker.py` |
| WhatsApp API + webhook parsing | `app/channels/whatsapp.py`; processing in `app/crm/conversations.py` |
| Pages | `app/views/<area>.py` + `templates/<area>/` |
| REST API | `app/views/api.py`; MCP tools `app/mcp_server.py` |
| Merge fields | `app/merge.py` |
| In-app help (English + Bengali) | `app/help/<slug>.{en,bn}.md`, registry in `app/help/__init__.py`, `app/views/help.py` |

## Invariants - do not break

1. **Every DB change goes through a versioned migration.** Never edit `v0001_baseline.py` (frozen) or an
   already-released migration; add a new one using the idempotent helpers in `migrations/ops.py`. Keep
   `tests/test_migrations.py::test_migrated_schema_matches_fresh_schema` passing (model and migrated
   schema must match). Migration logs go to **stderr** (stdout is the MCP stdio channel).
2. **People are unique by normalised identity**: `contact_points (kind, value)` unique; `people.email`
   mirrors the primary email. Create people with `people_svc.upsert` / `add_contact_point`, never
   `Person(email=...)` directly in app code.
3. **Enrollment = person in campaign**, unique `(campaign_id, person_id)`. Person data (names, company,
   phone, tags, notes) is NOT on `enrollments`; read it through `enrollment.person` (properties like
   `enrollment.first_name` exist for templates). Use `services.enroll()`.
4. **Consent and suppression are checked in services**, not in views: `services.sendable()`,
   `email_consent_ok()`, `not_suppressed()` in worker queries; `conversations.send` for 1:1 messages.
   Any new outbound path must reuse them.
5. **Audit every write** that a user/API/Claude can trigger: pass an `audit.Actor` (`views.common.actor()`
   in views, `apikeys.actor_for(key, ...)` in API/MCP).
6. **Times are naive UTC** (`app.clock.utcnow()`); convert only for display/input via `app/settings.py`.
7. **Services don't commit**; the view / API / MCP layer commits (exception: a failed send attempt is
   committed so it stays visible).
8. **Outward actions need the `send` scope** in API/MCP (sending messages, changing campaign status).
9. JSON columns: use `JSONDict` / `JSONList` from `app/db.py` (separate mutable type instances); assign
   a new dict when changing nested data.
10. Keep `/contacts...` redirects (old bookmarks) and the public paths `/t/`, `/webhooks/`, `/api/`
    exempt from session auth / CSRF in `web.guard`.

## Gotchas

* SQLite tests run with `PRAGMA foreign_keys=ON`; bulk `query.delete()` must delete children first
  (see `services.delete_enrollments`).
* Accessing an ORM object after another request deleted it raises `ObjectDeletedError`; tests capture
  ids first and open a new session.
* `with_entities()` on a query that has `selectinload` options fails: pass `load=False` to
  `people_query` / `enrollment_query`.
* Jinja has no list comprehensions; use filters (`|select|join`).
* Templates are cached in gunicorn: restart / HUP after template changes when testing a running server.
* WhatsApp free text only within 24h of the last inbound message (`conversations.whatsapp_window_open`).
* Running tests in a container with the repo bind-mounted leaves root-owned `__pycache__`; clean up.

## Style

Match the surrounding code: short docstrings stating intent, comments only for non-obvious reasons,
`snake_case`, user-facing messages in plain English (flash messages, `ValueError` texts surface in the
UI, API and MCP). Add or update tests with every behaviour change; update the relevant `docs/` page and the in-app help
(`app/help/`, both `en` and `bn`) of the module you touched.

## Using the CRM as an assistant (MCP)

If you are connected to the running CRM through MCP rather than editing code: follow the server's
instructions (search before creating, log what you did, never send messages or start campaigns unless
the user asked for that exact action). Tool reference: `docs/MCP.md`.
