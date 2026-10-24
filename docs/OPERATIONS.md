# Operations: install, configure, deploy, back up

## Services

| Container | Command | Job |
|---|---|---|
| `db` | Postgres 16 | All data (volume `pgdata`) |
| `web` | `gunicorn app.web:app` on `127.0.0.1:8091` | Panel, REST API (`/api/v1`), tracking (`/t/...`), WhatsApp webhooks (`/webhooks/whatsapp`) |
| `worker` | `python -m app.worker` | Sends campaign emails: due stages, retries, pacing, daily limit, send window |
| `reply_checker` | `python -m app.reply_checker` | Reads the IMAP inbox: replies (stop sequences, log to inbox) and bounces |
| `mcp` | `python -m app.mcp_server` on `127.0.0.1:8092` | MCP server for Claude |

Every Python container runs `init_db()` at start-up: schema migrations (serialised by a Postgres
advisory lock, so exactly one container migrates) and default rows.

## Environment variables (`.env`)

| Variable | Required | Meaning |
|---|---|---|
| `POSTGRES_PASSWORD`, `DATABASE_URL` | yes | Database (`postgresql+psycopg2://mailcampaign:PASS@db:5432/mailcampaign`) |
| `ADMIN_USER`, `ADMIN_PASSWORD` | yes | Built-in admin login (personal users are added in the panel) |
| `SECRET_KEY` | recommended | Session signing key (long random string) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | for email | Outgoing mail (587 STARTTLS or 465 SSL) |
| `SENDER_NAME`, `SENDER_EMAIL` | for email | From header |
| `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD` | for replies | Mailbox watched for replies and bounces |
| `TRACK_BASE_URL` | yes | Public HTTPS URL for tracking pixels, links, unsubscribe and webhooks |
| `PUBLIC_BASE_URL` | no | Panel URL shown in instructions (defaults to `TRACK_BASE_URL`) |
| `TIMEZONE` | no | Default timezone for sending windows and display (`Europe/Dublin`) |
| `MIN_DELAY_SECONDS`, `MAX_DELAY_SECONDS`, `MAX_PER_DAY`, `CHECK_INTERVAL_SECONDS` | no | Defaults only; edited live in *Sending settings* |
| `DEFAULT_PHONE_REGION` | no | Country for phone numbers without `+` (ISO code, default `IE`) |
| `DEFAULT_CURRENCY` | no | Deal currency (default `EUR`) |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | for WhatsApp | Meta Cloud API credentials |
| `WHATSAPP_APP_SECRET` | for WhatsApp | Verifies webhook signatures (unsigned webhooks are rejected) |
| `WHATSAPP_VERIFY_TOKEN` | for WhatsApp | Any string; also entered in Meta's webhook settings |
| `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_DISPLAY_NUMBER`, `WHATSAPP_API_VERSION` | no | Shown in the panel / API version (`v21.0`) |
| `MCP_HOST`, `MCP_PORT` | no | MCP bind address (`0.0.0.0:8092` inside the container) |
| `MCP_ALLOWED_HOSTS` | no | Comma separated Host values for DNS-rebinding protection |

## Install (fresh server)

```bash
git clone <repo> && cd customer-outreach
cp .env.example .env && nano .env          # fill in the table above
docker compose up -d --build
docker compose exec web python -m app.manage create-user you@example.com --name "You"
```

Point your reverse proxy at `127.0.0.1:8091` (panel + `TRACK_BASE_URL` domain) and send `/mcp` to
`127.0.0.1:8092` (example: `Caddyfile`).

## Local development / testing

```bash
docker compose up -d --build                                                   # real SMTP from .env
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build # Mailtrap sandbox, no IMAP
```
`.env.mailtrap` (git-ignored) holds the Mailtrap SMTP credentials and `TRACK_BASE_URL=http://localhost:8091`.

Tests: `python -m pytest` (SQLite) or `TEST_DATABASE_URL=<empty postgres db> python -m pytest`.

## Deploy

`./deploy.sh` (on the VPS):

1. `git reset --hard origin/main`
2. `docker compose build`
3. **dumps the database** to `backups/pre-deploy-<date>-<commit>.sql.gz` (keeps the last 10; aborts the
   deploy if the dump fails)
4. `docker compose up -d` - containers restart; the first one migrates the schema
5. waits for `/healthz`, prints applied migrations, checks the MCP server

## Migrations

```bash
docker compose exec web python -m app.manage status     # row counts + applied migrations
```
Migrations are automatic and versioned (`schema_migrations`). Details and how to write one:
[DATABASE.md](DATABASE.md#migrations).

Upgrading from the pre-CRM version (contacts table) happens on the first start of this version:
`contacts` becomes `enrollments`, every distinct email becomes a person, company names become
companies (with website domains), phone numbers become E.164, notes become timeline notes, tags move to
people, unsubscribes become email opt-outs, bounces mark the address. Sent history, custom texts and
follow-up dates are kept. It was rehearsed on a copy of a production-shaped database.

## Backup & restore

```bash
# backup now
docker compose exec -T db pg_dump -U mailcampaign mailcampaign | gzip > backups/manual-$(date +%F).sql.gz

# restore into a fresh database (stops writes first)
docker compose stop web worker reply_checker mcp
docker compose exec -T db dropdb -U mailcampaign mailcampaign
docker compose exec -T db createdb -U mailcampaign mailcampaign
gunzip -c backups/<file>.sql.gz | docker compose exec -T db psql -U mailcampaign mailcampaign
docker compose up -d
```
Restoring an older backup is fine: the containers migrate it forward on start.

## WhatsApp

1. In [Meta for Developers](https://developers.facebook.com/apps) create a **Business** app, add the
   **WhatsApp** product and a phone number.
2. Create a **system user** in Business Settings with a permanent token that has
   `whatsapp_business_messaging` (and `whatsapp_business_management` for templates).
3. In `.env`: `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` (App settings > Basic),
   `WHATSAPP_VERIFY_TOKEN` (make one up). Redeploy.
4. WhatsApp > Configuration > Webhook: callback `https://<TRACK_BASE_URL host>/webhooks/whatsapp`, the
   verify token from step 3; subscribe to **messages**.
5. *Workspace > Integrations*: send the `hello_world` test template to your phone; reply to it and the
   message appears in the Inbox.
6. Create message templates in WhatsApp Manager; register them in *Templates* (channel WhatsApp, the
   exact template name and language) to use them outside the 24-hour window.

Webhooks are verified with HMAC-SHA256 and stored in `webhook_events` (unique per message/status), so
Meta's retries never create duplicates. *Integrations > Recent webhook deliveries* shows failures.

## Monitoring & troubleshooting

* Dashboard *System* card: sending switch, worker heartbeat, SMTP, IMAP, WhatsApp.
* `docker compose logs -f worker` - each send: `sent [campaign / stage] email` or `FAILED ...`.
* `docker compose logs -f reply_checker` - replies and bounces detected.
* `curl 127.0.0.1:8091/healthz`, `curl 127.0.0.1:8092/healthz`.
* *Workspace > Audit log* - who changed what (users, API keys, Claude, imports).

| Problem | Check |
|---|---|
| Worker "SMTP problem, pausing 5 min" | SMTP credentials / provider limits; `Sending settings > SMTP test` |
| Nothing sends | sending paused? campaign active? send window? daily limit reached? |
| Replies not detected | IMAP settings; the reply must come from a known address or quote a sent Message-ID |
| Webhook 403 | `WHATSAPP_APP_SECRET` missing/wrong |
| Web won't start after deploy | `docker compose logs web` - a failed migration is rolled back and logged; restore the pre-deploy backup if needed |

## Scaling notes

The schema and indexes are designed to stay fast into the hundreds of thousands of people on one small
VPS (not load-tested yet; the tests cover correctness, not volume). When you outgrow it see
[DATABASE.md](DATABASE.md#scalability-notes): partition `events` / `audit_log`, run several workers
with `SKIP LOCKED`, add read replicas for reporting.
