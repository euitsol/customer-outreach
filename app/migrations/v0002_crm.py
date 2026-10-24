"""CRM restructure.

* New CRM core: users, companies, people, contact_points, consents, person/company tags (M2M),
  pipelines + stages, deals (+ deal_people M2M), activities, tasks, custom field definitions.
* Omnichannel: channel_accounts, conversations, conversation_messages, webhook_events.
* Platform: api_keys, audit_log.
* `contacts` (person data + campaign state mixed together) becomes `enrollments` (campaign state
  only, linked to a person). Each distinct email address becomes one person; company names become
  companies; phone numbers become E.164 contact points; per-contact notes become note activities;
  `custom` merge fields move to `enrollments.variables`; contact tags move to the person.
* Foreign keys get proper ON DELETE rules on Postgres; append-only tables get 64-bit ids.

Re-runnable: rows already linked to a person are skipped.
"""
import json
import sys
from datetime import datetime

from sqlalchemy import MetaData, Table, select, text

from app.clock import utcnow
from app.crm import normalize
from app.db import Base, JSONType
from app.migrations import ops

VERSION = 2
NAME = "crm_core"

NEW_TABLES = [
    "users", "api_keys", "audit_log", "custom_fields", "companies", "people", "contact_points", "consents",
    "person_tags", "company_tags", "pipelines", "pipeline_stages", "deals", "deal_people", "activities",
    "tasks", "channel_accounts", "conversations", "conversation_messages", "webhook_events",
]
OLD_INDEXES = ["ix_contacts_campaign_id", "ix_contacts_email", "ix_contacts_next_due_at", "ix_contacts_campaign_email",
               "ix_messages_contact_id", "ix_events_contact_id"]
DROPPED_COLUMNS = ["first_name", "last_name", "company", "phone", "notes", "custom", "queued", "subject",
                   "body1", "body2", "body2_date", "body3", "body3_date"]
WEBSITE_KEYS = ("website", "site", "url", "web", "homepage", "company_website")


def upgrade(conn):
    pg = ops.is_pg(conn)
    md = Base.metadata

    # 1. brand-new tables (dependency order)
    for t in md.sorted_tables:
        if t.name in NEW_TABLES:
            ops.create_model_table(conn, t)

    # 2. contacts -> enrollments, contact_id -> enrollment_id
    ops.rename_table(conn, "contacts", "enrollments")
    for table in ("messages", "events", "click_links"):
        ops.rename_column(conn, table, "contact_id", "enrollment_id")

    # 3. new columns on existing tables
    json_ddl = ops.type_ddl(conn, JSONType)
    ops.add_column(conn, "enrollments", "person_id", "INTEGER")
    ops.add_column(conn, "enrollments", "variables", json_ddl)
    ops.add_column(conn, "campaigns", "channel", "VARCHAR(20) DEFAULT 'email' NOT NULL")
    ops.add_column(conn, "campaigns", "owner_id", "INTEGER")
    ops.add_column(conn, "templates", "channel", "VARCHAR(20) DEFAULT 'email' NOT NULL")
    ops.add_column(conn, "templates", "meta", json_ddl)
    ops.add_column(conn, "tags", "color", "VARCHAR(20)")
    ops.add_column(conn, "tags", "created_at", "TIMESTAMP")

    # 4. people / companies out of the old contact rows
    conn.execute(text("UPDATE enrollments SET email = lower(trim(email))"))
    _backfill(conn)

    # 5. drop what moved elsewhere
    if ops.has_table(conn, "contact_tags"):
        conn.execute(text("DROP TABLE contact_tags"))
    for name in OLD_INDEXES:
        ops.drop_index(conn, name)
    for col in DROPPED_COLUMNS:
        ops.drop_column(conn, "enrollments", col)

    # 6. integrity: orphans out, indexes / constraints in
    for table in ("messages", "events", "click_links"):
        conn.execute(text(f"DELETE FROM {table} WHERE enrollment_id NOT IN (SELECT id FROM enrollments)"))
    conn.execute(text("DELETE FROM messages WHERE step_id NOT IN (SELECT id FROM campaign_steps)"))
    for name in ("enrollments", "messages", "events", "click_links"):
        ops.create_model_indexes(conn, md.tables[name])
    dupes = conn.execute(text(
        "SELECT COUNT(*) FROM (SELECT campaign_id, person_id FROM enrollments GROUP BY campaign_id, person_id "
        "HAVING COUNT(*) > 1) d")).scalar()
    if dupes:
        print(f"migration: {dupes} person(s) enrolled twice in one campaign; unique index not created", file=sys.stderr, flush=True)

    if pg:
        _postgres_constraints(conn, dupes)
    elif not dupes:
        ops.create_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS uq_enrollments_campaign_person "
                               "ON enrollments (campaign_id, person_id)")


def _postgres_constraints(conn, dupes):
    ops.pg_rename_constraint(conn, "enrollments", "contacts_pkey", "enrollments_pkey")
    ops.pg_rename_sequence(conn, "contacts_id_seq", "enrollments_id_seq")
    ops.pg_rename_constraint(conn, "messages", "uq_message_contact_step", "uq_messages_enrollment_step")
    nulls = conn.execute(text("SELECT COUNT(*) FROM enrollments WHERE person_id IS NULL")).scalar()
    if not nulls:
        conn.execute(text("ALTER TABLE enrollments ALTER COLUMN person_id SET NOT NULL"))
    if not dupes and not ops.pg_constraint_exists(conn, "uq_enrollments_campaign_person"):
        conn.execute(text("ALTER TABLE enrollments ADD CONSTRAINT uq_enrollments_campaign_person "
                          "UNIQUE (campaign_id, person_id)"))
    for table, column, ref, rule in [
        ("enrollments", "campaign_id", "campaigns", "CASCADE"),
        ("enrollments", "person_id", "people", "CASCADE"),
        ("campaign_steps", "campaign_id", "campaigns", "CASCADE"),
        ("messages", "enrollment_id", "enrollments", "CASCADE"),
        ("messages", "step_id", "campaign_steps", "CASCADE"),
        ("events", "enrollment_id", "enrollments", "CASCADE"),
        ("click_links", "enrollment_id", "enrollments", "CASCADE"),
        ("campaigns", "owner_id", "users", "SET NULL"),
    ]:
        ops.pg_replace_fk(conn, table, column, ref, rule)
    for table in ("events", "click_links"):
        conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN id TYPE BIGINT"))
        ops.pg_try(conn, f"ALTER SEQUENCE {table}_id_seq AS BIGINT")
    conn.execute(text("UPDATE tags SET created_at = now() WHERE created_at IS NULL"))


# ---------------------------------------------------------------------- data

def _backfill(conn):
    md = Base.metadata
    people, companies = md.tables["people"], md.tables["companies"]
    points, consents, activities = md.tables["contact_points"], md.tables["consents"], md.tables["activities"]
    person_tags = md.tables["person_tags"]
    enr = Table("enrollments", MetaData(), autoload_with=conn)
    cols = set(enr.c.keys())
    now = utcnow()

    def col(row, name):
        return row[name] if name in cols else None

    rows = conn.execute(select(enr).where(enr.c.person_id.is_(None)).order_by(enr.c.id)).mappings().all()
    if not rows:
        return

    groups = {}
    for r in rows:
        key = normalize.email(r["email"]) or f"#{r['id']}"
        groups.setdefault(key, []).append(r)

    company_by_name = {(n or "").strip().lower(): i for i, n in conn.execute(select(companies.c.id, companies.c.name))}
    used_domains = {d for (d,) in conn.execute(select(companies.c.domain).where(companies.c.domain.isnot(None)))}
    phone_owner = {v: p for v, p in conn.execute(select(points.c.value, points.c.person_id)
                                                 .where(points.c.kind == "phone"))}

    def company_id_for(name, website, mail):
        key = (name or "").strip().lower()
        if not key:
            return None
        if key in company_by_name:
            return company_by_name[key]
        domain = normalize.domain_from_url(website) or normalize.company_domain_from_email(mail)
        if domain in used_domains:
            domain = None
        res = conn.execute(companies.insert().values(
            name=name.strip()[:300], domain=domain, website=(website or None) and website[:500],
            attributes={}, created_at=now, updated_at=now))
        cid = res.inserted_primary_key[0]
        company_by_name[key] = cid
        if domain:
            used_domains.add(domain)
        return cid

    person_for_enrollment = {}
    for key, rs in groups.items():
        merged, custom = {}, {}
        for r in rs:                                       # oldest first, so newer values win
            for f in ("first_name", "last_name", "company", "phone"):
                v = (col(r, f) or "").strip()
                if v:
                    merged[f] = v
            custom.update(_json_dict(col(r, "custom")))
        mail = normalize.email(rs[0]["email"])
        existing = conn.execute(select(people.c.id).where(people.c.email == mail)).scalar() if mail else None
        if existing:
            pid = existing
        else:
            website = next((str(custom[k]) for k in WEBSITE_KEYS if custom.get(k)), None)
            company_id = company_id_for(merged.get("company"), website, mail)
            ph = normalize.phone(merged.get("phone"))
            last_contacted = max([_dt(r["last_step_at"]) for r in rs if r["last_step_at"]] or [None])
            pid = conn.execute(people.insert().values(
                company_id=company_id, first_name=merged.get("first_name"), last_name=merged.get("last_name"),
                email=mail, phone=ph, lifecycle_stage="lead", source="import", attributes={},
                do_not_contact=False, last_contacted_at=last_contacted,
                created_at=min([_dt(r["created_at"]) for r in rs if r["created_at"]] or [now]),
                updated_at=now)).inserted_primary_key[0]
            if mail:
                bounced = any(r["bounced"] for r in rs)
                conn.execute(points.insert().values(person_id=pid, kind="email", value=mail, label="work",
                                                    is_primary=True, status="bounced" if bounced else "active",
                                                    created_at=now, updated_at=now))
            if ph and ph not in phone_owner:
                conn.execute(points.insert().values(person_id=pid, kind="phone", value=ph, is_primary=True,
                                                    status="active", created_at=now, updated_at=now))
                phone_owner[ph] = pid
            if any(r["unsubscribed"] for r in rs):
                conn.execute(consents.insert().values(person_id=pid, channel="email", status="revoked",
                                                      source="migrated: unsubscribed", recorded_at=now))
            seen_notes = set()
            for r in rs:
                note = (col(r, "notes") or "").strip()
                if note and note not in seen_notes:
                    seen_notes.add(note)
                    conn.execute(activities.insert().values(
                        kind="note", subject="Note", body=note, person_id=pid,
                        occurred_at=_dt(r["created_at"]) or now,
                        created_at=now))
        for r in rs:
            person_for_enrollment[r["id"]] = pid
            values = {"person_id": pid}
            if "custom" in cols:
                values["variables"] = _json_dict(col(r, "custom")) or None
            conn.execute(md.tables["enrollments"].update()
                         .where(md.tables["enrollments"].c.id == r["id"]).values(**values))

    if ops.has_table(conn, "contact_tags"):
        ct = Table("contact_tags", MetaData(), autoload_with=conn)
        have = set(conn.execute(select(person_tags.c.person_id, person_tags.c.tag_id)))
        for contact_id, tag_id in conn.execute(select(ct.c.contact_id, ct.c.tag_id)):
            pid = person_for_enrollment.get(contact_id)
            if pid and (pid, tag_id) not in have:
                conn.execute(person_tags.insert().values(person_id=pid, tag_id=tag_id, created_at=now))
                have.add((pid, tag_id))


def _dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _json_dict(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}
