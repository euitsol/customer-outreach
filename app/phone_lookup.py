"""Daily number check with Twilio Lookup v2, driven by Settings (limit per day, paid line-type on/off).

Free check: is it a real, correctly formatted phone number. Paid check (`lookup_paid`, about $0.008 per
number): also the line type. Neither tells whether the number is registered on WhatsApp; that is only
learned from a real message, so `whatsapp-verified` is set when a message is delivered or the person writes.

Outcome per number (same machinery as a failed WhatsApp send, `services.flag_whatsapp_invalid`):
  not a valid number            -> blocked, tag `whatsapp-invalid`
  landline / toll-free / ...    -> blocked, tag `no-whatsapp` (paid check only)
  anything else                 -> `verified_at` set (never checked again), tag stays `whatsapp-pending`
"""
import httpx
from sqlalchemy import case, exists

from app import config, settings as app_settings
from app.clock import utcnow
from app.models import Campaign, ContactPoint, Enrollment, Person, Tag, person_tags
from app.services import WA_STATUS_TAGS, flag_whatsapp_invalid, set_whatsapp_status

URL = "https://lookups.twilio.com/v2/PhoneNumbers/"
PAID_COST_USD = 0.008
NO_WHATSAPP_TYPES = {"landline", "tollFree", "premium", "sharedCost", "uan", "voicemail", "pager"}


class LookupUnavailable(Exception):
    """Twilio refused or failed the request in a way that means: stop for now."""


def credentials_ok():
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN)


def lookup(number, client, paid=False):
    """(valid, line_type). line_type is None unless `paid` and Twilio knows it. Raises LookupUnavailable."""
    params = {"Fields": "line_type_intelligence"} if paid else None
    try:
        resp = client.get(URL + number.replace("+", "%2B"), params=params,
                          auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN))
    except httpx.HTTPError as e:
        raise LookupUnavailable(f"Could not reach Twilio Lookup: {e}")
    if resp.status_code == 404:
        return False, None
    if resp.status_code != 200:
        raise LookupUnavailable(f"Twilio Lookup HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise LookupUnavailable("Twilio Lookup returned a non-JSON response")
    lti = body.get("line_type_intelligence") or {}
    return bool(body.get("valid")), (lti.get("type") if paid else None)


def _unchecked(session):
    return (session.query(ContactPoint).filter(ContactPoint.kind.in_(("phone", "whatsapp")),
                                               ContactPoint.status == "active", ContactPoint.verified_at.is_(None)))


def _candidates(session, limit):
    """Unchecked, still-active numbers; those in a WhatsApp campaign first."""
    in_wa = exists().where(Enrollment.email == ContactPoint.value).where(
        Enrollment.campaign_id == Campaign.id).where(Campaign.channel == "whatsapp")
    rows = _unchecked(session).order_by(case((in_wa, 0), else_=1), ContactPoint.id).all()
    seen = []
    for cp in rows:
        if cp.value.startswith("+") and cp.value not in seen:
            seen.append(cp.value)
        if len(seen) >= limit:
            break
    return seen


def tag_pending(session):
    """People with an unchecked number and no WhatsApp status yet get `whatsapp-pending`."""
    has_status = exists().where(person_tags.c.person_id == Person.id).where(
        person_tags.c.tag_id == Tag.id).where(Tag.name.in_(WA_STATUS_TAGS.values()))
    ids = {cp.person_id for cp in _unchecked(session)}
    if not ids:
        return 0
    people = session.query(Person).filter(Person.id.in_(ids), ~has_status).all()
    for p in people:
        set_whatsapp_status(session, p, "pending")
    return len(people)


def _today(session, s, now):
    return app_settings.to_local(now, s).date().isoformat()


def usage(session, s=None, now=None):
    """What the settings page shows: today's checks, the limit, how many numbers wait, estimated spend."""
    s = s or app_settings.load(session)
    now = now or utcnow()
    done = int(app_settings.get_raw(session, "lookup_count", 0) or 0) \
        if app_settings.get_raw(session, "lookup_day") == _today(session, s, now) else 0
    waiting = len({cp.value for cp in _unchecked(session).all() if cp.value.startswith("+")})
    paid_total = int(app_settings.get_raw(session, "lookup_paid_total", 0) or 0)
    return dict(done=done, limit=s["lookup_daily_limit"], waiting=waiting, paid=s["lookup_paid"],
                paid_total=paid_total, spend_total=round(paid_total * PAID_COST_USD, 2),
                spend_max_today=round(s["lookup_daily_limit"] * PAID_COST_USD, 2) if s["lookup_paid"] else 0,
                configured=credentials_ok())


def run_daily(session, client=None, now=None, limit=None):
    """Check up to the daily limit of new numbers (`limit` overrides it for a manual run). Commits.
    Returns (checked, blocked)."""
    s = app_settings.load(session)
    cap = s["lookup_daily_limit"] if limit is None else limit
    if not credentials_ok() or cap <= 0:
        return 0, 0
    now = now or utcnow()
    paid = s["lookup_paid"]
    tag_pending(session)
    today = _today(session, s, now)
    done = int(app_settings.get_raw(session, "lookup_count", 0) or 0) \
        if app_settings.get_raw(session, "lookup_day") == today else 0
    todo = _candidates(session, cap - done) if cap > done else []
    if not todo:
        session.commit()
        return 0, 0
    own = client is None
    client = client or httpx.Client(timeout=15)
    checked = blocked = 0
    try:
        for number in todo:
            try:
                valid, line_type = lookup(number, client, paid=paid)
            except LookupUnavailable as e:
                print(f"phone lookup stopped: {e}", flush=True)
                break
            checked += 1
            if not valid:
                blocked += 1
                flag_whatsapp_invalid(session, number, "Twilio Lookup: not a valid phone number")
            elif line_type in NO_WHATSAPP_TYPES:
                blocked += 1
                flag_whatsapp_invalid(session, number, f"Twilio Lookup: {line_type} line, can't use WhatsApp",
                                      status="none")
            else:
                for cp in session.query(ContactPoint).filter(ContactPoint.kind.in_(("phone", "whatsapp")),
                                                             ContactPoint.value == number):
                    cp.verified_at = now
    finally:
        if own:
            client.close()
        app_settings.put(session, "lookup_day", today)
        app_settings.put(session, "lookup_count", done + checked)
        if paid:
            app_settings.put(session, "lookup_paid_total",
                             int(app_settings.get_raw(session, "lookup_paid_total", 0) or 0) + checked)
        session.commit()
    if checked:
        print(f"phone lookup: {checked} checked, {blocked} blocked", flush=True)
    return checked, blocked
