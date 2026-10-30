# REST API v1

Base URL: `https://YOUR-PANEL/api/v1` (locally `http://localhost:8091/api/v1`). JSON in, JSON out.

## Authentication

Create a key under *Workspace > API keys & Claude* and send it on every request:

```
Authorization: Bearer oc_1a2b3c4d_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
(`X-API-Key: <key>` also works.) Scopes:

| Scope | Allows |
|---|---|
| `read` | every `GET` |
| `write` | `POST` / `PATCH` / `DELETE` on records, enrolling |
| `send` | sending email / WhatsApp, changing campaign status |

## Errors

```json
{"error": "invalid", "message": "jane@x.io already belongs to person 12."}
```
| Status | `error` | When |
|---|---|---|
| 400 | `invalid` / `bad_request` | Validation or business rule failed (duplicate email, unknown stage, opted out, outside WhatsApp window...) |
| 401 | `unauthorized` | Missing / invalid / expired / revoked key |
| 403 | `forbidden` | Key lacks the scope |
| 404 | `not_found` | Record or route doesn't exist |

Dates: ISO 8601, UTC (`2026-11-03T09:30`, `2026-11-03`). List endpoints take `limit` (max 200) and
`offset` and return `total`.

## Endpoints

### Meta
| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/me` | read | The key's name, prefix and scopes |
| GET | `/overview` | read | Counts, pipeline, tasks, unread, email stats, active campaigns, recent notes |
| GET | `/search?q=&limit=` | read | `{people, companies, deals}` |

### People
| Method | Path | Scope | Body / query |
|---|---|---|---|
| GET | `/people` | read | `q, lifecycle_stage, tag, company_id, campaign_id, owner_id, limit, offset` |
| POST | `/people` | write | `first_name, last_name, email, phone, job_title, company, company_website, lifecycle_stage, lead_status, source, owner_id, tags[], custom_fields{}` |
| GET | `/people/<id>?timeline=25` | read | Full profile incl. contact points, consents, enrollments, open tasks, deals, timeline |
| PATCH | `/people/<id>` | write | Any of the POST fields, plus `do_not_contact`, `company_id`, `tags[]` (replace), `add_tags[]`, `remove_tags[]`; returns `{person, changed[]}` |
| DELETE | `/people/<id>` | write | Deletes the person and their history |
| POST | `/people/<id>/merge` | write | `{"other_id": 34}` - folds 34 into `<id>` |
| POST | `/people/<id>/messages` | send | `channel: email|whatsapp, body, subject?, conversation_id?, template?, template_language?, template_params[]?` |

`lifecycle_stage`: subscriber, lead, mql, sql, opportunity, customer, evangelist, other.
`lead_status`: new, open, in_progress, contacted, qualified, unqualified, bad_timing.

### Companies
| Method | Path | Scope | Body / query |
|---|---|---|---|
| GET | `/companies` | read | `q, limit, offset` |
| POST | `/companies` | write | `name*, website, industry, size, city, country, phone, description, tags[], custom_fields{}` |
| GET | `/companies/<id>` | read | With people, deals, recent activities |
| PATCH | `/companies/<id>` | write | Any POST field (+ `domain`, `address`, `owner_id`) |

### Deals
| Method | Path | Scope | Body / query |
|---|---|---|---|
| GET | `/pipelines` | read | Pipelines with stages |
| GET | `/deals` | read | `status (open|won|lost), pipeline_id, person_id, company_id, q, limit, offset` + `pipeline_summary` |
| POST | `/deals` | write | `title*, amount, currency, person_id, company_id, stage (name) or stage_id, pipeline_id, expected_close_date, source, custom_fields{}` |
| PATCH | `/deals/<id>` | write | Same fields + `lost_reason`; moving to a won/lost stage closes the deal |

### Notes & tasks
| Method | Path | Scope | Body / query |
|---|---|---|---|
| POST | `/activities` | write | `kind (note|call|meeting|email|whatsapp), body, subject, person_id / company_id / deal_id, occurred_at` |
| GET | `/tasks` | read | `view (open|today|overdue|done), person_id, company_id, deal_id, limit` |
| POST | `/tasks` | write | `title*, due_at, priority (low|normal|high), notes, person_id, company_id, deal_id, assignee_id` |
| PATCH | `/tasks/<id>` | write | `status (open|done|cancelled), title, due_at, priority, notes, assignee_id` |

### Campaigns
| Method | Path | Scope | Body |
|---|---|---|---|
| GET | `/campaigns?archived=1` | read | With headline stats |
| GET | `/campaigns/<id>` | read | Status counts, engagement, per-stage funnel |
| POST | `/campaigns/import` | write | `{"name": "...", "csv": "<file text>", "mapping": {"Website": "company_website"}?, "dry_run": true?}` - same as the Import page: creates a **draft** campaign (or fills the one with that name), upserts people by email, enrolls them and loads per-person stage subject / body / date columns. `dry_run` only reports what would happen |
| POST | `/campaigns/<id>/enrollments` | write | `{"person_ids": [1,2], "emails": ["a@b.io"]}` |
| POST | `/campaigns/<id>/status` | send | `{"status": "active"|"paused"|"archived"}` |

### Conversations
| Method | Path | Scope | Body / query |
|---|---|---|---|
| GET | `/conversations` | read | `status (open|unread|pending|closed|all), channel, person_id, limit` |
| GET | `/conversations/<id>` | read | Messages (incl. campaign emails), `whatsapp_window_open` |
| POST | `/conversations/<id>/messages` | send | `body, subject?, template?, template_language?, template_params[]?` |

## Examples

```bash
KEY=oc_...; API=http://localhost:8091/api/v1

# create a lead from a website form
curl -s -X POST $API/people -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"first_name":"Jane","email":"jane@acmehotel.ie","company":"Acme Hotel","company_website":"acmehotel.ie",
       "source":"website form","tags":["inbound"],"custom_fields":{"rooms":40}}'

# add a note and a task
curl -s -X POST $API/activities -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"person_id":12,"kind":"note","body":"Asked for a demo next week"}'
curl -s -X POST $API/tasks -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"person_id":12,"title":"Book demo","due_at":"2026-11-03T09:30","priority":"high"}'

# open a deal and move it
curl -s -X POST $API/deals -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"title":"Acme - booking engine","amount":9500,"person_id":12,"stage":"Qualified"}'
curl -s -X PATCH $API/deals/7 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"stage":"Won"}'

# enroll in a campaign
curl -s -X POST $API/campaigns/3/enrollments -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"emails":["jane@acmehotel.ie"]}'
```

Every write is recorded in the audit log as `api: <key name>`.
