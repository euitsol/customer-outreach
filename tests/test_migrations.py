"""Schema migrations: every older database shape must upgrade without losing people, sent history,
custom text or follow-up dates, and must end up identical to a freshly created database."""
from datetime import datetime

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (register models)
from app.migrations import MIGRATIONS, migrate, v0001_baseline
from app.models import (
    Activity, Campaign, CampaignStep, Company, Consent, ContactPoint, Enrollment, Event, Message, Person, Pipeline,
    person_tags,
)

SINGLE_PAGE_SCHEMA = """
CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, is_active BOOLEAN DEFAULT 0, created_at DATETIME);
CREATE TABLE contacts (
  id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL, company VARCHAR(300), first_name VARCHAR(150), last_name VARCHAR(150),
  email VARCHAR(300) NOT NULL, phone VARCHAR(100), priority VARCHAR(50), notes TEXT, send_flag BOOLEAN, needs_verification BOOLEAN,
  queued BOOLEAN DEFAULT 0, subject TEXT, body1 TEXT, body2 TEXT, body2_date DATE, body3 TEXT, body3_date DATE, stage INTEGER,
  message_id VARCHAR(300), unsubscribed BOOLEAN, replied BOOLEAN, created_at DATETIME);
CREATE TABLE events (id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL, stage INTEGER, type VARCHAR(30), detail TEXT, created_at DATETIME);
"""


def _engine(tmp_path, name):
    return create_engine(f"sqlite:///{tmp_path}/{name}.db")


def _versions(engine):
    with engine.connect() as c:
        return [v for (v,) in c.execute(text("SELECT version FROM schema_migrations ORDER BY version"))]


def test_single_page_database_is_upgraded(tmp_path):
    engine = _engine(tmp_path, "single")
    with engine.begin() as c:
        for stmt in SINGLE_PAGE_SCHEMA.strip().split(";"):
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

    migrate(engine)
    migrate(engine)                                       # idempotent
    assert _versions(engine) == [m.VERSION for m in MIGRATIONS]

    session = sessionmaker(bind=engine)()
    camp = session.query(Campaign).one()
    assert camp.status == "active"                        # was is_active=1
    steps = session.query(CampaignStep).order_by(CampaignStep.position).all()
    assert [s.name for s in steps] == ["Email 1", "Follow-up 1", "Follow-up 2"]

    ann, bob = session.query(Enrollment).order_by(Enrollment.id).all()
    msgs = {(m.enrollment_id, m.position): m for m in session.query(Message)}
    assert msgs[(1, 1)].status == "sent" and msgs[(1, 1)].sent_at == datetime(2026, 10, 16, 9, 0)
    assert msgs[(1, 1)].subject == "Subj A" and msgs[(1, 1)].body == "Body 1A"
    assert msgs[(1, 2)].status == "pending" and msgs[(1, 2)].due_at == datetime(2026, 10, 20)
    assert msgs[(1, 3)].due_at == datetime(2026, 10, 25)
    assert ann.stage == 1 and ann.next_due_at == datetime(2026, 10, 20)
    assert ann.last_step_at == datetime(2026, 10, 16, 9, 0)
    assert msgs[(2, 1)].queued is True                    # "send ASAP" survives as a queued message
    assert bob.next_due_at is None
    assert session.query(Message).filter_by(enrollment_id=2, position=2).count() == 0
    assert ann.person.first_name == "Ann" and ann.person.email == "a@x.ie"
    assert session.query(Event).filter_by(enrollment_id=1, type="sent").count() == 1


def test_campaign_manager_database_becomes_crm(tmp_path):
    """The multi-page version (contacts with names / company / phone / notes / custom / tags)."""
    engine = _engine(tmp_path, "multi")
    with engine.begin() as c:
        v0001_baseline.md.create_all(c)
        c.execute(text("INSERT INTO campaigns (id, name, status, is_active) VALUES (1, 'A', 'active', 1), (2, 'B', 'draft', 0)"))
        c.execute(text("INSERT INTO campaign_steps (id, campaign_id, position, name, delay_hours) "
                       "VALUES (1, 1, 1, 'E1', 0), (2, 2, 1, 'E1', 0)"))
        c.execute(text(
            "INSERT INTO contacts (id, campaign_id, email, first_name, last_name, company, phone, notes, custom, send_flag,"
            " stage, unsubscribed, replied, bounced, needs_verification, created_at) VALUES "
            "(1, 1, 'Jack@Sprout.com', 'Jack', NULL, 'Sprout & Co', '087 123 4567', 'call him',"
            "  '{\"website\": \"https://www.sproutfoodco.com\", \"offer\": \"B\"}', 1, 1, 0, 0, 0, 0, '2026-10-01 10:00:00'),"
            "(2, 2, 'jack@sprout.com', NULL, 'Kirwan', 'sprout & co', NULL, 'call him', '{\"x\": \"y\"}', 1, 0, 1, 0, 0, 0,"
            "  '2026-10-02 10:00:00'),"
            "(3, 1, 'amy@gmail.com', 'Amy', 'Lee', NULL, '+353 1 555 0101', NULL, NULL, 1, 0, 0, 0, 1, 0, '2026-10-03 10:00:00')"))
        c.execute(text("INSERT INTO tags (id, name) VALUES (1, 'vip'), (2, 'hotel')"))
        c.execute(text("INSERT INTO contact_tags VALUES (1, 1), (2, 1), (2, 2), (3, 2)"))
        c.execute(text("INSERT INTO messages (id, contact_id, step_id, position, status, queued, attempts) "
                       "VALUES (1, 1, 1, 1, 'sent', 0, 1), (2, 3, 1, 1, 'pending', 0, 0)"))
        c.execute(text("INSERT INTO events (contact_id, stage, type, created_at) VALUES "
                       "(1, 1, 'sent', '2026-10-05 10:00:00'), (99, 1, 'opened', '2026-10-05 10:00:00')"))
        c.execute(text("INSERT INTO settings VALUES ('legacy_migrated_v1', '1')"))

    migrate(engine)
    migrate(engine)
    s = sessionmaker(bind=engine)()

    people = {p.email: p for p in s.query(Person)}
    assert set(people) == {"jack@sprout.com", "amy@gmail.com"}            # same address = one person
    jack, amy = people["jack@sprout.com"], people["amy@gmail.com"]
    assert (jack.first_name, jack.last_name) == ("Jack", "Kirwan")        # filled in from both rows
    assert jack.phone == "+353871234567" and amy.phone == "+35315550101"  # E.164
    company = s.query(Company).one()                                      # "Sprout & Co" == "sprout & co"
    assert jack.company_id == company.id and company.domain == "sproutfoodco.com"
    assert amy.company_id is None                                         # gmail says nothing about a company
    assert sorted(e.campaign_id for e in jack.enrollments) == [1, 2]
    e1 = s.get(Enrollment, 1)
    assert e1.email == "jack@sprout.com" and e1.variables == {"website": "https://www.sproutfoodco.com", "offer": "B"}
    assert s.get(Enrollment, 3).variables is None
    assert {t for (t,) in s.query(person_tags.c.tag_id).filter(person_tags.c.person_id == jack.id)} == {1, 2}
    assert s.query(Consent).filter_by(person_id=jack.id, channel="email").one().status == "revoked"
    assert s.query(ContactPoint).filter_by(value="amy@gmail.com").one().status == "bounced"
    assert [a.body for a in s.query(Activity).filter_by(person_id=jack.id)] == ["call him"]   # de-duplicated
    assert s.query(Event).count() == 1                                    # orphan event dropped
    assert s.get(Message, 1).enrollment_id == 1
    assert s.query(Pipeline).count() == 1

    cols = {c["name"] for c in inspect(engine).get_columns("enrollments")}
    assert not cols & {"first_name", "last_name", "company", "phone", "notes", "custom", "body1"}
    assert "contact_tags" not in inspect(engine).get_table_names()


def test_migrated_schema_matches_fresh_schema(tmp_path):
    fresh, old = _engine(tmp_path, "fresh"), _engine(tmp_path, "old")
    migrate(fresh)
    with old.begin() as c:
        v0001_baseline.md.create_all(c)
    migrate(old)
    fi, oi = inspect(fresh), inspect(old)
    assert set(fi.get_table_names()) == set(oi.get_table_names())
    for table in fi.get_table_names():
        assert {c["name"] for c in fi.get_columns(table)} == {c["name"] for c in oi.get_columns(table)}, table
        missing = {i["name"] for i in fi.get_indexes(table)} - {i["name"] for i in oi.get_indexes(table)}
        # unique constraints are inline on fresh SQLite tables and unique indexes after migration
        assert not {m for m in missing if not m.startswith("uq_")}, (table, missing)
