"""End-to-end checks of the panel, the worker and the tracking endpoints (SQLite, SMTP mocked)."""
import re
from datetime import datetime, timedelta
from app.clock import utcnow
from email import message_from_string
from email.header import decode_header, make_header

import pytest

from app import mailer, worker
from app.db import SessionLocal
from app.mailer import SendError
from app.models import Campaign, Enrollment, Event, Message, Person, Suppression
from app.web import app


@pytest.fixture(scope="module")
def client():
    app.config["TESTING"] = True
    c = app.test_client()
    assert c.get("/").status_code == 302          # login required
    r = c.post("/login", data={"username": "admin", "password": "secret"})
    assert r.status_code == 302
    return c


def csrf(client):
    html = client.get("/campaigns/new").get_data(as_text=True)
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1)


def post(client, url, **data):
    data["csrf_token"] = csrf(client)
    return client.post(url, data=data, follow_redirects=False)


class Outbox(list):
    state = None


@pytest.fixture
def outbox(monkeypatch):
    sent = Outbox()
    state = {"fail": None}

    def fake_deliver(msg, to_email):
        if state["fail"]:
            raise state["fail"]
        sent.append((to_email, message_from_string(msg.as_string())))
    monkeypatch.setattr(mailer, "deliver", fake_deliver)
    monkeypatch.setattr(worker, "_sleep", lambda *a, **k: None)
    sent.state = state
    return sent


def subj(m):
    return str(make_header(decode_header(m["Subject"])))


CSV = (
    "Title row,,\n"
    "Email,First name,Company,Subject line,Email 1,Follow-up 1,Follow-up 1 date,Website,Send?\n"
    'jane@acme.ie,Jane,Acme,Hello Jane,"Hi Jane,\n\nSee https://acme.ie/x.",,2099-01-01,https://acme.ie,Send\n'
    "bob@beta.ie,Bob,Beta,,,,,https://beta.ie,Send\n"
    "bad-email,Nope,,,,,,,Send\n"
    "skip@gamma.ie,Skip,Gamma,,,,,,Do not send\n"
)


def test_pages_render_empty(client):
    for url in ["/", "/campaigns", "/enrollments", "/outbox", "/activity", "/import", "/settings", "/tags",
                "/suppression", "/templates", "/campaigns/new", "/enrollments/new", "/healthz", "/people",
                "/people/new", "/companies", "/companies/new", "/deals", "/deals?view=list", "/deals/new", "/tasks",
                "/tasks?view=done", "/inbox", "/settings/users", "/settings/api-keys", "/settings/integrations",
                "/settings/fields", "/settings/pipelines", "/settings/audit"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/contacts").status_code == 302         # old URL redirects


def test_full_flow(client, outbox):
    # --- create campaign with default stages
    r = post(client, "/campaigns/new", name="Test Camp", description="demo")
    assert r.status_code == 302
    s = SessionLocal()
    camp = s.query(Campaign).filter_by(name="Test Camp").one()
    cid = camp.id
    steps = camp.steps
    assert [st.position for st in steps] == [1, 2, 3]

    # --- write the templates
    post(client, f"/campaigns/{cid}/steps/{steps[0].id}/save", name="Intro", subject="Quick idea for {{company}}",
         body="Hi {{first_name|there}},\n\nGeneric text for {{company}}.\n\nBye", delay_days="0", delay_hours="0")
    post(client, f"/campaigns/{cid}/steps/{steps[1].id}/save", name="Nudge", subject="",
         body="Hi {{first_name|there}}, any thoughts?", delay_days="2", delay_hours="3")

    # --- import: map, preview, confirm
    up = client.post("/import", data={"csrf_token": csrf(client), "campaign_id": str(cid),
                                       "file": (__import__("io").BytesIO(CSV.encode()), "leads.csv")},
                     content_type="multipart/form-data")
    assert up.status_code == 302
    mapping_url = up.headers["Location"]
    page = client.get(mapping_url).get_data(as_text=True)
    assert "Map columns" in page and "3 data rows" not in page  # 4 rows incl. bad ones
    token = re.search(r"/import/([0-9a-f]{24})", mapping_url).group(1)
    # use the guessed mapping by reading it back from the rendered selects
    sels = re.findall(r'<select name="(map_\d+)">(.*?)</select>', page, re.S)
    data = {"csrf_token": csrf(client), "campaign_id": str(cid), "mode": "upsert", "extra_custom": "1", "confirm": "1"}
    for name, body in sels:
        data[name] = re.search(r'<option value="([^"]+)" selected', body).group(1)
    assert "email" in data.values() and "step1_body" in data.values() and "step2_date" in data.values()
    assert data["map_7"] == "company_website"   # Website goes to the company record
    r = client.post(f"/import/{token}", data=data)
    assert r.status_code == 302, r.get_data(as_text=True)[:500]

    s.expire_all()
    contacts = {c.email: c for c in s.query(Enrollment).filter_by(campaign_id=cid)}
    assert set(contacts) == {"jane@acme.ie", "bob@beta.ie"}        # invalid + do-not-send rows dropped
    jane = contacts["jane@acme.ie"]
    assert jane.person.first_name == "Jane" and jane.person.company.name == "Acme"
    assert jane.person.company.website == "https://acme.ie" and jane.person.company.domain == "acme.ie"
    m1 = s.query(Message).filter_by(enrollment_id=jane.id, position=1).one()
    assert m1.subject == "Hello Jane" and "acme.ie/x" in m1.body
    m2 = s.query(Message).filter_by(enrollment_id=jane.id, position=2).one()
    assert m2.due_at.date().isoformat() in ("2099-01-01", "2098-12-31")                          # personalised send date

    # --- start the campaign; the worker sends stage 1 to both, with merge fields applied
    post(client, f"/campaigns/{cid}/status", action="start")
    s.expire_all()
    assert s.get(Campaign, cid).status == "active"
    worker.run_cycle(s)
    assert len(outbox) == 2
    by_to = {to: m for to, m in outbox}
    assert subj(by_to["jane@acme.ie"]) == "Hello Jane"           # custom subject
    assert subj(by_to["bob@beta.ie"]) == "Quick idea for Beta"   # template subject, merged
    jane_plain = by_to["jane@acme.ie"].get_payload()[0].get_payload(decode=True).decode()
    assert "https://track.example.com/t/c/" in jane_plain             # link tracked, trailing '.' kept outside
    assert "Unsubscribe" in jane_plain
    assert by_to["jane@acme.ie"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    bob_plain = by_to["bob@beta.ie"].get_payload()[0].get_payload(decode=True).decode()
    assert "Hi Bob," in bob_plain and "Generic text for Beta" in bob_plain

    s.expire_all()
    bob = s.query(Enrollment).filter_by(email="bob@beta.ie").one()
    assert bob.stage == 1 and bob.message_id
    assert bob.next_due_at is not None and bob.next_due_at > utcnow() + timedelta(hours=50)  # 2d3h wait
    assert s.get(Enrollment, jane.id).next_due_at == m2.due_at

    # --- nothing more is due; running again sends nothing
    worker.run_cycle(s)
    assert len(outbox) == 2

    # --- "send now" on stage 2 for Bob, with SMTP failing first, then retried from the panel
    outbox.state["fail"] = SendError("451 try later", "message")
    step2 = steps[1]
    post(client, f"/enrollments/{bob.id}/stage/{step2.id}/action", action="send_now")
    worker.run_cycle(s)
    s.expire_all()
    mb = s.query(Message).filter_by(enrollment_id=bob.id, position=2).one()
    assert mb.status == "failed" and mb.attempts == 1 and "451" in mb.last_error
    assert s.get(Enrollment, bob.id).stage == 1                        # stage did not advance
    outbox.state["fail"] = None
    worker.run_cycle(s)                                                # backoff not elapsed: auto-retry waits
    assert len(outbox) == 2
    post(client, f"/campaigns/{cid}/steps/{step2.id}/action", action="retry_failed")
    worker.run_cycle(s)
    s.expire_all()
    assert len(outbox) == 3
    assert outbox[-1][0] == "bob@beta.ie" and subj(outbox[-1][1]) == "Re: Quick idea for Beta"
    assert outbox[-1][1]["In-Reply-To"] == s.get(Enrollment, bob.id).message_id
    assert s.get(Enrollment, bob.id).stage == 2

    # --- every campaign email is threaded into the person's inbox conversation
    from app.models import Conversation, ConversationMessage
    conv = s.query(Conversation).filter_by(person_id=s.get(Enrollment, bob.id).person_id, channel="email").one()
    assert s.query(ConversationMessage).filter_by(conversation_id=conv.id, direction="out").count() == 2

    # --- permanent refusal marks the enrollment bounced and suppresses the address
    s.get(Enrollment, jane.id).next_due_at = None
    s.commit()
    outbox.state["fail"] = SendError("550 no such user", "recipient")
    worker.run_cycle(s)
    s.expire_all()
    assert s.get(Enrollment, jane.id).bounced is True
    assert s.query(Suppression).filter_by(value="jane@acme.ie").count() == 1
    outbox.state["fail"] = None

    # --- every page renders with data in it
    bob_person = s.get(Enrollment, bob.id).person_id
    for url in ["/", f"/campaigns/{cid}", f"/campaigns/{cid}/sequence", f"/campaigns/{cid}/enrollments",
                f"/campaigns/{cid}/outbox?view=failed", f"/campaigns/{cid}/edit", "/enrollments?status=due&sort=due",
                f"/enrollments/{bob.id}", f"/enrollments/{jane.id}", "/outbox?view=queued", "/outbox?view=failed",
                "/activity?type=sent", "/activity/export", "/enrollments/export", "/suppression",
                f"/campaigns/{cid}/steps/{steps[0].id}/preview?enrollment_id={bob.id}",
                f"/messages/{mb.id}", "/import/template.csv", f"/people/{bob_person}", "/people?q=beta",
                "/people/export.csv", f"/people/{bob_person}/merge", "/companies?q=acme", "/inbox",
                f"/inbox?c={conv.id}", "/people/lookup.json?q=bo", "/companies/lookup.json?q=ac"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
    pv = client.get(f"/campaigns/{cid}/steps/{steps[0].id}/preview?enrollment_id={bob.id}").get_json()
    assert pv["subject"] == "Quick idea for Beta"
    sample = client.get(f"/campaigns/{cid}/steps/{steps[0].id}/preview").get_json()
    assert sample["subject"] == "Quick idea for Example Ltd"


def test_tracking_and_unsubscribe(client):
    s = SessionLocal()
    bob = s.query(Enrollment).filter_by(email="bob@beta.ie").one()
    msg = s.query(Message).filter_by(enrollment_id=bob.id, position=1).one()
    msg.sent_at = utcnow() - timedelta(minutes=5)
    s.commit()
    # public endpoints need no login
    anon = app.test_client()
    r = anon.get(f"/t/o/{msg.open_token}.gif")
    assert r.status_code == 200 and r.mimetype == "image/gif"
    assert s.query(Event).filter_by(enrollment_id=bob.id, type="opened").count() == 1
    assert anon.get("/t/o/nope.gif").status_code == 200       # unknown token: still a pixel, no error

    from app.models import ClickLink
    link = s.query(ClickLink).filter_by(enrollment_id=bob.id).first()
    if link:
        r = anon.get(f"/t/c/{link.token}")
        assert r.status_code == 302 and r.headers["Location"] == link.target_url

    # GET only asks for confirmation; POST unsubscribes (also what mail clients send for one-click)
    assert anon.get(f"/t/u/{msg.unsub_token}").status_code == 200
    s.expire_all()
    assert s.get(Enrollment, bob.id).unsubscribed is False
    assert anon.post(f"/t/u/{msg.unsub_token}", data={"List-Unsubscribe": "One-Click"}).status_code == 200
    s.expire_all()
    assert s.get(Enrollment, bob.id).unsubscribed is True
    assert s.query(Suppression).filter_by(value="bob@beta.ie").count() == 1
    from app.crm import people as people_svc
    assert people_svc.consent_status(s, s.get(Enrollment, bob.id).person, "email") == "revoked"


def test_csrf_and_auth(client):
    anon = app.test_client()
    assert anon.get("/people").status_code == 302
    assert anon.post("/enrollments/bulk", data={"action": "delete"}).status_code == 401
    assert anon.post("/people/bulk", data={"action": "delete"}).status_code == 401
    assert client.post("/campaigns/new", data={"name": "x"}).status_code == 400   # missing csrf token


def test_reply_and_bounce_detection():
    from app import reply_checker
    s = SessionLocal()
    camp = Campaign(name="Replies", status="active")
    s.add(camp)
    s.flush()
    from app.crm import people as people_svc
    from app.services import enroll
    c, _ = enroll(s, camp, people_svc.upsert(s, email="reply@x.ie", first_name="Rae")[0])
    d, _ = enroll(s, camp, people_svc.upsert(s, email="gone@y.ie")[0])
    c.stage = d.stage = 1
    s.commit()
    own = {"sender@example.com"}

    auto = message_from_string("From: reply@x.ie\nSubject: Automatic reply: hi\nAuto-Submitted: auto-replied\n\nout")
    assert reply_checker.handle_message(s, auto, own) is None
    s.expire_all()
    assert s.get(Enrollment, c.id).replied is False

    real = message_from_string("From: Reply <reply@x.ie>\nSubject: Re: hi\nMessage-ID: <r1@x.ie>\n\n"
                               "Sounds good\n\nOn Mon, 1 Jan 2026 Tester wrote:\n> original text")
    assert reply_checker.handle_message(s, real, own) == "reply"
    s.commit()
    assert s.get(Enrollment, c.id).replied is True
    from app.models import Conversation, ConversationMessage
    conv = s.query(Conversation).filter_by(person_id=s.get(Enrollment, c.id).person_id).one()
    inbound = s.query(ConversationMessage).filter_by(conversation_id=conv.id, direction="in").one()
    assert inbound.body == "Sounds good" and conv.unread_count == 1       # quoted history stripped
    assert reply_checker.handle_message(s, real, own) == "logged"         # same Message-ID again: no duplicate
    s.commit()
    assert s.query(ConversationMessage).filter_by(conversation_id=conv.id, direction="in").count() == 1

    bounce = message_from_string(
        "From: MAILER-DAEMON@mx.example.com\nSubject: Delivery Status Notification (Failure)\n"
        "X-Failed-Recipients: gone@y.ie\n\nThe address was not found. 550 5.1.1")
    assert reply_checker.handle_message(s, bounce, own) == "bounce"
    s.commit()
    assert s.get(Enrollment, d.id).bounced is True
    from app.models import ContactPoint
    assert s.query(ContactPoint).filter_by(kind="email", value="gone@y.ie").one().status == "bounced"
