"""Sending mailboxes: credentials, which mailbox sends what, per-mailbox daily limits.

The mailbox flagged `from_env` uses SMTP_* / IMAP_* from .env; every other row has its own credentials.
A campaign rotates through the mailboxes listed in `campaign.mailbox_ids` (none listed = the default
mailbox only, so adding a mailbox never changes what an existing campaign sends from). Follow-ups stay on
the mailbox that sent the previous email to that person. A single pending email can name its own mailbox
(`Message.mailbox_id` before it is sent): that choice wins over rotation while the mailbox can send.
"""
import imaplib

from sqlalchemy import func

from app import config, mailer, settings as app_settings
from app.mailer import smtp_creds
from app.crm import audit, normalize
from app.models import Campaign, ConversationMessage, Mailbox, Message

PASSWORD_FIELDS = ("smtp_password", "imap_password")


# ---------------------------------------------------------------------- credentials

def imap_creds(mb):
    """(host, port, user, password) for reading replies, or None when IMAP is not set up."""
    if mb.from_env:
        host, port, user, pw = config.IMAP_HOST, config.IMAP_PORT, config.IMAP_USER, config.IMAP_PASSWORD
    else:
        host, port = mb.imap_host, mb.imap_port or 993
        user, pw = mb.imap_user or mb.smtp_user or mb.email, mb.imap_password or mb.smtp_password
    return (host, port, user, pw) if host and user and pw else None


def addresses(mb):
    """Every address mail from this mailbox can carry (From address and login names), lower case."""
    out = {mb.email}
    for creds in (smtp_creds(mb), imap_creds(mb)):
        if creds:
            out.add(creds[2])
    return {a.lower() for a in out if a}


# ---------------------------------------------------------------------- lookup

def usable(session):
    """Active mailboxes that can send, the .env one first."""
    rows = (session.query(Mailbox).filter(Mailbox.status == "active")
            .order_by(Mailbox.from_env.desc(), Mailbox.id).all())
    return [m for m in rows if smtp_creds(m)]


def ready(session):
    return bool(usable(session))


def get(session, mailbox_id):
    return session.get(Mailbox, mailbox_id) if mailbox_id else None


def default_mailbox(session):
    boxes = usable(session)
    return boxes[0] if boxes else None


def eligible_from(boxes, campaign):
    chosen = list(getattr(campaign, "mailbox_ids", None) or [])
    if not chosen:
        return boxes[:1]
    return [m for m in boxes if m.id in chosen]


def eligible(session, campaign):
    return eligible_from(usable(session), campaign)


# ---------------------------------------------------------------------- daily limits and rotation

def sent_today(session, s):
    """{mailbox_id: emails sent from it since local midnight}"""
    rows = (session.query(Message.mailbox_id, func.count(Message.id))
            .filter(Message.status == "sent", Message.mailbox_id.isnot(None),
                    Message.sent_at >= app_settings.local_midnight_utc(s))
            .group_by(Message.mailbox_id).all())
    return dict(rows)


def has_room(mb, counts):
    return not mb.daily_limit or counts.get(mb.id, 0) < mb.daily_limit


def campaigns_without_room(session, s):
    """Active campaigns that cannot send right now: no usable mailbox, or every one is at its daily limit."""
    boxes, counts, out = usable(session), sent_today(session, s), []
    for c in session.query(Campaign).filter(Campaign.status == "active"):
        if not any(has_room(m, counts) for m in eligible_from(boxes, c)):
            out.append(c.id)
    return out


def previous_mailbox_id(session, enrollment):
    """Mailbox that sent the latest email to this enrollment (None if nothing was sent yet)."""
    if enrollment is None or not enrollment.id:
        return None
    row = (session.query(Message.mailbox_id)
           .filter(Message.enrollment_id == enrollment.id, Message.status == "sent", Message.mailbox_id.isnot(None))
           .order_by(Message.position.desc()).first())
    return row[0] if row else None


def choose(session, campaign, enrollment, s, enforce_limit=True, mailbox_id=None):
    """Mailbox for the next campaign email, or None.

    A mailbox chosen for this email (mailbox_id) is used if it is active and can send (None while it is at
    its daily limit). Otherwise: stay on the mailbox used for this person before, else the eligible mailbox
    that has sent the fewest emails today. enforce_limit=False (send now / retry by hand) may exceed a
    mailbox's daily limit.
    """
    counts = sent_today(session, s)
    if mailbox_id:
        pinned = next((m for m in usable(session) if m.id == mailbox_id), None)
        if pinned:
            return pinned if (not enforce_limit or has_room(pinned, counts)) else None
    boxes = eligible(session, campaign)
    room = [m for m in boxes if has_room(m, counts)]
    pool = room or ([] if enforce_limit else boxes)
    if not pool:
        return None
    prev = previous_mailbox_id(session, enrollment)
    for m in pool:
        if m.id == prev:
            return m
    return min(pool, key=lambda m: (counts.get(m.id, 0), m.id))


def full_today(session, s):
    """Ids of mailboxes that reached their daily limit today."""
    counts = sent_today(session, s)
    return [m.id for m in session.query(Mailbox).filter(Mailbox.daily_limit.isnot(None)) if not has_room(m, counts)]


def find(session, value):
    """Mailbox by email address or id (as written in an import file), or None."""
    v = str(value or "").strip().lstrip("#").lower()
    if not v:
        return None
    if v.isdigit():
        return session.get(Mailbox, int(v))
    return session.query(Mailbox).filter(func.lower(Mailbox.email) == v).first()


def for_conversation(session, conv, mailbox_id=None):
    """Mailbox for a one-to-one email: the one asked for, else the one that last wrote in this thread,
    else the default. None when no mailbox can send."""
    boxes = usable(session)
    if mailbox_id:
        for m in boxes:
            if m.id == mailbox_id:
                return m
        raise ValueError("That mailbox is not available: choose an active mailbox with SMTP set up.")
    last = (session.query(ConversationMessage.sender)
            .filter(ConversationMessage.conversation_id == conv.id, ConversationMessage.direction == "out",
                    ConversationMessage.sender.isnot(None))
            .order_by(ConversationMessage.id.desc()).first())
    if last:
        for m in boxes:
            if m.email == (last[0] or "").lower():
                return m
    return boxes[0] if boxes else None


# ---------------------------------------------------------------------- management (panel)

def _port(value, default, label):
    value = (str(value).strip() if value is not None else "")
    if not value:
        return default
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} must be a number between 1 and 65535.")
    return port


def save(session, mb, data, actor=None):
    """Create (mb is None) or update a mailbox from form-like `data`. Blank passwords keep the stored ones.
    Raises ValueError with a message for the user. Does not commit."""
    creating = mb is None
    if creating:
        mb = Mailbox()
    if mb.from_env:
        values = {"name": (data.get("name") or mb.name).strip()[:200],
                  "daily_limit": _limit(data.get("daily_limit")),
                  "status": data.get("status") if data.get("status") in ("active", "disabled") else mb.status}
        if not values["name"]:
            raise ValueError("Enter a name for the mailbox.")
    else:
        name = (data.get("name") or "").strip()
        email = normalize.email(data.get("email"))
        if not name:
            raise ValueError("Enter a name for the mailbox.")
        if not email:
            raise ValueError("Enter the mailbox's email address.")
        clash = session.query(Mailbox).filter(func.lower(Mailbox.email) == email, Mailbox.id != (mb.id or 0)).first()
        if clash:
            raise ValueError(f"{email} is already set up as '{clash.name}'.")
        host = (data.get("smtp_host") or "").strip()
        if not host:
            raise ValueError("Enter the outgoing (SMTP) server.")
        if creating and not (data.get("smtp_password") or ""):
            raise ValueError("Enter the mailbox password.")
        values = {
            "name": name[:200], "email": email, "smtp_host": host[:300],
            "smtp_port": _port(data.get("smtp_port"), 587, "SMTP port"),
            "smtp_user": (data.get("smtp_user") or "").strip()[:320] or None,
            "imap_host": (data.get("imap_host") or "").strip()[:300] or None,
            "imap_port": _port(data.get("imap_port"), 993, "IMAP port"),
            "imap_user": (data.get("imap_user") or "").strip()[:320] or None,
            "daily_limit": _limit(data.get("daily_limit")),
            "status": data.get("status") if data.get("status") in ("active", "disabled") else (mb.status or "active"),
        }
        for field in PASSWORD_FIELDS:                      # blank = keep what is stored
            if data.get(field):
                values[field] = data[field]
    before = audit.diff(mb, values.keys()) if not creating else {}
    for k, v in values.items():
        setattr(mb, k, v)
    if creating:
        session.add(mb)
    session.flush()
    shown = {k: v for k, v in values.items() if k not in PASSWORD_FIELDS}
    audit.record(session, actor, "create" if creating else "update", "mailbox", mb.id,
                 shown if creating else audit.changes(before, audit.diff(mb, shown.keys())))
    return mb


def _limit(value):
    value = (str(value).strip() if value is not None else "")
    if not value:
        return None
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError("The daily limit must be a whole number of 1 or more, or blank for no limit.")
    return n


def delete(session, mb, actor=None):
    if mb.from_env:
        raise ValueError("The mailbox from the server's .env file cannot be deleted. Disable it instead.")
    used = [c.name for c in session.query(Campaign).all() if mb.id in (c.mailbox_ids or [])]
    if used:
        raise ValueError(f"Campaign(s) {', '.join(used)} send from this mailbox. Remove it there first, "
                         "or disable the mailbox instead.")
    audit.record(session, actor, "delete", "mailbox", mb.id, {"email": mb.email})
    session.delete(mb)


def send_test(mb, to, s):
    """Send a short test email through this mailbox. Raises mailer.SendError."""
    msg, _ = mailer.build_message(
        to_email=to, to_name="", subject="Test email from your outreach panel",
        plain=f"If you can read this, sending from {mb.email} works.",
        html_doc=f"<p>If you can read this, sending from {mb.email} works.</p>",
        sender_name=s["sender_name"], reply_to=s["reply_to"] or None, from_email=mb.email)
    mailer.deliver(msg, to, mb)


def test_imap(mb):
    """Log in to the mailbox's IMAP server. Raises ValueError / OSError / imaplib errors."""
    creds = imap_creds(mb)
    if not creds:
        raise ValueError("IMAP is not set up for this mailbox, so its replies are not read.")
    host, port, user, pw = creds
    imap = imaplib.IMAP4_SSL(host, port, timeout=10)
    try:
        imap.login(user, pw)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def public(mb, counts=None):
    """Mailbox as JSON for the API / MCP: never any password."""
    counts = counts if counts is not None else {}
    return {"id": mb.id, "name": mb.name, "email": mb.email, "status": mb.status,
            "can_send": bool(smtp_creds(mb)), "reads_replies": bool(imap_creds(mb)),
            "daily_limit": mb.daily_limit, "sent_today": counts.get(mb.id, 0), "from_env": mb.from_env}
