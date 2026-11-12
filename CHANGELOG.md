# Changelog

## Unreleased

### Changed
- **WhatsApp now runs on Twilio** instead of the Meta Cloud API: `TWILIO_*` settings replace `WHATSAPP_*`,
  webhooks are `/webhooks/twilio/whatsapp` (incoming) and `/webhooks/twilio/status` (delivery), templates are
  registered by Twilio Content SID (`HX...`), and a STOP reply opts the person out of WhatsApp.

### Added
- **Daily phone-number check**: the WhatsApp worker checks up to 50 new numbers a day with Twilio Lookup v2,
  numbers in WhatsApp campaigns first. The limit and an optional paid line-type check (about $0.008 per number)
  are set under Settings > WhatsApp number check (`manage check-phones` runs it now). People get a status tag:
  `whatsapp-pending`, `whatsapp-verified` (a message was delivered or they wrote), `whatsapp-invalid` or
  `no-whatsapp` (blocked). Filter by them in a campaign's enrollments. `manage backfill-whatsapp-status`
  tags existing numbers from past delivered / failed WhatsApp messages.
- **Undeliverable WhatsApp numbers are blocked automatically**: when Twilio reports that a number can't receive
  WhatsApp (error 63024 / 63003 / 21211 / 21614) the number is added to the do-not-contact list, its campaign
  enrollments are marked bounced and the person is tagged `whatsapp-invalid`; one-to-one sends and new WhatsApp
  campaign enrollments for it are refused. Email is not affected.
  Hard-bounced emails now also get the tag `email-bounced`. Run
  `docker compose exec web python -m app.manage flag-invalid-whatsapp` once to flag failures that already happened.
- **Activity shows every sent message**: one-to-one emails and WhatsApp messages (Inbox replies, person page, API,
  Claude) now appear next to campaign events, with a *Source* filter and in the CSV export. The Outbox *Scheduled*
  tab shows today's sends vs the daily and per-mailbox limits and why anything is waiting.
- **Create campaigns from the API and MCP**: `POST /api/v1/campaigns` and the `create_campaign` MCP tool create a
  draft email or WhatsApp campaign with its stages; starting a WhatsApp campaign through the API/MCP now also checks
  that every stage has a Content SID.
- **WhatsApp cooldown between campaigns**: a person who got a message from one WhatsApp campaign waits
  `TWILIO_COOLDOWN_DAYS` (default 7) before another WhatsApp campaign messages them, so one thread isn't flooded.
- **WhatsApp in the Outbox and Activity pages**: channel filter and badge, WhatsApp rows in *Scheduled* use the
  WhatsApp consent / phone block-list rules, new *Delivered* event, read receipts shown as *Opened*, messages Twilio
  could not deliver land in *Outbox > Failed* with the reason, and the message page shows template, delivery and read times.
- **WhatsApp campaigns**: choose *WhatsApp* when creating a campaign. Each stage sends an approved Twilio
  template (Content SID + values with merge fields) to people's phone numbers, with the usual delays,
  follow-ups and stop-on-reply. Messages appear in the person's WhatsApp thread; read receipts count as opens.
  A separate worker loop paces sends and caps them at `TWILIO_DAILY_LIMIT` (default 250) per day.
  Migration `0004` adds `campaign_steps.wa_template`.
- **In-app help** in English and Bengali (বাংলা): a guide for every module under *Help & guides*, and a
  **Help** button on every page (English / বাংলা) that opens the guide for the page you are on.
  Pages are Markdown files in `app/help/` rendered with the new `Markdown` dependency.
- **Mailbox per email**: one campaign email can name its own sending mailbox (*Send from* on the
  person's stage, or an `Email 1 mailbox` / `Follow-up 1 mailbox` import column, also via the API import).
  It wins over the campaign's rotation, waits for the next day when that mailbox is at its daily limit,
  and falls back to the rotation when the mailbox is disabled. The Outbox and email pages show the mailbox.
  No database change.
- REST API `POST /campaigns/import`: the Import page as an API call (draft campaign, people, per-person
  stage text and dates from CSV text; `dry_run` to preview).

## 2.0.0 - CRM (2026-10-25)

The campaign manager became a full CRM. Upgrading is automatic on first start (see "Upgrade" below).

### Added
- **People** with multiple emails / phones / WhatsApp numbers, lifecycle stage, lead status, owner,
  source, tags, custom fields, consent per channel, do-not-contact, timeline, duplicate merge.
- **Companies** (matched by website domain), **Deals** with pipelines, stages, probabilities, kanban
  board, weighted forecast, multiple people per deal with roles.
- **Tasks** and **activities** (notes, calls, meetings) on people, companies and deals.
- **Inbox**: email replies and WhatsApp chats per person, assignment, status, unread counts; campaign
  emails threaded in.
- **WhatsApp Business** (via Twilio): send text / templates, signed webhooks, delivery / read
  receipts, 24-hour window handling, idempotent processing.
- **Users** with roles; **API keys** with read / write / send scopes; **REST API** `/api/v1`.
- **MCP server** so Claude can use the CRM (26 tools, 2 prompts), with audit attribution.
- **Audit log** of every change; **custom field** definitions; **CRM-only import**.
- Versioned **schema migrations**, pre-deploy database backups, Postgres trigram / JSONB / partial
  indexes, BIGINT keys on append-only tables, proper foreign-key delete rules.
- Docs: user guide, operations, architecture, database, API, MCP, AI-assistant guide.

### Changed
- `contacts` → `enrollments` (campaign membership only); person data moved to people / companies.
  The **Contacts** menu is now **Enrollments**; old `/contacts` URLs redirect.
- A reply on any channel (email or WhatsApp) stops that person's sequences.
- Unsubscribes record an email opt-out on the person; bounces mark the address.
- Import maps "Website" to the company website, and "Job title" / "Company website" columns.

### Upgrade
`./deploy.sh` backs up the database, then the first container to start runs migrations
`0001_baseline_campaign_manager` and `0002_crm_core`. Each distinct email becomes one person; company
names become companies; phones become E.164; notes become timeline notes; tags move to people; sent
history, custom texts and follow-up dates are kept. Check with
`docker compose exec web python -m app.manage status`. Rollback: restore the `backups/pre-deploy-*`
dump and deploy the previous commit.

## 1.x
Multi-page campaign manager: sequences, per-contact emails, paced sending, reply / bounce detection,
open / click tracking, import / export.
