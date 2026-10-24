# Outreach CRM

A self-hosted CRM with cold-email sequences, a shared email + WhatsApp inbox, a sales pipeline and
an MCP server so Claude can work with your data. Flask + Postgres, five containers (`web`,
`worker`, `reply_checker`, `mcp`, `db`).

## What's in it

| Area | Pages |
|---|---|
| **CRM** | **People** (filters, bulk tag / lifecycle / owner / enroll / merge / export; profile with identities, consent, custom fields, timeline, tasks, deals, campaigns; send email or WhatsApp), **Companies** (people, deals, account notes), **Deals** (kanban board with drag & drop, list, weighted forecast, multi-person deals with roles) |
| **Work** | **Inbox** (every email reply and WhatsApp chat per person; reply, assign, close; WhatsApp 24-hour window and templates), **Tasks** (due today / overdue / mine), **Dashboard** |
| **Outreach** | **Campaigns** with multi-stage sequences, per-person email text, paced sending, follow-ups, open / click tracking; **Enrollments** (who is where in which sequence), **Outbox**, **Email activity**, **Import** (CSV / Excel into people + companies + a campaign) |
| **Workspace** | Users and roles, API keys, Integrations (SMTP/IMAP, WhatsApp, Claude), custom fields, pipelines, audit log, sending settings |

Rules that hold everywhere: a reply on **any** channel stops that person's sequences; unsubscribes and
hard bounces suppress the address across campaigns and are recorded as consent / identity status;
"do not contact" blocks every channel; every change made by users, the API or Claude lands in the
audit log.

Database design, ER diagram and migration workflow: [docs/DATABASE.md](docs/DATABASE.md).

## Configuration

Copy `.env.example` to `.env` and fill in Postgres, panel login, SMTP / IMAP, `TRACK_BASE_URL`.
Optional blocks: WhatsApp (`WHATSAPP_*`), `DEFAULT_PHONE_REGION` (country for local phone numbers),
`DEFAULT_CURRENCY`, `PUBLIC_BASE_URL`, MCP (`MCP_*`).

Sign-in: the `ADMIN_USER` / `ADMIN_PASSWORD` from `.env` always works; add personal users under
**Workspace > Users** (the first one becomes owner) or from the CLI:

```bash
docker compose exec web python -m app.manage create-user you@example.com --name "You"
```

## Run locally (Docker / WSL)

```bash
cp .env.example .env      # passwords, SMTP, TRACK_BASE_URL=http://localhost:8091
docker compose up -d --build
# panel: http://127.0.0.1:8091   MCP: http://127.0.0.1:8092/mcp
```

To test sending without emailing anyone, put Mailtrap sandbox credentials in `.env.mailtrap` and use
the override (it also blanks IMAP):

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

## Connect Claude (MCP)

1. **Workspace > API keys & Claude**: create a key. Scopes: `read` (search / view), `write` (create and
   update records, enroll in campaigns), `send` (email / WhatsApp people, start or pause campaigns).
2. Connect a client:

   * Claude Code, over HTTP:
     ```bash
     claude mcp add --transport http outreach-crm https://YOUR-PANEL/mcp --header "Authorization: Bearer YOUR_KEY"
     ```
     (locally `http://localhost:8092/mcp`; on the VPS proxy `/mcp` to `127.0.0.1:8092`, see `Caddyfile`)
   * Claude Desktop / Claude Code on the Docker host, over stdio:
     ```json
     {"mcpServers": {"outreach-crm": {"command": "docker", "args": ["compose", "-f",
       "/path/to/customer-outreach/docker-compose.yml", "exec", "-T", "-e", "OUTREACH_API_KEY=YOUR_KEY",
       "web", "python", "-m", "app.mcp_server", "--stdio"]}}}
     ```
   * claude.ai: add a custom connector pointing at `https://YOUR-PANEL/mcp` with the key as bearer token.

Tools: `crm_overview`, `search`, `list_people`, `get_person`, `create_person`, `update_person`,
`merge_people`, `list_companies`, `get_company`, `create_company`, `update_company`, `list_pipelines`,
`list_deals`, `create_deal`, `update_deal`, `log_activity`, `list_tasks`, `create_task`, `update_task`,
`list_campaigns`, `get_campaign`, `enroll_in_campaign`, `set_campaign_status`, `list_conversations`,
`get_conversation`, `send_message`; prompts `daily_briefing` and `account_research`. Changes appear in
the audit log as `mcp: <key name>`.

## WhatsApp

Meta Cloud API. Set `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` and a
`WHATSAPP_VERIFY_TOKEN` of your choice, redeploy, then register `https://<TRACK_BASE_URL>/webhooks/whatsapp`
in the Meta app (subscribe to `messages`). Step-by-step and a test-send button: **Workspace >
Integrations**. Inbound messages create / match the person by phone number and land in the inbox;
delivery and read receipts update the sent message. Outside the 24-hour customer-service window only
approved templates can be sent (register them under **Templates** with channel WhatsApp).

## REST API

`/api/v1`, `Authorization: Bearer <key>`, JSON in and out. Main routes: `GET /search`,
`GET|POST /people`, `GET|PATCH|DELETE /people/<id>`, `POST /people/<id>/merge`,
`POST /people/<id>/messages` (send scope), `GET|POST /companies`, `GET|PATCH /companies/<id>`,
`GET /pipelines`, `GET|POST /deals`, `PATCH /deals/<id>`, `POST /activities`, `GET|POST /tasks`,
`PATCH /tasks/<id>`, `GET /campaigns`, `GET /campaigns/<id>`, `POST /campaigns/<id>/enrollments`,
`POST /campaigns/<id>/status` (send scope), `GET /conversations`, `GET /conversations/<id>`,
`POST /conversations/<id>/messages` (send scope), `GET /overview`. Errors:
`{"error": "...", "message": "..."}` with 400 / 401 / 403 / 404.

## Deploy (VPS)

`./deploy.sh` pulls `origin/main`, builds, **dumps the database to `backups/`**, restarts, and waits for
`/healthz`. The web container listens on `127.0.0.1:8091`, MCP on `127.0.0.1:8092` (proxy your vhost to
them). The first start migrates the existing database in place (schema version table, then the CRM
restructure: every distinct email becomes a person, company names become companies, notes become
timeline notes, tags move to people, sent history and follow-up dates are kept). Check with:

```bash
docker compose exec web python -m app.manage status
```

## CLI

```bash
docker compose exec web python -m app.manage status
docker compose exec web python -m app.manage reset            # dry run: shows what would be deleted
docker compose exec web python -m app.manage reset --yes      # wipes CRM + campaign data
docker compose exec web python -m app.manage seed seeds/ireland_hospitality_resend.csv --campaign "Ireland Hospitality - Resend (tracked)" --replace
docker compose exec web python -m app.manage create-user you@example.com --role owner
docker compose exec web python -m app.manage create-api-key "Claude" --scopes read,write
```

`seed` leaves the campaign as a **draft** (add `--start` to start it). Any CSV/XLSX the Import page
accepts works, including the original research sheet.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
# against Postgres (empty database):
TEST_DATABASE_URL=postgresql+psycopg2://user:pass@host/emptydb .venv/bin/python -m pytest
```
