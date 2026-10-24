"""Baseline: bring any database created by an earlier version up to the campaign-manager schema
(the last schema before versioned migrations existed).

The table definitions below are a frozen copy of that schema. Do not edit them to match newer
models: later migrations start from exactly this shape.

Covers two kinds of old databases:
* the single-page version (contacts carrying body1/body2/body3 columns) - converted into campaign
  steps + per-contact messages, keeping sent history and follow-up dates (formerly app/legacy.py);
* the multi-page version created by `create_all` - nothing to do except fill in missing pieces.
"""
from datetime import datetime, time, timedelta

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, MetaData, String, Table, Text,
    UniqueConstraint, func, select, text,
)

from app.clock import utcnow
from app.migrations import ops

VERSION = 1
NAME = "baseline_campaign_manager"

md = MetaData()

campaigns = Table(
    "campaigns", md,
    Column("id", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("description", Text),
    Column("status", String(20), index=True),
    Column("is_active", Boolean),
    Column("start_at", DateTime),
    Column("daily_limit", Integer),
    Column("min_delay", Integer),
    Column("max_delay", Integer),
    Column("track_opens", Boolean),
    Column("track_clicks", Boolean),
    Column("sender_name", String(200)),
    Column("reply_to", String(300)),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime),
)
campaign_steps = Table(
    "campaign_steps", md,
    Column("id", Integer, primary_key=True),
    Column("campaign_id", Integer, ForeignKey("campaigns.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("name", String(200)),
    Column("subject", Text),
    Column("body", Text),
    Column("delay_hours", Integer),
    Column("created_at", DateTime, server_default=func.now()),
)
tags = Table(
    "tags", md,
    Column("id", Integer, primary_key=True),
    Column("name", String(100), unique=True, nullable=False),
)
contacts = Table(
    "contacts", md,
    Column("id", Integer, primary_key=True),
    Column("campaign_id", Integer, ForeignKey("campaigns.id"), nullable=False, index=True),
    Column("company", String(300)),
    Column("first_name", String(150)),
    Column("last_name", String(150)),
    Column("email", String(300), nullable=False, index=True),
    Column("phone", String(100)),
    Column("priority", String(50)),
    Column("notes", Text),
    Column("custom", Text),
    Column("send_flag", Boolean),
    Column("needs_verification", Boolean),
    Column("unsubscribed", Boolean),
    Column("replied", Boolean),
    Column("bounced", Boolean),
    Column("stage", Integer),
    Column("message_id", String(300)),
    Column("next_due_at", DateTime, index=True),
    Column("last_step_at", DateTime),
    Column("queued", Boolean),
    Column("subject", Text),
    Column("body1", Text),
    Column("body2", Text),
    Column("body2_date", Date),
    Column("body3", Text),
    Column("body3_date", Date),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime),
    Index("ix_contacts_campaign_email", "campaign_id", "email"),
)
contact_tags = Table(
    "contact_tags", md,
    Column("contact_id", Integer, ForeignKey("contacts.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)
messages = Table(
    "messages", md,
    Column("id", Integer, primary_key=True),
    Column("contact_id", Integer, ForeignKey("contacts.id"), nullable=False, index=True),
    Column("step_id", Integer, ForeignKey("campaign_steps.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("subject", Text),
    Column("body", Text),
    Column("due_at", DateTime),
    Column("status", String(20), index=True),
    Column("queued", Boolean, index=True),
    Column("attempts", Integer),
    Column("last_error", Text),
    Column("next_attempt_at", DateTime),
    Column("sent_at", DateTime),
    Column("message_id", String(300)),
    Column("sent_subject", Text),
    Column("sent_body", Text),
    Column("open_token", String(64), index=True),
    Column("unsub_token", String(64), index=True),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
    UniqueConstraint("contact_id", "step_id", name="uq_message_contact_step"),
)
events = Table(
    "events", md,
    Column("id", Integer, primary_key=True),
    Column("contact_id", Integer, ForeignKey("contacts.id"), nullable=False, index=True),
    Column("stage", Integer),
    Column("type", String(30), index=True),
    Column("detail", Text),
    Column("created_at", DateTime, server_default=func.now(), index=True),
)
click_links = Table(
    "click_links", md,
    Column("id", Integer, primary_key=True),
    Column("token", String(64), unique=True, nullable=False),
    Column("contact_id", Integer, ForeignKey("contacts.id"), nullable=False),
    Column("stage", Integer),
    Column("target_url", Text, nullable=False),
)
suppressions = Table(
    "suppressions", md,
    Column("id", Integer, primary_key=True),
    Column("kind", String(10), nullable=False),
    Column("value", String(300), nullable=False, index=True),
    Column("reason", String(200)),
    Column("created_at", DateTime),
    UniqueConstraint("kind", "value", name="uq_suppression"),
)
templates = Table(
    "templates", md,
    Column("id", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("subject", Text),
    Column("body", Text),
    Column("created_at", DateTime),
    Column("updated_at", DateTime),
)
settings = Table(
    "settings", md,
    Column("key", String(100), primary_key=True),
    Column("value", Text),
)

# columns that very old databases may lack
NEW_COLUMNS = [
    ("campaigns", "description", "TEXT"),
    ("campaigns", "status", "VARCHAR(20)"),
    ("campaigns", "is_active", "BOOLEAN DEFAULT FALSE"),
    ("campaigns", "start_at", "TIMESTAMP"),
    ("campaigns", "daily_limit", "INTEGER"),
    ("campaigns", "min_delay", "INTEGER"),
    ("campaigns", "max_delay", "INTEGER"),
    ("campaigns", "track_opens", "BOOLEAN DEFAULT TRUE"),
    ("campaigns", "track_clicks", "BOOLEAN DEFAULT TRUE"),
    ("campaigns", "sender_name", "VARCHAR(200)"),
    ("campaigns", "reply_to", "VARCHAR(300)"),
    ("campaigns", "updated_at", "TIMESTAMP"),
    ("contacts", "phone", "VARCHAR(100)"),
    ("contacts", "notes", "TEXT"),
    ("contacts", "custom", "TEXT"),
    ("contacts", "queued", "BOOLEAN DEFAULT FALSE"),
    ("contacts", "bounced", "BOOLEAN DEFAULT FALSE"),
    ("contacts", "next_due_at", "TIMESTAMP"),
    ("contacts", "last_step_at", "TIMESTAMP"),
    ("contacts", "updated_at", "TIMESTAMP"),
    ("contacts", "subject", "TEXT"),
    ("contacts", "body1", "TEXT"),
    ("contacts", "body2", "TEXT"),
    ("contacts", "body2_date", "DATE"),
    ("contacts", "body3", "TEXT"),
    ("contacts", "body3_date", "DATE"),
]

BACKFILLS = [
    "UPDATE campaigns SET status = CASE WHEN is_active THEN 'active' ELSE 'paused' END WHERE status IS NULL",
    "UPDATE campaigns SET track_opens = TRUE WHERE track_opens IS NULL",
    "UPDATE campaigns SET track_clicks = TRUE WHERE track_clicks IS NULL",
    "UPDATE contacts SET bounced = FALSE WHERE bounced IS NULL",
]

LEGACY_FLAG = "legacy_migrated_v1"


def upgrade(conn):
    md.create_all(conn, checkfirst=True)
    for table, column, ddl in NEW_COLUMNS:
        ops.add_column(conn, table, column, ddl)
    for t in md.sorted_tables:
        ops.create_model_indexes(conn, t)
    for stmt in BACKFILLS:
        conn.execute(text(stmt))
    _convert_single_page_data(conn)


# ---------------------------------------------------------------------- legacy data conversion

def _convert_single_page_data(conn):
    flag = conn.execute(select(settings.c.value).where(settings.c.key == LEGACY_FLAG)).scalar()
    if flag == "1":
        return
    now = utcnow()
    sent_at = {}
    for cid, stage, first in conn.execute(
            select(events.c.contact_id, events.c.stage, func.min(events.c.created_at))
            .where(events.c.type == "sent").group_by(events.c.contact_id, events.c.stage)):
        sent_at[(cid, stage)] = _as_dt(first)

    for camp in conn.execute(select(campaigns)).mappings().all():
        if conn.execute(select(func.count()).select_from(campaign_steps)
                        .where(campaign_steps.c.campaign_id == camp["id"])).scalar():
            continue
        rows = conn.execute(select(contacts).where(contacts.c.campaign_id == camp["id"])).mappings().all()
        n_steps = 0
        for c in rows:
            if (c["body3"] or "").strip():
                n_steps = 3
            elif (c["body2"] or "").strip():
                n_steps = max(n_steps, 2)
            elif (c["body1"] or "").strip():
                n_steps = max(n_steps, 1)
        if not n_steps:
            continue
        names, delays = ["Email 1", "Follow-up 1", "Follow-up 2"], [0, 48, 72]
        steps = []
        for i in range(n_steps):
            res = conn.execute(campaign_steps.insert().values(
                campaign_id=camp["id"], position=i + 1, name=names[i], delay_hours=delays[i]))
            steps.append({"id": res.inserted_primary_key[0], "position": i + 1, "delay_hours": delays[i]})

        for c in rows:
            legacy = [(c["subject"], c["body1"], None), (None, c["body2"], c["body2_date"]),
                      (None, c["body3"], c["body3_date"])]
            stage = c["stage"] or 0
            due_by_pos = {}
            for i, st in enumerate(steps):
                subj, body, due = legacy[i]
                done = stage >= st["position"]
                if not (body or "").strip() and not done and not (subj or "").strip():
                    continue
                due_at = datetime.combine(_as_date(due), time(0, 0)) if due and i > 0 else None
                due_by_pos[st["position"]] = due_at
                conn.execute(messages.insert().values(
                    contact_id=c["id"], step_id=st["id"], position=st["position"], subject=subj or None,
                    body=body or None, due_at=due_at, status="sent" if done else "pending",
                    sent_at=sent_at.get((c["id"], st["position"])),
                    message_id=c["message_id"] if st["position"] == 1 else None,
                    queued=bool(c["queued"]) and st["position"] == stage + 1, attempts=0,
                    created_at=now, updated_at=now))
            last = [sent_at[(c["id"], s["position"])] for s in steps if (c["id"], s["position"]) in sent_at]
            last_step_at = max(last) if last else None
            nxt = next((s for s in steps if s["position"] == stage + 1), None)
            if not nxt:
                next_due = None
            elif due_by_pos.get(nxt["position"]):
                next_due = due_by_pos[nxt["position"]]
            elif stage == 0:
                base = _as_dt(camp["start_at"])
                delay = timedelta(hours=nxt["delay_hours"])
                next_due = (base or _as_dt(c["created_at"]) or now) + delay if delay else base
            else:
                next_due = (last_step_at or now) + timedelta(hours=nxt["delay_hours"])
            conn.execute(contacts.update().where(contacts.c.id == c["id"])
                         .values(last_step_at=last_step_at, next_due_at=next_due))

    if conn.execute(select(settings.c.key).where(settings.c.key == LEGACY_FLAG)).first():
        conn.execute(settings.update().where(settings.c.key == LEGACY_FLAG).values(value="1"))
    else:
        conn.execute(settings.insert().values(key=LEGACY_FLAG, value="1"))


def _as_dt(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if hasattr(v, "year"):
        return v
    return datetime.fromisoformat(str(v)).date()
