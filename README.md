# Customer Outreach

Cold-email campaign manager: multi-stage sequences, per-contact personalised emails, paced sending,
automatic follow-ups, reply/bounce detection and open/click tracking. Flask + Postgres, three containers
(`web`, `worker`, `reply_checker`).

## Panel pages

| Page | What it does |
|---|---|
| **Dashboard** | Sends today, open/reply rates, 14-day chart, worker/SMTP/IMAP health, what's due next |
| **Campaigns** | Create, duplicate, start/pause, archive, delete. Per campaign: **Overview** (stage funnel with *Retry failed*, *Send now*, *Cancel queued* per stage), **Sequence** (design stages), **Contacts**, **Outbox**, **Settings** (start time, pacing overrides, sender, tracking) |
| **Sequence editor** | Any number of stages; each has subject, body, wait time (days + hours) after the previous stage, merge fields (`{{first_name\|there}}`, `{{company}}`, any imported column), live preview with a real contact, test-send, load from a saved template, reorder |
| **Contacts** | Server-side filters (campaign, status, stage, engagement, tag, priority, customised text, dates, free-text), sorting, paging, "select all matching", bulk actions (send next now, retry, tag, move, reschedule, pause, restart, suppress, export, delete) |
| **Contact page** | Edit profile + merge fields; per stage: custom subject/body/send date for that person, send now / retry / resend / skip, rendered preview, sent copy, timeline |
| **Import** | CSV or Excel, auto column mapping (also understands the old "Email 1 / Follow-up 1" sheets), preview + error report before saving, create/update/update-only modes. Columns like `stage2_body`, `stage2_subject`, `stage2_date` give each person their own email per stage; other columns become merge fields |
| **Outbox** | Scheduled / queued / failed, filter by stage, bulk retry or skip |
| **Activity log** | Every sent / failed / opened / clicked / replied / bounced / unsubscribed event, filterable, CSV export |
| **Templates, Tags, Do not contact** | Reusable emails, tag management (rename/merge), global suppression list (emails or whole domains) |
| **Settings** | Pause everything, delay range between emails, daily limit, send window + weekdays + timezone, retry attempts/backoff, signature, unsubscribe footer, tracking switches, SMTP/IMAP tests |

Sending rules: replies stop the sequence (auto-replies are ignored), unsubscribes and hard bounces
suppress the address across all campaigns, failed emails retry with backoff and then wait for a manual retry.
Unsubscribe uses a confirmation page plus the one-click `List-Unsubscribe` header.

## Configuration

Copy `.env.example` to `.env`. New variables: `ADMIN_USER` / `ADMIN_PASSWORD` (panel login; if the password is
empty the panel is open), optional `SECRET_KEY`, `TIMEZONE`. Pacing values in `.env` are only defaults, they
are edited live in **Settings**.

## Run locally (Docker / WSL)

```bash
cp .env.example .env      # edit passwords, SMTP, TRACK_BASE_URL=http://localhost:8091
docker compose up -d --build
# panel: http://127.0.0.1:8091
docker compose logs -f worker
```

## Deploy (VPS)

`./deploy.sh` pulls `origin/main`, rebuilds and restarts. The web container listens on `127.0.0.1:8091`
(proxy your vhost to it). The first start upgrades an existing database in place: it adds the new
columns/tables and converts each old contact's `body1/body2/body3` into campaign stages plus per-contact
custom text, keeping sent history and follow-up dates. Take a backup first:

```bash
docker compose exec db pg_dump -U mailcampaign mailcampaign > backup.sql
```

## Reset and re-seed data (CLI)

```bash
docker compose exec web python -m app.manage status
docker compose exec web python -m app.manage reset            # dry run: shows what would be deleted
docker compose exec web python -m app.manage reset --yes      # wipes campaigns, contacts, history, tags
docker compose exec web python -m app.manage seed seeds/ireland_hospitality_resend.csv     --campaign "Ireland Hospitality - Resend (tracked)" --replace
```

`seed` leaves the campaign as a **draft** (add `--start` to start it). Any CSV/XLSX the Import page accepts
works here, including the original research sheet. `seeds/ireland_hospitality_resend.csv` is generated from
that sheet by `seeds/build_ireland_resend.py` (cleaned text, tracked re-send subjects).

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests
```
