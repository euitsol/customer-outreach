"""Several sending mailboxes: panel management, rotation inside one campaign, per-mailbox limits,
sticky follow-ups, one-to-one choice, reply checking, API / MCP."""
import re
from email import message_from_string
from itertools import count
from types import SimpleNamespace

import pytest

from app import mailboxes, mailer, reply_checker, sender, settings as app_settings, worker
from app.clock import utcnow
from app.crm import audit, conversations as conv_svc, people as people_svc
from app.db import SessionLocal
from app.models import Campaign, CampaignStep, Mailbox, Message
from app.services import create_default_steps, enroll
from app.web import app

_n = count(1)


@pytest.fixture(scope="module")
def client():
    app.config["TESTING"] = True
    c = app.test_client()
    assert c.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return c


def token(client):
    html = client.get("/settings/mailboxes").get_data(as_text=True)
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1)


def post(client, url, **data):
    data["csrf_token"] = token(client)
    return client.post(url, data=data, follow_redirects=True)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake(msg, to_email, mailbox=None):
        # plain copy: the mailbox object belongs to the session of whoever sent (a request session may close)
        box = SimpleNamespace(id=mailbox.id, email=mailbox.email, from_env=mailbox.from_env)
        sent.append((to_email, box, message_from_string(msg.as_string())))
    monkeypatch.setattr(mailer, "deliver", fake)
    monkeypatch.setattr(worker, "_sleep", lambda *a, **k: None)
    return sent


def new_mailbox(db, name=None, **kw):
    n = next(_n)
    mb = Mailbox(name=name or f"Box {n}", email=f"box{n}-{id(db)}@mb.test", smtp_host="smtp.mb.test", smtp_port=465,
                 smtp_password="pw", imap_host=kw.pop("imap_host", None), **kw)
    db.add(mb)
    db.commit()
    return mb


def new_campaign(db, mailbox_ids=None, people=3, status="active", daily_limit=None):
    n = next(_n)
    camp = Campaign(name=f"MB camp {n} {id(db)}", status=status, is_active=status == "active",
                    mailbox_ids=mailbox_ids or [], daily_limit=daily_limit)
    db.add(camp)
    db.flush()
    create_default_steps(db, camp)
    steps = list(db.query(CampaignStep).filter_by(campaign_id=camp.id).order_by(CampaignStep.position))
    steps[0].subject, steps[0].body = "Hello {{first_name}}", "Hi {{first_name}}, a quick note."
    steps[1].body = "Following up."
    enrolled = []
    for i in range(people):
        p, _ = people_svc.upsert(db, email=f"p{n}-{i}-{id(db)}@lead.test", first_name=f"P{i}")
        e, _ = enroll(db, camp, p)
        enrolled.append(e)
    db.commit()
    return camp, steps, enrolled


def send_one(db, enrollment, step, camp, steps, **kw):
    s = app_settings.load(db)
    out = sender.send_stage(db, enrollment, step, s=s, campaign=camp, steps=steps, **kw)
    db.commit()
    return out


# ---------------------------------------------------------------------- the .env mailbox

def test_env_mailbox_is_seeded_and_is_the_default(db):
    env = db.query(Mailbox).filter_by(from_env=True).one()
    assert env.email == "sender@example.com" and env.status == "active"
    assert mailboxes.default_mailbox(db).id == env.id
    assert mailboxes.smtp_creds(env) == ("smtp.test", 587, "sender@example.com", "x")


# ---------------------------------------------------------------------- panel management

def test_add_edit_and_delete_a_mailbox(client, db):
    form = dict(action="create", name="Bengali Translations", email="Marketing@Bengali.test", smtp_host="mail.bengali.test",
                smtp_port="465", smtp_password="s3cret-pw", imap_host="mail.bengali.test", imap_port="993",
                daily_limit="40")
    page = post(client, "/settings/mailboxes", **form).get_data(as_text=True)
    assert "marketing@bengali.test" in page and "s3cret-pw" not in page
    mb = db.query(Mailbox).filter_by(email="marketing@bengali.test").one()
    mid = mb.id
    assert mb.smtp_password == "s3cret-pw" and mb.daily_limit == 40 and mb.smtp_port == 465 and not mb.from_env
    assert mailboxes.smtp_creds(mb) == ("mail.bengali.test", 465, "marketing@bengali.test", "s3cret-pw")
    # IMAP login and password fall back to the SMTP ones
    assert mailboxes.imap_creds(mb) == ("mail.bengali.test", 993, "marketing@bengali.test", "s3cret-pw")

    # a blank password keeps the stored one
    post(client, "/settings/mailboxes", action="update", mailbox_id=mid, name="Bengali", email=mb.email,
         smtp_host="mail.bengali.test", smtp_port="587", smtp_password="", status="active", daily_limit="")
    db.expire_all()
    mb = db.get(Mailbox, mid)
    assert mb.name == "Bengali" and mb.smtp_port == 587 and mb.smtp_password == "s3cret-pw" and mb.daily_limit is None

    # the audit log never holds a password
    from app.models import AuditLog
    entries = db.query(AuditLog).filter_by(entity_type="mailbox", entity_id=str(mid)).all()
    assert {e.action for e in entries} == {"create", "update"}
    assert "s3cret-pw" not in str([e.changes for e in entries])

    post(client, "/settings/mailboxes", action="delete", mailbox_id=mid)
    db.expire_all()
    assert db.get(Mailbox, mid) is None


@pytest.mark.parametrize("bad", [
    dict(smtp_host=""), dict(smtp_password=""), dict(email="not-an-email"), dict(smtp_port="99999"),
    dict(daily_limit="0"), dict(email="sender@example.com"), dict(name=""),
])
def test_invalid_mailbox_is_rejected(client, db, bad):
    before = db.query(Mailbox).count()
    form = dict(action="create", name="X", email="x@invalid.test", smtp_host="h.test", smtp_port="465",
                smtp_password="pw")
    form.update(bad)
    page = post(client, "/settings/mailboxes", **form).get_data(as_text=True)
    assert 'class="flash err"' in page or "flash err" in page
    db.expire_all()
    assert db.query(Mailbox).count() == before


def test_env_mailbox_cannot_be_deleted_or_lose_its_source(client, db):
    env = db.query(Mailbox).filter_by(from_env=True).one()
    page = post(client, "/settings/mailboxes", action="delete", mailbox_id=env.id).get_data(as_text=True)
    assert "cannot be deleted" in page
    post(client, "/settings/mailboxes", action="update", mailbox_id=env.id, name="Main", email="evil@x.test",
         smtp_host="evil.test", smtp_password="evil", status="active", daily_limit="")
    db.expire_all()
    env = db.get(Mailbox, env.id)
    assert env.name == "Main" and env.email == "sender@example.com" and env.smtp_host is None
    assert mailboxes.smtp_creds(env)[0] == "smtp.test"


def test_mailbox_used_by_a_campaign_cannot_be_deleted(client, db):
    mb = new_mailbox(db)
    camp, *_ = new_campaign(db, [mb.id], people=1)
    page = post(client, "/settings/mailboxes", action="delete", mailbox_id=mb.id).get_data(as_text=True)
    assert camp.name in page
    db.expire_all()
    assert db.get(Mailbox, mb.id) is not None


def test_only_admins_change_mailboxes(client, db):
    from werkzeug.security import generate_password_hash
    from app.models import User
    u = User(email="member-mb@x.test", role="member", is_active=True, password_hash=generate_password_hash("pw-member-123"))
    db.add(u)
    db.commit()
    c = app.test_client()
    assert c.post("/login", data={"username": "member-mb@x.test", "password": "pw-member-123"}).status_code == 302
    html = c.get("/settings/mailboxes").get_data(as_text=True)
    t = re.search(r'name="csrf" content="([^"]+)"', html).group(1)
    r = c.post("/settings/mailboxes", data={"csrf_token": t, "action": "create", "name": "N", "email": "n@x.test",
                                            "smtp_host": "h", "smtp_password": "p"})
    assert r.status_code == 403
    assert db.query(Mailbox).filter_by(email="n@x.test").count() == 0


def test_pages_render_with_mailboxes(client, db):
    new_mailbox(db)
    camp, *_ = new_campaign(db, people=1)
    for url in ["/settings/mailboxes", "/settings", f"/campaigns/{camp.id}/edit", "/settings/integrations", "/",
                "/inbox", "/help/mailboxes", "/help/mailboxes?lang=bn"]:
        assert client.get(url).status_code == 200, url


# ---------------------------------------------------------------------- rotation inside one campaign

def test_a_campaign_rotates_between_its_mailboxes_and_followups_stay_put(db, outbox):
    a, b = new_mailbox(db), new_mailbox(db)
    camp, steps, people = new_campaign(db, [a.id, b.id], people=4)
    for e in people:
        assert send_one(db, e, steps[0], camp, steps)[0] == "sent"
    used = [db.query(Message).filter_by(enrollment_id=e.id, position=1).one().mailbox_id for e in people]
    assert sorted(used) == sorted([a.id, a.id, b.id, b.id])
    # the From header and the SMTP account both follow the mailbox
    for to, mb, msg in outbox:
        assert mb.email in msg["From"] and mb.email in msg["List-Unsubscribe"]
        assert msg["Message-ID"].endswith("@mb.test>")
    # make the first person's mailbox the busier one: the follow-up still goes out from it
    first = people[0]
    original = db.query(Message).filter_by(enrollment_id=first.id, position=1).one().mailbox_id
    db.add(Message(enrollment_id=people[1].id, step_id=steps[2].id, position=3, status="sent", sent_at=utcnow(),
                   mailbox_id=original))
    db.commit()
    before = len(outbox)
    send_one(db, first, steps[1], camp, steps)
    assert len(outbox) == before + 1
    assert db.query(Message).filter_by(enrollment_id=first.id, position=2).one().mailbox_id == original
    assert outbox[-1][1].id == original


def test_followup_falls_back_when_its_mailbox_is_no_longer_allowed(db, outbox):
    a, b = new_mailbox(db), new_mailbox(db)
    camp, steps, (e,) = new_campaign(db, [a.id, b.id], people=1)
    send_one(db, e, steps[0], camp, steps)
    first_box = db.query(Message).filter_by(enrollment_id=e.id, position=1).one().mailbox_id
    keep = b.id if first_box == a.id else a.id
    camp.mailbox_ids = [keep]
    db.commit()
    send_one(db, e, steps[1], camp, steps)
    assert db.query(Message).filter_by(enrollment_id=e.id, position=2).one().mailbox_id == keep


def test_campaign_without_a_choice_only_uses_the_default_mailbox(db, outbox):
    new_mailbox(db), new_mailbox(db)
    camp, steps, people = new_campaign(db, [], people=3)
    for e in people:
        send_one(db, e, steps[0], camp, steps)
    env = db.query(Mailbox).filter_by(from_env=True).one()
    assert {mb.id for _, mb, _ in outbox} == {env.id}
    assert all("sender@example.com" in msg["From"] for _, _, msg in outbox)


def test_campaign_settings_form_saves_the_mailbox_choice(client, db):
    a, b = new_mailbox(db), new_mailbox(db)
    camp, *_ = new_campaign(db, people=1)
    data = {"csrf_token": token(client), "name": camp.name, "mailbox_ids": [str(a.id), str(b.id), "99999", "x"],
            "track_opens": "1", "track_clicks": "1"}
    assert client.post(f"/campaigns/{camp.id}/edit", data=data).status_code == 302
    db.expire_all()
    assert db.get(Campaign, camp.id).mailbox_ids == sorted([a.id, b.id])
    page = client.get(f"/campaigns/{camp.id}/edit").get_data(as_text=True)
    assert f'value="{a.id}" checked' in page
    data["mailbox_ids"] = []
    client.post(f"/campaigns/{camp.id}/edit", data=data)
    db.expire_all()
    assert db.get(Campaign, camp.id).mailbox_ids == []


def test_duplicate_keeps_the_mailbox_choice(db):
    a = new_mailbox(db)
    camp, *_ = new_campaign(db, [a.id], people=1)
    from app.services import duplicate_campaign
    copy = duplicate_campaign(db, camp, camp.name + " copy")
    db.commit()
    assert copy.mailbox_ids == [a.id]


# ---------------------------------------------------------------------- limits and availability

def test_mailbox_daily_limit_holds_the_campaign_back(db, outbox):
    box = new_mailbox(db, daily_limit=1)
    camp, steps, people = new_campaign(db, [box.id], people=3)
    s = app_settings.load(db)
    assert send_one(db, people[0], steps[0], camp, steps)[0] == "sent"
    counts = mailboxes.sent_today(db, s)
    assert counts[box.id] == 1 and not mailboxes.has_room(box, counts)
    assert camp.id in mailboxes.campaigns_without_room(db, s)
    nxt = worker.pick_auto(db, s, utcnow())
    assert nxt is None or nxt.campaign_id != camp.id           # the worker skips the campaign
    # an explicit "send now" is not blocked by the mailbox limit
    msg = Message(enrollment_id=people[1].id, step_id=steps[0].id, position=1, status="pending", queued=True)
    db.add(msg)
    db.commit()
    assert send_one(db, people[1], steps[0], camp, steps, message=msg)[0] == "sent"
    assert sender.mailboxes.choose(db, camp, people[2], s, enforce_limit=True) is None


def test_limit_of_one_mailbox_moves_the_work_to_the_other(db, outbox):
    a, b = new_mailbox(db, daily_limit=1), new_mailbox(db)
    camp, steps, people = new_campaign(db, [a.id, b.id], people=4)
    for e in people:
        assert send_one(db, e, steps[0], camp, steps)[0] == "sent"
    boxes = [db.query(Message).filter_by(enrollment_id=e.id, position=1).one().mailbox_id for e in people]
    assert boxes.count(a.id) == 1 and boxes.count(b.id) == 3


def test_no_usable_mailbox_is_a_system_problem(db, outbox):
    box = new_mailbox(db, status="disabled")
    camp, steps, (e,) = new_campaign(db, [box.id], people=1)
    outcome, err = send_one(db, e, steps[0], camp, steps)
    assert outcome == "system" and "No mailbox" in err and outbox == []
    assert db.get(Campaign, camp.id).id in mailboxes.campaigns_without_room(db, app_settings.load(db))
    # nothing was counted against the email
    assert db.query(Message).filter_by(enrollment_id=e.id, status="sent").count() == 0


def test_mailbox_without_credentials_is_not_usable(db):
    box = Mailbox(name="Half", email="half@mb.test", smtp_host="h.test", smtp_password=None)
    db.add(box)
    db.commit()
    assert box.id not in [m.id for m in mailboxes.usable(db)]


def test_worker_cycle_sends_from_the_chosen_mailboxes(db, outbox):
    a, b = new_mailbox(db), new_mailbox(db)
    camp, steps, people = new_campaign(db, [a.id, b.id], people=4)
    worker.run_cycle(db)
    mine = {e.email for e in people}
    got = [(to, mb.id) for to, mb, _ in outbox if to in mine]
    assert len(got) == 4 and {mb for _, mb in got} == {a.id, b.id}


# ---------------------------------------------------------------------- one-to-one email

def test_one_to_one_follows_the_thread_and_can_be_overridden(db, outbox):
    a, b = new_mailbox(db), new_mailbox(db)
    camp, steps, (e,) = new_campaign(db, [b.id], people=1)
    send_one(db, e, steps[0], camp, steps)
    person = e.person
    outbox.clear()
    conv_svc.send(db, person, "email", "Thanks for your time", settings=app_settings.load(db))
    assert outbox[-1][1].id == b.id and b.email in outbox[-1][2]["From"]      # same mailbox as the campaign email
    conv_svc.send(db, person, "email", "From the other one", mailbox_id=a.id, settings=app_settings.load(db))
    assert outbox[-1][1].id == a.id
    conv_svc.send(db, person, "email", "And back", settings=app_settings.load(db))
    assert outbox[-1][1].id == a.id                                            # the thread's latest mailbox
    db.commit()
    with pytest.raises(conv_svc.SendBlocked):
        conv_svc.send(db, person, "email", "x", mailbox_id=987654, settings=app_settings.load(db))
    off = new_mailbox(db, status="disabled")
    with pytest.raises(conv_svc.SendBlocked):
        conv_svc.send(db, person, "email", "x", mailbox_id=off.id, settings=app_settings.load(db))


def test_one_to_one_without_any_mailbox_is_blocked(db, outbox, monkeypatch):
    p, _ = people_svc.upsert(db, email="nobody-mb@lead.test", first_name="No")
    db.commit()
    monkeypatch.setattr(mailboxes, "usable", lambda session: [])
    with pytest.raises(conv_svc.SendBlocked, match="No mailbox"):
        conv_svc.send(db, p, "email", "hello", settings=app_settings.load(db))


def test_compose_forms_offer_the_choice_with_several_mailboxes(client, db, outbox):
    a = new_mailbox(db)
    p, _ = people_svc.upsert(db, email="compose-mb@lead.test", first_name="Cee")
    db.commit()
    html = client.get(f"/people/{p.id}").get_data(as_text=True)
    assert 'name="mailbox_id"' in html and a.email in html
    r = client.post(f"/people/{p.id}/message", data={"csrf_token": token(client), "channel": "email", "subject": "Hi",
                                                      "body": "Hello there", "mailbox_id": str(a.id)})
    assert r.status_code == 302
    assert outbox[-1][1].id == a.id


# ---------------------------------------------------------------------- tests of the connection

def test_smtp_test_uses_the_chosen_mailbox(client, db, outbox):
    a = new_mailbox(db)
    r = client.post("/settings/test-smtp", data={"csrf_token": token(client), "to": "me@x.test",
                                                  "mailbox_id": str(a.id), "next": "mailboxes"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/mailboxes")
    to, mb, msg = outbox[-1]
    assert to == "me@x.test" and mb.id == a.id and a.email in msg["From"]
    # default when none is chosen: the .env mailbox
    client.post("/settings/test-smtp", data={"csrf_token": token(client), "to": "me@x.test"})
    assert outbox[-1][1].from_env


def test_imap_test_reports_missing_imap(client, db):
    a = new_mailbox(db)
    r = client.post("/settings/test-imap", data={"csrf_token": token(client), "mailbox_id": str(a.id),
                                                  "next": "mailboxes"}, follow_redirects=True)
    assert "IMAP is not set up" in r.get_data(as_text=True)


# ---------------------------------------------------------------------- reply checking

class FakeImap:
    log = []
    fail_hosts = set()

    def __init__(self, host, port, timeout=None):
        if host in self.fail_hosts:
            raise OSError("cannot connect")
        self.host = host

    def login(self, user, password):
        self.log.append((self.host, user, password))

    def select(self, *a, **k):
        return "OK", [b"0"]

    def response(self, name):
        return name, [b"77"]

    def uid(self, *a):
        return "OK", [b""]

    def logout(self):
        pass


def test_reply_checker_reads_every_mailbox_and_survives_one_failing(db, monkeypatch):
    ok_box = new_mailbox(db, imap_host="imap.ok.test")
    bad_box = new_mailbox(db, imap_host="imap.bad.test")
    no_imap = new_mailbox(db)
    FakeImap.log, FakeImap.fail_hosts = [], {"imap.bad.test"}
    monkeypatch.setattr(reply_checker.imaplib, "IMAP4_SSL", FakeImap)
    reply_checker.check_replies(db)
    db.commit()
    hosts = {h for h, _, _ in FakeImap.log}
    assert "imap.ok.test" in hosts and "imap.bad.test" not in hosts
    assert (("imap.ok.test", ok_box.email, "pw")) in FakeImap.log
    db.expire_all()
    ok_box, bad_box, no_imap = (db.get(Mailbox, m.id) for m in (ok_box, bad_box, no_imap))
    assert ok_box.imap_uidvalidity == "77" and ok_box.reply_status.startswith("OK")
    assert bad_box.reply_status.startswith("Error") and bad_box.imap_last_uid is None
    assert no_imap.reply_status is None
    status = app_settings.get_raw(db, "reply_status")
    assert ok_box.name in status and bad_box.name in status


def test_mail_between_our_own_mailboxes_is_ignored(db):
    a = new_mailbox(db)
    raw = message_from_string(f"From: {a.email}\nTo: x@y.test\nSubject: hi\nMessage-ID: <own1@x>\n\nbody")
    own = {x for m in db.query(Mailbox) for x in mailboxes.addresses(m)}
    assert reply_checker.handle_message(db, raw, own) is None


# ---------------------------------------------------------------------- API and MCP

def test_api_lists_mailboxes_without_passwords_and_sends_from_one(client, db, outbox):
    from app.crm import apikeys
    a = new_mailbox(db)
    key, _ = apikeys.create(db, "mb-test", ["read", "write", "send"], None, None, audit.Actor("system"))
    db.commit()
    h = {"Authorization": f"Bearer {key}"}
    r = app.test_client().get("/api/v1/mailboxes", headers=h)
    assert r.status_code == 200
    rows = r.get_json()["mailboxes"]
    assert any(m["id"] == a.id and m["can_send"] and m["email"] == a.email for m in rows)
    assert all("password" not in m and "smtp_password" not in m for m in rows)
    p, _ = people_svc.upsert(db, email="api-mb@lead.test", first_name="Api")
    db.commit()
    c = app.test_client()
    r = c.post(f"/api/v1/people/{p.id}/messages", headers=h,
               json={"channel": "email", "body": "hi", "subject": "S", "mailbox_id": a.id})
    assert r.status_code == 201, r.get_data(as_text=True)
    assert outbox[-1][1].id == a.id
    r = c.post(f"/api/v1/people/{p.id}/messages", headers=h,
               json={"channel": "email", "body": "hi", "subject": "S", "mailbox_id": "abc"})
    assert r.status_code == 400 and "mailbox_id" in r.get_data(as_text=True)


def test_mcp_exposes_mailboxes():
    import anyio
    from app import mcp_server

    async def go():
        return {t.name: t for t in await mcp_server.server.list_tools()}
    tools = anyio.run(go)
    assert "list_mailboxes" in tools and tools["list_mailboxes"].annotations.read_only_hint is True
    assert "mailbox_id" in tools["send_message"].input_schema["properties"]


# ---------------------------------------------------------------------- migration

def test_migration_adds_the_env_mailbox_and_keeps_the_reply_position(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker
    from app.migrations import migrate, v0001_baseline
    engine = create_engine(f"sqlite:///{tmp_path}/mb.db")
    with engine.begin() as c:
        v0001_baseline.md.create_all(c)
        c.execute(text("INSERT INTO settings VALUES ('imap_last_uid', '55'), ('imap_uidvalidity', '9')"))
    migrate(engine)
    migrate(engine)                                                       # idempotent: still one env mailbox
    s = sessionmaker(bind=engine)()
    env = s.query(Mailbox).one()
    assert env.from_env and env.email == "sender@example.com"
    assert (env.imap_last_uid, env.imap_uidvalidity) == ("55", "9")
    assert "mailbox_id" in {c["name"] for c in inspect(engine).get_columns("messages")}
    assert "mailbox_ids" in {c["name"] for c in inspect(engine).get_columns("campaigns")}
