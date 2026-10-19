"""The VPS database was created by the old single-page version. Upgrading must keep every contact,
its sent history and its custom text, and must not re-send anything."""
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import db as appdb
from app.legacy import migrate_legacy
from app.models import Campaign, CampaignStep, Contact, Message

OLD_SCHEMA = """
CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, is_active BOOLEAN DEFAULT 0, created_at DATETIME);
CREATE TABLE contacts (
  id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, company VARCHAR(300), first_name VARCHAR(150), last_name VARCHAR(150),
  email VARCHAR(300) NOT NULL, phone VARCHAR(100), priority VARCHAR(50), notes TEXT, send_flag BOOLEAN, needs_verification BOOLEAN,
  queued BOOLEAN DEFAULT 0, subject TEXT, body1 TEXT, body2 TEXT, body2_date DATE, body3 TEXT, body3_date DATE, stage INTEGER,
  message_id VARCHAR(300), unsubscribed BOOLEAN, replied BOOLEAN, created_at DATETIME);
CREATE TABLE events (id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL, stage INTEGER, type VARCHAR(30), detail TEXT, created_at DATETIME);
"""


def test_old_database_is_upgraded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as c:
        for stmt in OLD_SCHEMA.strip().split(";"):
            if stmt.strip():
                c.execute(text(stmt))
        c.execute(text("INSERT INTO campaigns (id, name, is_active) VALUES (1, 'Ireland', 1)"))
        c.execute(text(
            "INSERT INTO contacts (id, campaign_id, email, first_name, subject, body1, body2, body2_date, body3, body3_date,"
            " stage, message_id, send_flag, needs_verification, unsubscribed, replied, queued) VALUES "
            "(1, 1, 'a@x.ie', 'Ann', 'Subj A', 'Body 1A', 'Body 2A', '2026-10-20', 'Body 3A', '2026-10-25', 1, '<m1@x>', 1,0,0,0,0),"
            "(2, 1, 'b@x.ie', 'Bob', 'Subj B', 'Body 1B', NULL, NULL, NULL, NULL, 0, NULL, 1,0,0,0,1)"))
        c.execute(text("INSERT INTO events (contact_id, stage, type, detail, created_at) "
                       "VALUES (1, 1, 'sent', 'Subj A', '2026-10-16 09:00:00')"))

    with engine.begin() as conn:
        appdb.Base.metadata.create_all(bind=conn)
        appdb._ensure_columns(conn)
        for stmt in appdb.INDEXES + appdb.BACKFILLS:
            conn.execute(text(stmt))

    session = sessionmaker(bind=engine)()
    migrate_legacy(session)
    migrate_legacy(session)                               # idempotent

    camp = session.query(Campaign).one()
    assert camp.status == "active"                        # was is_active=1
    steps = session.query(CampaignStep).order_by(CampaignStep.position).all()
    assert [s.name for s in steps] == ["Email 1", "Follow-up 1", "Follow-up 2"]

    ann, bob = session.query(Contact).order_by(Contact.id).all()
    msgs = {(m.contact_id, m.position): m for m in session.query(Message)}
    assert msgs[(1, 1)].status == "sent" and msgs[(1, 1)].sent_at == datetime(2026, 10, 16, 9, 0)
    assert msgs[(1, 1)].subject == "Subj A" and msgs[(1, 1)].body == "Body 1A"
    assert msgs[(1, 2)].status == "pending" and msgs[(1, 2)].due_at == datetime(2026, 10, 20)
    assert msgs[(1, 3)].due_at == datetime(2026, 10, 25)
    assert ann.stage == 1 and ann.next_due_at == datetime(2026, 10, 20)
    assert ann.last_step_at == datetime(2026, 10, 16, 9, 0)
    assert msgs[(2, 1)].queued is True                    # "send ASAP" survives as a queued message
    assert bob.next_due_at is None
    assert session.query(Message).filter_by(contact_id=2, position=2).count() == 0
