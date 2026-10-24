# Changelog

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
- **WhatsApp Business** (Meta Cloud API): send text / templates, signed webhooks, delivery / read
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
