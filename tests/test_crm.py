"""CRM features: people, companies, deals, tasks, inbox, WhatsApp, REST API, users, custom fields."""
import hashlib
import hmac
import json
import re
import time
from email import message_from_string

import pytest

from app import config, mailer
from app.channels import whatsapp
from app.crm import apikeys, people as people_svc
from app.db import SessionLocal
from app.models import (
    Activity, AuditLog, Campaign, Company, Consent, ContactPoint, Conversation, ConversationMessage, Deal,
    Enrollment, Person, PipelineStage, Task, User, WebhookEvent,
)
from app.services import create_default_steps
from app.web import app


@pytest.fixture(scope="module")
def client():
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.post("/login", data={"username": "admin", "password": "secret"})
    assert r.status_code == 302
    return c


def token(client):
    html = client.get("/people").get_data(as_text=True)
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1)


def post(client, url, **data):
    data["csrf_token"] = token(client)
    return client.post(url, data=data)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(mailer, "deliver", lambda msg, to: out.append((to, message_from_string(msg.as_string()))))
    return out


@pytest.fixture
def wa(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "WHATSAPP_TOKEN", "tok")
    monkeypatch.setattr(config, "WHATSAPP_PHONE_NUMBER_ID", "1234")
    monkeypatch.setattr(config, "WHATSAPP_APP_SECRET", "appsecret")
    monkeypatch.setattr(config, "WHATSAPP_VERIFY_TOKEN", "verify-me")

    def fake_post(payload, phone_number_id=None, client=None):
        calls.append(payload)
        return f"wamid.OUT{len(calls)}"
    monkeypatch.setattr(whatsapp, "_post", fake_post)
    return calls


def person_by_email(s, email):
    return s.query(Person).filter_by(email=email).one()


# ---------------------------------------------------------------------- people

def test_person_lifecycle(client, sent):
    r = post(client, "/people/new", first_name="Grace", last_name="Hopper", email="Grace@Navy.mil",
             phone="087 765 4321", company="US Navy", company_website="https://www.navy.mil", job_title="Admiral",
             lifecycle_stage="sql", tags="vip, navy")
    assert r.status_code == 302
    s = SessionLocal()
    g = person_by_email(s, "grace@navy.mil")                        # emails are normalised
    assert g.phone == "+353877654321" and g.lifecycle_stage == "sql"
    assert g.company.name == "US Navy" and g.company.domain == "navy.mil"
    assert sorted(t.name for t in g.tags) == ["navy", "vip"]
    assert client.get(f"/people/{g.id}").status_code == 200

    # duplicate email -> opens the existing record instead of creating a second one
    r = post(client, "/people/new", email="grace@navy.mil", first_name="Dupe")
    assert r.status_code == 302 and f"/people/{g.id}" in r.headers["Location"]

    # second identity, then make it primary
    post(client, f"/people/{g.id}/points", action="add", kind="email", value="grace@cobol.org")
    s.expire_all()
    cp = s.query(ContactPoint).filter_by(value="grace@cobol.org").one()
    assert cp.person_id == g.id and not cp.is_primary
    post(client, f"/people/{g.id}/points", action="primary", point_id=str(cp.id))
    s.expire_all()
    assert s.get(Person, g.id).email == "grace@cobol.org"

    # someone else can't take that address
    other, _ = people_svc.upsert(s, email="ada2@example.com")
    with pytest.raises(people_svc.IdentityTaken):
        people_svc.add_contact_point(s, other, "email", "GRACE@cobol.org")
    s.rollback()

    # log a call; it lands on the timeline and touches last_activity_at
    post(client, f"/people/{g.id}/log", kind="call", subject="Intro call", body="Wants a demo", duration="15")
    s.expire_all()
    assert s.query(Activity).filter_by(person_id=g.id, kind="call").one().subject == "Intro call"
    assert s.get(Person, g.id).last_activity_at is not None

    # one-to-one email from the person page -> inbox thread
    r = post(client, f"/people/{g.id}/message", channel="email", subject="Demo", body="Hi Grace, here is the demo link.")
    assert r.status_code == 302 and sent[-1][0] == "grace@cobol.org"
    s.expire_all()
    conv = s.query(Conversation).filter_by(person_id=g.id, channel="email").one()
    out = s.query(ConversationMessage).filter_by(conversation_id=conv.id).one()
    assert out.direction == "out" and out.status == "sent" and out.external_id

    # tasks
    post(client, "/tasks/new", title="Send pricing", person_id=str(g.id), due_at="2030-01-01T09:00")
    s.expire_all()
    t = s.query(Task).filter_by(person_id=g.id).one()
    post(client, f"/tasks/{t.id}/status", status="done")
    s.expire_all()
    assert s.get(Task, t.id).status == "done" and s.get(Task, t.id).completed_at
    for url in ["/tasks?view=done", f"/people/{g.id}", "/people?lifecycle=sql", "/people?tag=vip", "/people?has=phone",
                f"/inbox?c={conv.id}", "/settings/audit?entity=person"]:
        assert client.get(url).status_code == 200, url
    s.close()


def test_opt_out_blocks_email_everywhere(client, sent):
    s = SessionLocal()
    camp = Campaign(name="Opt-out camp", status="active")
    s.add(camp)
    s.flush()
    create_default_steps(s, camp)
    p, _ = people_svc.upsert(s, email="optout@example.com", first_name="Olive")
    from app.services import enroll
    e, _ = enroll(s, camp, p)
    s.commit()
    post(client, f"/people/{p.id}/consent", channel="email", status="revoked")
    s.expire_all()
    assert s.get(Enrollment, e.id).unsubscribed is True
    assert s.query(Consent).filter_by(person_id=p.id, channel="email").one().status == "revoked"
    r = post(client, f"/people/{p.id}/message", channel="email", body="Still there?")
    assert r.status_code == 302 and not [x for x in sent if x[0] == "optout@example.com"]
    s.close()


def test_merge_people(client):
    s = SessionLocal()
    camp = Campaign(name="Merge camp", status="draft")
    s.add(camp)
    s.flush()
    create_default_steps(s, camp)
    a, _ = people_svc.upsert(s, email="sam@acme.io", first_name="Sam")
    b, _ = people_svc.upsert(s, email="samuel.k@gmail.com", last_name="Kerr", phone="+353 86 111 2222")
    from app.services import enroll
    enroll(s, camp, a)
    eb, _ = enroll(s, camp, b)
    eb.stage = 2                                        # b got further in the same campaign
    people_svc.add_tags(s, b, "warm")
    s.add(Activity(kind="note", body="met at expo", person_id=b.id))
    s.commit()
    a_id, b_id = a.id, b.id
    assert client.get(f"/people/{a_id}/merge").status_code == 200
    assert client.get(f"/people/{a_id}/merge?other={b_id}").status_code == 200
    r = post(client, f"/people/{a_id}/merge", other=str(b_id))
    assert r.status_code == 302
    s.close()
    s = SessionLocal()
    keep = s.get(Person, a_id)
    assert s.get(Person, b_id) is None
    assert (keep.first_name, keep.last_name, keep.phone) == ("Sam", "Kerr", "+353861112222")
    assert {cp.value for cp in keep.contact_points} >= {"sam@acme.io", "samuel.k@gmail.com"}
    assert [e.stage for e in keep.enrollments] == [2]                   # the further enrollment won
    assert [t.name for t in keep.tags] == ["warm"]
    assert s.query(Activity).filter_by(person_id=keep.id, body="met at expo").count() == 1
    assert s.query(AuditLog).filter_by(action="merge", entity_id=str(keep.id)).count() == 1
    s.close()


# ---------------------------------------------------------------------- companies, deals

def test_companies_and_deals(client):
    r = post(client, "/companies/new", name="Initech", website="initech.com", industry="Software", city="Austin")
    assert r.status_code == 302
    s = SessionLocal()
    co = s.query(Company).filter_by(name="Initech").one()
    assert co.domain == "initech.com" and co.website == "initech.com"
    peter, _ = people_svc.upsert(s, email="peter@initech.com", first_name="Peter")
    s.commit()
    assert s.get(Person, peter.id).company_id is None     # an email domain alone doesn't attach people...
    post(client, f"/companies/{co.id}/people", person_id=str(peter.id), action="attach")   # ...you attach them
    s.expire_all()
    assert s.get(Person, peter.id).company_id == co.id

    r = post(client, "/deals/new", title="Initech - TPS reports", person_id=str(peter.id), amount="12,500", currency="usd")
    assert r.status_code == 302
    s.expire_all()
    deal = s.query(Deal).filter_by(title="Initech - TPS reports").one()
    assert deal.company_id == co.id and float(deal.amount) == 12500 and deal.currency == "USD"
    assert deal.status == "open" and [p.id for p in deal.people] == [peter.id]

    won = s.query(PipelineStage).filter_by(pipeline_id=deal.pipeline_id, kind="won").one()
    r = client.post(f"/deals/{deal.id}/move", data={"stage_id": str(won.id), "csrf_token": token(client)},
                    headers={"X-Requested-With": "fetch"})
    assert r.get_json() == {"ok": True, "status": "won", "stage": "Won"}
    s.expire_all()
    assert s.get(Deal, deal.id).status == "won" and s.get(Deal, deal.id).closed_at is not None
    for url in ["/deals", "/deals?view=list&status=won", f"/deals/{deal.id}", f"/companies/{co.id}", "/companies",
                "/"]:
        assert client.get(url).status_code == 200, url

    # deleting the company keeps its people and deals
    co_id, peter_id, deal_id = co.id, peter.id, deal.id
    post(client, f"/companies/{co_id}/delete")
    s.close()
    s = SessionLocal()
    assert s.get(Company, co_id) is None and s.get(Person, peter_id).company_id is None
    assert s.get(Deal, deal_id).company_id is None
    s.close()


def test_custom_fields_flow_into_records_and_emails(client):
    r = post(client, "/settings/fields", action="create", entity="person", label="Booking engine",
             field_type="select", options="Cloudbeds\nMews\nNone")
    assert r.status_code == 302
    post(client, "/settings/fields", action="create", entity="company", label="Rooms", field_type="number")
    s = SessionLocal()
    p, _ = people_svc.upsert(s, email="hotelier@example.ie", first_name="Hugh", company_name="Seaview Hotel")
    s.commit()
    post(client, f"/people/{p.id}/edit", first_name="Hugh", cf_booking_engine="Mews")
    s.expire_all()
    assert s.get(Person, p.id).attributes == {"booking_engine": "Mews"}
    r = post(client, f"/people/{p.id}/edit", first_name="Hugh", cf_booking_engine="Opera")   # not an option
    s.expire_all()
    assert s.get(Person, p.id).attributes == {"booking_engine": "Mews"}
    post(client, f"/companies/{p.company_id}/edit", name="Seaview Hotel", cf_rooms="42")
    s.expire_all()
    assert s.get(Company, p.company_id).attributes == {"rooms": 42}

    from app import merge
    camp = Campaign(name="Merge fields", status="draft")
    s.add(camp)
    s.flush()
    from app.services import enroll
    e, _ = enroll(s, camp, s.get(Person, p.id), variables={"angle": "direct bookings"})
    ctx = merge.context_for(e)
    assert merge.render("{{first_name}} at {{company}} ({{rooms}} rooms) uses {{booking_engine}}: {{angle}}", ctx) \
        == "Hugh at Seaview Hotel (42 rooms) uses Mews: direct bookings"
    s.rollback()
    s.close()


# ---------------------------------------------------------------------- WhatsApp

def _signed(payload):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"appsecret", raw, hashlib.sha256).hexdigest()
    return raw, sig


def test_whatsapp_inbound_reply_and_status(client, wa):
    anon = app.test_client()
    assert anon.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=42").data == b"42"
    assert anon.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=42").status_code == 403

    inbound = {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "1234"},
        "contacts": [{"profile": {"name": "Maeve Byrne"}, "wa_id": "353871112233"}],
        "messages": [{"from": "353871112233", "id": "wamid.IN1", "timestamp": str(int(time.time()) - 60), "type": "text",
                      "text": {"body": "Hi, is the offer still on?"}}]}}]}]}
    raw, sig = _signed(inbound)
    assert anon.post("/webhooks/whatsapp", data=raw, headers={"Content-Type": "application/json"}).status_code == 403
    r = anon.post("/webhooks/whatsapp", data=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.get_json()["messages"] == 1
    r = anon.post("/webhooks/whatsapp", data=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r.get_json()["duplicates"] == 1                             # Meta retries are no-ops

    s = SessionLocal()
    maeve = s.query(Person).filter_by(phone="+353871112233").one()
    assert (maeve.first_name, maeve.last_name, maeve.source) == ("Maeve", "Byrne", "whatsapp")
    conv = s.query(Conversation).filter_by(person_id=maeve.id, channel="whatsapp").one()
    assert conv.unread_count == 1 and conv.last_inbound_at is not None
    assert s.query(ConversationMessage).filter_by(conversation_id=conv.id).one().body == "Hi, is the offer still on?"

    # reply within the 24h window: free text goes out through the Cloud API
    r = post(client, f"/inbox/{conv.id}/reply", body="Yes! Until Friday.")
    assert r.status_code == 302 and wa[-1]["type"] == "text" and wa[-1]["to"] == "353871112233"
    s.expire_all()
    out = s.query(ConversationMessage).filter_by(conversation_id=conv.id, direction="out").one()
    assert out.external_id == "wamid.OUT1" and out.status == "sent"

    status = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.OUT1", "status": "delivered", "timestamp": str(int(time.time())), "recipient_id": "353871112233"},
        {"id": "wamid.OUT1", "status": "read", "timestamp": str(int(time.time())), "recipient_id": "353871112233"}]}}]}]}
    raw, sig = _signed(status)
    anon.post("/webhooks/whatsapp", data=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    s.expire_all()
    out = s.get(ConversationMessage, out.id)
    assert out.status == "read" and out.read_at and out.delivered_at
    assert s.query(WebhookEvent).filter_by(provider="whatsapp").count() == 3

    # outside the 24h window free text is refused; a template is allowed
    conv = s.get(Conversation, conv.id)
    conv.last_inbound_at = conv.last_inbound_at.replace(year=2020)
    s.commit()
    post(client, f"/inbox/{conv.id}/reply", body="Are you there?")
    assert len(wa) == 1
    post(client, f"/inbox/{conv.id}/reply", wa_template="follow_up", wa_params="Maeve | Friday")
    assert wa[-1]["type"] == "template" and wa[-1]["template"]["name"] == "follow_up"
    assert [p["text"] for p in wa[-1]["template"]["components"][0]["parameters"]] == ["Maeve", "Friday"]
    s.close()


def test_whatsapp_reply_stops_email_sequence(client, wa):
    s = SessionLocal()
    camp = Campaign(name="WA stop", status="active")
    s.add(camp)
    s.flush()
    create_default_steps(s, camp)
    p, _ = people_svc.upsert(s, email="liam@pub.ie", phone="+353 85 999 0000", first_name="Liam")
    from app.services import enroll
    e, _ = enroll(s, camp, p)
    e.stage = 1
    s.commit()
    payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "353859990000", "profile": {"name": "Liam"}}],
               "messages": [{"from": "353859990000", "id": "wamid.LIAM", "timestamp": "1761400000", "type": "text",
                             "text": {"body": "Interested, call me"}}]}}]}]}
    raw, sig = _signed(payload)
    app.test_client().post("/webhooks/whatsapp", data=raw,
                           headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    s.expire_all()
    assert s.get(Enrollment, e.id).replied is True                       # matched by phone to the same person
    assert s.query(Person).filter_by(phone="+353859990000").count() == 1
    s.close()


# ---------------------------------------------------------------------- REST API

def test_rest_api(client, sent):
    s = SessionLocal()
    raw_rw, _ = apikeys.create(s, "rw", ["read", "write"])
    raw_ro, _ = apikeys.create(s, "ro", ["read"])
    s.commit()
    api = app.test_client()
    h = {"Authorization": f"Bearer {raw_rw}"}

    assert api.get("/api/v1/people").status_code == 401
    assert api.get("/api/v1/people", headers={"Authorization": "Bearer oc_nope_nope"}).status_code == 401
    assert api.get("/api/v1/me", headers=h).get_json()["scopes"] == ["read", "write"]

    r = api.post("/api/v1/people", headers=h, json={"first_name": "Api", "email": "api@zap.io", "company": "Zap",
                                                     "tags": ["inbound"]})
    assert r.status_code == 201
    pid = r.get_json()["id"]
    assert api.post("/api/v1/people", headers=h, json={"email": "API@zap.io"}).status_code == 400   # duplicate
    assert api.post("/api/v1/people", headers={"Authorization": f"Bearer {raw_ro}"},
                    json={"email": "x@zap.io"}).status_code == 403                                # read-only key
    r = api.patch(f"/api/v1/people/{pid}", headers=h, json={"lifecycle_stage": "customer", "add_tags": ["won"]})
    assert r.status_code == 200 and r.get_json()["person"]["lifecycle_stage"] == "customer"
    assert sorted(r.get_json()["person"]["tags"]) == ["inbound", "won"]
    assert api.get("/api/v1/people/999999", headers=h).status_code == 404
    assert api.get("/api/v1/people?q=zap", headers=h).get_json()["total"] == 1

    r = api.post("/api/v1/deals", headers=h, json={"title": "Zap renewal", "person_id": pid, "amount": 900,
                                                    "stage": "Proposal sent"})
    assert r.status_code == 201 and r.get_json()["stage"] == "Proposal sent" and r.get_json()["company"]["name"] == "Zap"
    did = r.get_json()["id"]
    assert api.patch(f"/api/v1/deals/{did}", headers=h, json={"stage": "Lost", "lost_reason": "budget"}) \
        .get_json()["deal"]["status"] == "lost"
    assert api.post("/api/v1/activities", headers=h, json={"person_id": pid, "body": "Synced from Zapier"}).status_code == 201
    r = api.post("/api/v1/tasks", headers=h, json={"title": "Renewal call", "person_id": pid, "due_at": "2030-02-01"})
    assert r.status_code == 201
    assert api.patch(f"/api/v1/tasks/{r.get_json()['id']}", headers=h, json={"status": "done"}).get_json()["status"] == "done"

    camp_id = api.get("/api/v1/campaigns", headers=h).get_json()["campaigns"][0]["id"]
    r = api.post(f"/api/v1/campaigns/{camp_id}/enrollments", headers=h, json={"person_ids": [pid],
                                                                               "emails": ["nobody@nowhere.io"]})
    assert r.get_json()["results"][0]["status"] == "enrolled" and r.get_json()["unknown_emails"] == ["nobody@nowhere.io"]

    # sending needs the send scope
    assert api.post(f"/api/v1/people/{pid}/messages", headers=h, json={"channel": "email", "body": "hi"}).status_code == 403
    raw_send, _ = apikeys.create(s, "sender", ["read", "send"])
    s.commit()
    r = api.post(f"/api/v1/people/{pid}/messages", headers={"Authorization": f"Bearer {raw_send}"},
                 json={"channel": "email", "subject": "Hello", "body": "Thanks for signing up"})
    assert r.status_code == 201 and sent[-1][0] == "api@zap.io"
    assert api.get("/api/v1/search?q=zap", headers=h).get_json()["deals"][0]["title"] == "Zap renewal"
    assert api.get("/api/v1/overview", headers=h).status_code == 200
    s.expire_all()
    assert s.query(AuditLog).filter(AuditLog.actor_label == "api: rw").count() >= 5
    s.close()


# ---------------------------------------------------------------------- users

def test_users_and_login(client):
    r = post(client, "/settings/users", action="create", email="owner@example.com", name="Owner",
             password="correct horse battery", role="member")
    assert r.status_code == 302
    s = SessionLocal()
    u = s.query(User).filter_by(email="owner@example.com").one()
    assert u.role == "owner"                                       # the first user always becomes owner
    s.close()
    fresh = app.test_client()
    assert fresh.post("/login", data={"username": "owner@example.com", "password": "wrong-password"}).status_code == 200
    r = fresh.post("/login", data={"username": "OWNER@example.com", "password": "correct horse battery"})
    assert r.status_code == 302
    html = fresh.get("/").get_data(as_text=True)
    assert "Owner" in html
    # records created by this user are owned by them
    t = re.search(r'name="csrf" content="([^"]+)"', html).group(1)
    fresh.post("/people/new", data={"csrf_token": t, "email": "mine@example.com", "first_name": "Mine"})
    s = SessionLocal()
    assert s.query(Person).filter_by(email="mine@example.com").one().owner_id == u.id
    assert s.query(AuditLog).filter_by(actor_label="Owner", entity_type="person", action="create").count() == 1
    s.close()


def test_crm_only_import(client):
    import io
    csv_data = ("Email,First name,Company,Website,Job title,Tags,Rooms\n"
                "anna@castlehotel.ie,Anna,Castle Hotel,castlehotel.ie,GM,hotel,40\n"
                "ANNA@castlehotel.ie,Dup,,,,,\n"
                "grace@cobol.org,Grace H,,,,navy,\n")                 # already a person from an earlier test
    up = client.post("/import", data={"csrf_token": token(client), "campaign_id": "crm",
                                      "file": (io.BytesIO(csv_data.encode()), "people.csv")},
                     content_type="multipart/form-data")
    assert up.status_code == 302 and "campaign_id=crm" in up.headers["Location"]
    page = client.get(up.headers["Location"]).get_data(as_text=True)
    assert "stage1_body" not in page                                      # no campaign stage targets
    tok = re.search(r"/import/([0-9a-f]{24})", up.headers["Location"]).group(1)
    sels = re.findall(r'<select name="(map_\d+)">(.*?)</select>', page, re.S)
    data = {"csrf_token": token(client), "campaign_id": "crm", "mode": "upsert", "extra_custom": "1", "confirm": "1"}
    for name, body in sels:
        data[name] = re.search(r'<option value="([^"]+)" selected', body).group(1)
    s = SessionLocal()
    before = s.query(Enrollment).count()
    r = client.post(f"/import/{tok}", data=data)
    assert r.status_code == 302 and "/people" in r.headers["Location"]
    s.expire_all()
    anna = s.query(Person).filter_by(email="anna@castlehotel.ie").one()
    assert anna.first_name == "Anna" and anna.job_title == "GM" and anna.company.domain == "castlehotel.ie"
    assert anna.attributes == {"rooms": "40"} and [t.name for t in anna.tags] == ["hotel"]
    grace = s.query(ContactPoint).filter_by(value="grace@cobol.org").one().person
    assert grace.first_name == "Grace H" and "navy" in [t.name for t in grace.tags]   # updated in place
    assert s.query(Person).join(ContactPoint).filter(ContactPoint.value == "grace@cobol.org").count() == 1
    assert s.query(Enrollment).count() == before                          # nobody enrolled
    s.close()
