"""Daily Twilio Lookup check: blocks bad numbers, tags WhatsApp status, limit + paid mode come from Settings."""
import httpx

from app import config, phone_lookup, settings as app_settings
from app.db import SessionLocal
from app.models import ContactPoint, Person
from app.crm import people as people_svc
from app.services import set_whatsapp_status


def _tags(s, email):
    return {t.name for t in s.query(Person).filter_by(email=email).one().tags}


def _client(types, seen):
    def handler(request):
        number = request.url.path.rsplit("/", 1)[1].replace("%2B", "+")
        seen.append((number, request.url.params.get("Fields")))
        t = types.get(number[-4:], "mobile")
        body = {"valid": t != "invalid"}
        if request.url.params.get("Fields"):
            body["line_type_intelligence"] = {"type": t}
        return httpx.Response(200, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_daily_lookup_free_then_paid(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "tok")
    s = SessionLocal()
    for i, e in enumerate(("lk-good", "lk-bad", "lk-land"), 1):
        people_svc.upsert(s, email=f"{e}@example.com", phone=f"+35387111000{i}")
    s.commit()
    types = {"0002": "invalid", "0003": "landline"}
    seen = []
    app_settings.save_many(s, {"lookup_daily_limit": 1000, "lookup_paid": False})
    s.commit()

    checked, blocked = phone_lookup.run_daily(s, client=_client(types, seen))
    assert checked >= 3 and blocked >= 1
    assert all(f is None for _, f in seen)                                  # free: no data package requested
    assert "whatsapp-pending" in _tags(s, "lk-good@example.com")
    assert "whatsapp-invalid" in _tags(s, "lk-bad@example.com")
    assert "no-whatsapp" not in _tags(s, "lk-land@example.com")             # landline unknown without paid check
    assert phone_lookup.run_daily(s, client=_client(types, seen)) == (0, 0)  # nothing new

    # paid mode: unchecked numbers get the line type; a landline is now blocked
    people_svc.upsert(s, email="lk-land2@example.com", phone="+353871110003")
    people_svc.upsert(s, email="lk-mob@example.com", phone="+353871110009")
    cp = s.query(ContactPoint).filter_by(value="+353871110003").one()
    cp.verified_at = None
    s.commit()
    app_settings.save_many(s, {"lookup_paid": True})
    s.commit()
    seen.clear()
    phone_lookup.run_daily(s, client=_client(types, seen))
    assert ("+353871110003", "line_type_intelligence") in seen
    assert "no-whatsapp" in _tags(s, "lk-land@example.com")
    assert "whatsapp-pending" in _tags(s, "lk-mob@example.com")
    assert phone_lookup.usage(s)["paid_total"] >= 2
    s.close()


def test_daily_limit_from_settings(monkeypatch):
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "tok")
    s = SessionLocal()
    for i in range(3):
        people_svc.upsert(s, email=f"lim{i}@example.com", phone=f"+35387222000{i}")
    s.commit()
    app_settings.put(s, "lookup_count", 0)                                  # earlier tests used today's allowance
    app_settings.save_many(s, {"lookup_daily_limit": 0})
    s.commit()
    assert phone_lookup.run_daily(s, client=_client({}, [])) == (0, 0)      # 0 = off
    app_settings.save_many(s, {"lookup_daily_limit": 2, "lookup_paid": False})
    s.commit()
    seen = []
    assert phone_lookup.run_daily(s, client=_client({}, seen))[0] == 2
    assert phone_lookup.run_daily(s, client=_client({}, seen)) == (0, 0)    # today's allowance is used up
    assert phone_lookup.run_daily(s, client=_client({}, seen), limit=1)[0] == 0   # manual limit counts today's checks too
    s.close()


def test_verified_tag_never_overrides_a_block():
    s = SessionLocal()
    p, _ = people_svc.upsert(s, email="lk-ver@example.com", phone="+353871113333")
    set_whatsapp_status(s, p, "invalid")
    set_whatsapp_status(s, p, "verified")
    assert {t.name for t in p.tags} >= {"whatsapp-invalid"} and "whatsapp-verified" not in {t.name for t in p.tags}
    q, _ = people_svc.upsert(s, email="lk-ver2@example.com", phone="+353871114444")
    set_whatsapp_status(s, q, "pending")
    set_whatsapp_status(s, q, "verified")
    assert {t.name for t in q.tags} == {"whatsapp-verified"}
    s.close()


def test_backfill_from_past_results():
    from app.crm import conversations
    from app.models import ConversationMessage
    s = SessionLocal()
    rows = {}
    for key, phone, status, error, direction in (
            ("ok", "+353871115001", "delivered", None, "out"),
            ("read", "+353871115002", "read", None, "out"),
            ("bad", "+353871115003", "failed", "63024: Invalid message recipient", "out"),
            ("flaky", "+353871115004", "failed", "30008: Unknown error", "out"),
            ("wrote", "+353871115005", "received", None, "in")):
        p, _ = people_svc.upsert(s, email=f"bf-{key}@example.com", phone=phone)
        conv = conversations.get_or_create(s, p, "whatsapp")
        s.add(ConversationMessage(conversation_id=conv.id, direction=direction, channel="whatsapp",
                                  status=status, error=error))
        rows[key] = p.email
    people_svc.upsert(s, email="bf-untouched@example.com", phone="+353871115006")
    s.commit()

    phone_lookup.backfill(s)
    s.commit()
    assert _tags(s, rows["ok"]) == {"whatsapp-verified"} and _tags(s, rows["read"]) == {"whatsapp-verified"}
    assert _tags(s, rows["wrote"]) == {"whatsapp-verified"}
    assert _tags(s, rows["bad"]) == {"whatsapp-invalid"}
    assert _tags(s, rows["flaky"]) == {"whatsapp-pending"}              # other failures prove nothing
    assert _tags(s, "bf-untouched@example.com") == {"whatsapp-pending"}
    assert s.query(ContactPoint).filter_by(value="+353871115001").one().verified_at is not None   # no Lookup spend
    s.close()


def _login_client():
    import re
    from app.web import app
    app.config["TESTING"] = True
    c = app.test_client()
    assert c.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    token = re.search(r'name="csrf" content="([^"]+)"', c.get("/people").get_data(as_text=True)).group(1)
    return c, token


def _patch_twilio(monkeypatch, types=None):
    real = httpx.Client
    seen = []
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "tok")
    mock = _client(types or {}, seen)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=mock._transport, **kw))
    return seen


def test_verify_button_on_person_page_logs_and_skips_verified(monkeypatch):
    from app.models import AuditLog
    seen = _patch_twilio(monkeypatch, {"6002": "invalid"})
    s = SessionLocal()
    good, _ = people_svc.upsert(s, email="vb-good@example.com", phone="+353871116001")
    bad, _ = people_svc.upsert(s, email="vb-bad@example.com", phone="+353871116002")
    done, _ = people_svc.upsert(s, email="vb-done@example.com", phone="+353871116003")
    set_whatsapp_status(s, done, "verified")
    s.commit()
    ids = (good.id, bad.id, done.id)
    c, token = _login_client()
    assert b"Verify WhatsApp number" in c.get(f"/people/{ids[0]}").data
    assert b"Verify WhatsApp number" not in c.get(f"/people/{ids[2]}").data      # already verified: no button
    for pid in ids:
        assert c.post(f"/people/{pid}/verify-whatsapp", data={"csrf_token": token}).status_code == 302
    assert [n for n, _ in seen] == ["+353871116001", "+353871116002"]            # verified person never looked up
    s2 = SessionLocal()
    assert "whatsapp-invalid" in _tags(s2, "vb-bad@example.com")
    rows = s2.query(AuditLog).filter_by(action="whatsapp_check").order_by(AuditLog.id.desc()).limit(2).all()
    assert {r.changes["source"] for r in rows} == {"person page"} and {r.entity_id for r in rows} == {str(ids[0]), str(ids[1])}
    s2.close()
    s.close()


def test_verify_campaign_queues_then_worker_checks_pending_only(monkeypatch):
    from app.crm import audit
    from app.models import Campaign
    from app.services import enroll
    seen = _patch_twilio(monkeypatch)
    s = SessionLocal()
    camp = Campaign(name="Verify me", channel="whatsapp", status="draft", is_active=False)
    s.add(camp)
    s.flush()
    pending, _ = people_svc.upsert(s, email="vc-pending@example.com", phone="+353871117001")
    verified, _ = people_svc.upsert(s, email="vc-verified@example.com", phone="+353871117002")
    set_whatsapp_status(s, verified, "verified")
    for p in (pending, verified):
        enroll(s, camp, p, actor=audit.SYSTEM)
    s.commit()
    cid = camp.id
    c, token = _login_client()
    assert b"Verify pending numbers" in c.get(f"/campaigns/{cid}").data
    assert c.post(f"/campaigns/{cid}/verify-whatsapp", data={"csrf_token": token}).status_code == 302
    assert seen == []                                                          # queued, not run inside the request
    assert phone_lookup.queue_size(s) == 1
    assert phone_lookup.run_queue(s) == (1, 0)
    assert [n for n, _ in seen] == ["+353871117001"] and phone_lookup.queue_size(s) == 0
    s.close()
