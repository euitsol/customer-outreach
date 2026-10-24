"""Watches the IMAP inbox for replies (stops the sequence) and bounces (marks bad addresses)."""
import email
import html as html_lib
import imaplib
import re
import time
import traceback
from datetime import timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from sqlalchemy import func

from app import config, settings as app_settings
from app.clock import utcnow
from app.crm import conversations, people as people_svc
from app.db import SessionLocal, init_db
from app.models import ContactPoint, Enrollment, Event, Message
from app.services import add_suppression

EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|autoreply|out of office|out-of-office|abwesenheit)", re.I)
BOUNCE_SUBJECT = re.compile(
    r"(undeliver|delivery status notification|mail delivery failed|returned mail|failure notice|delivery failure)", re.I)


def _decode(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def is_bounce(msg):
    sender = parseaddr(msg.get("From", ""))[1].lower()
    ctype = (msg.get("Content-Type") or "").lower()
    return (sender.startswith(("mailer-daemon", "postmaster"))
            or "report-type=delivery-status" in ctype
            or bool(BOUNCE_SUBJECT.search(_decode(msg.get("Subject")))))


def is_auto_reply(msg):
    auto = (msg.get("Auto-Submitted") or "no").strip().lower()
    if auto != "no":
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "auto_reply", "junk"):
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return True
    return bool(AUTO_SUBJECT.search(_decode(msg.get("Subject"))))


def failed_recipients(msg):
    """Addresses a delivery-failure notification says could not be reached (permanent failures only)."""
    found = set()
    xfr = msg.get("X-Failed-Recipients")
    if xfr:
        found.update(a.lower() for _, a in getaddresses([xfr]) if a)
    permanent = False
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for block in part.get_payload():
                action = (block.get("Action") or "").strip().lower()
                status = (block.get("Status") or "").strip()
                rcpt = block.get("Final-Recipient") or block.get("Original-Recipient")
                if rcpt and action == "failed" and status.startswith("5"):
                    permanent = True
                    found.add(rcpt.split(";")[-1].strip().lower())
    if not found and not permanent:
        # No structured data: fall back to scanning the text of an obvious 5xx notice.
        text = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    text += payload.decode("utf-8", "ignore")
        if re.search(r"\b5\.\d\.\d\b|\b55\d\b", text):
            found.update(a.lower() for a in EMAIL_FIND.findall(text))
    return found


def _message_ids(msg):
    refs = " ".join(filter(None, [msg.get("In-Reply-To"), msg.get("References")]))
    return set(re.findall(r"<[^>]+>", refs))


QUOTE_START = re.compile(r"^(On .+ wrote:|-----Original Message-----|From: .+|Le .+ a écrit :|Am .+ schrieb .+:)\s*$",
                         re.M)


def body_text(msg, limit=20000):
    """Readable text of an email: the text/plain part, else de-tagged HTML; quoted history cut off."""
    plain = html_part = None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    if plain is None and html_part:
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", "", html_part)
        stripped = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", stripped)
        plain = html_lib.unescape(re.sub(r"<[^>]+>", "", stripped))
    plain = (plain or "").replace("\r\n", "\n")
    m = QUOTE_START.search(plain)
    if m and m.start() > 0:
        plain = plain[:m.start()]
    lines = [ln for ln in plain.split("\n") if not ln.startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:limit]


def _msg_date(msg):
    try:
        d = parsedate_to_datetime(msg.get("Date"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except (TypeError, ValueError, IndexError):
        return utcnow()


def handle_message(session, msg, own_addrs):
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender in own_addrs:
        return None

    if is_bounce(msg):
        for addr in failed_recipients(msg):
            if addr in own_addrs:
                continue
            hit = False
            for e in session.query(Enrollment).filter(func.lower(Enrollment.email) == addr,
                                                      Enrollment.bounced == False):  # noqa: E712
                e.bounced = True
                session.add(Event(enrollment_id=e.id, stage=e.stage, type="bounced",
                                  detail=_decode(msg.get("Subject"))[:300]))
                hit = True
            for cp in session.query(ContactPoint).filter(ContactPoint.kind == "email", ContactPoint.value == addr):
                cp.status = "bounced"
                hit = True
            if hit:
                add_suppression(session, addr, "Hard bounce")
                print(f"bounce -> {addr}", flush=True)
        return "bounce"

    if is_auto_reply(msg):
        return None

    subject = _decode(msg.get("Subject"))
    enrollments = []
    ids = _message_ids(msg)
    if ids:
        enrollments = [m.enrollment for m in session.query(Message).filter(Message.message_id.in_(ids))]
    if not enrollments and sender:
        enrollments = session.query(Enrollment).filter(func.lower(Enrollment.email) == sender,
                                                       Enrollment.stage >= 1).all()
    marked = 0
    for e in enrollments:
        if not e.replied:
            e.replied = True
            session.add(Event(enrollment_id=e.id, stage=e.stage, type="replied", detail=subject[:300]))
            marked += 1
            print(f"reply  <- {e.email}: sequence stopped", flush=True)

    # Log the email in the shared inbox when it comes from someone we know.
    person = enrollments[0].person if enrollments else people_svc.find_by_email(session, sender)
    logged = False
    if person is not None:
        conversations.record_email_inbound(
            session, person, subject=subject, body=body_text(msg), message_id=(msg.get("Message-ID") or "").strip() or None,
            in_reply_to=(msg.get("In-Reply-To") or "").strip() or None, when=_msg_date(msg), sender=sender)
        logged = True
    return "reply" if marked else ("logged" if logged else None)


def check_replies(session):
    if not (config.IMAP_HOST and config.IMAP_USER):
        app_settings.put(session, "reply_status", "IMAP not configured")
        return
    own = {a.lower() for a in (config.IMAP_USER, config.SENDER_EMAIL, config.SMTP_USER) if a}
    imap = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=30)
    try:
        imap.login(config.IMAP_USER, config.IMAP_PASSWORD)
        imap.select("INBOX", readonly=True)
        validity = ""
        typ, data = imap.response("UIDVALIDITY")
        if data and data[0]:
            validity = data[0].decode() if isinstance(data[0], bytes) else str(data[0])

        last_uid = app_settings.get_raw(session, "imap_last_uid")
        if validity != app_settings.get_raw(session, "imap_uidvalidity") or last_uid is None:
            since = (utcnow() - timedelta(days=14)).strftime("%d-%b-%Y")
            typ, data = imap.uid("SEARCH", None, "SINCE", since)
            last = 0
        else:
            last = int(last_uid)
            typ, data = imap.uid("SEARCH", None, "UID", f"{last + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split()) if typ == "OK" else []
        uids = [u for u in uids if u > last]

        top = last
        for uid in uids[-500:]:
            typ, md = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            handle_message(session, msg, own)
            session.commit()
            top = max(top, uid)
        if uids:
            top = max(top, uids[-1])
        app_settings.put(session, "imap_last_uid", top)
        app_settings.put(session, "imap_uidvalidity", validity)
        app_settings.put(session, "reply_status", f"OK, checked {len(uids)} new message(s)")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def main():
    init_db()
    print("reply checker started", flush=True)
    while True:
        session = SessionLocal()
        interval = 120
        try:
            interval = app_settings.load(session)["poll_interval"]
            check_replies(session)
        except Exception as e:
            session.rollback()
            app_settings.put(session, "reply_status", f"Error: {e}")
            print("reply_checker error:", traceback.format_exc(), flush=True)
        finally:
            app_settings.put(session, "reply_heartbeat", utcnow().isoformat())
            try:
                session.commit()
            except Exception:
                session.rollback()
            session.close()
        time.sleep(max(15, interval))


if __name__ == "__main__":
    main()
