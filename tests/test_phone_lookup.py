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
