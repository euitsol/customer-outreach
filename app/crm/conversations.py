"""Shared inbox: one thread per person per channel, fed by campaign emails, IMAP replies, WhatsApp
webhooks and replies typed in the panel / sent by Claude through the MCP server."""
from datetime import timedelta

from sqlalchemy import func

from app import config, mailer, settings as app_settings
from app.channels import account_for, whatsapp
from app.clock import utcnow
from app.crm import audit, normalize, people as people_svc
from app.models import Conversation, ConversationMessage, Enrollment, Event, Person


class SendBlocked(ValueError):
    """The message may not be sent (consent, channel rules, missing address)."""


# ---------------------------------------------------------------------- threads

def get_or_create(session, person, channel, subject=None, thread_id=None):
    conv = (session.query(Conversation)
            .filter(Conversation.person_id == person.id, Conversation.channel == channel)
            .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc()).first())
    if conv is None:
        acct = account_for(session, channel)
        conv = Conversation(person_id=person.id, channel=channel, channel_account_id=acct.id if acct else None,
                            subject=(subject or None) and subject[:500], external_thread_id=thread_id,
                            status="open", unread_count=0)
        session.add(conv)
        session.flush()
    else:
        if subject and not conv.subject:
            conv.subject = subject[:500]
        if thread_id and not conv.external_thread_id:
            conv.external_thread_id = thread_id
    return conv


def add_message(session, conv, *, direction, body=None, subject=None, sender=None, recipient=None, status=None,
                external_id=None, sequence_message_id=None, user_id=None, meta=None, when=None, error=None):
    """Append a message to a thread (idempotent on (channel, external_id))."""
    if external_id:
        dup = (session.query(ConversationMessage)
               .filter(ConversationMessage.channel == conv.channel, ConversationMessage.external_id == external_id)
               .first())
        if dup:
            return dup, False
    when = when or utcnow()
    m = ConversationMessage(
        conversation_id=conv.id, direction=direction, channel=conv.channel, sender=sender, recipient=recipient,
        subject=(subject or None) and subject[:500], body=body, external_id=external_id,
        status=status or ("received" if direction == "in" else "sent"), sequence_message_id=sequence_message_id,
        user_id=user_id, meta=meta, sent_at=when, error=error)
    session.add(m)
    conv.last_message_at = max(filter(None, [conv.last_message_at, when]))
    if direction == "in":
        conv.last_inbound_at = max(filter(None, [conv.last_inbound_at, when]))
        conv.unread_count = (conv.unread_count or 0) + 1
        conv.status = "open"
    session.flush()
    return m, True


def mark_read(conv):
    conv.unread_count = 0


def set_status(session, conv, status, actor=None):
    if status not in ("open", "pending", "closed"):
        raise ValueError("Unknown conversation status.")
    conv.status = status
    if status == "closed":
        conv.unread_count = 0
    audit.record(session, actor, "status", "conversation", conv.id, {"status": status})


def inbox_query(session, status="open", channel=None, assignee_id=None, term=None):
    q = session.query(Conversation).join(Person, Person.id == Conversation.person_id)
    if status == "unread":
        q = q.filter(Conversation.unread_count > 0)
    elif status in ("open", "pending", "closed"):
        q = q.filter(Conversation.status == status)
    if channel:
        q = q.filter(Conversation.channel == channel)
    if assignee_id:
        q = q.filter(Conversation.assignee_id == assignee_id)
    if term:
        like = f"%{term.lower()}%"
        q = q.filter(func.lower(func.coalesce(Person.first_name, "") + " " + func.coalesce(Person.last_name, "")
                                + " " + func.coalesce(Person.email, "") + " " + func.coalesce(Person.phone, ""))
                     .like(like))
    return q.order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc())


def unread_total(session):
    return session.query(func.coalesce(func.sum(Conversation.unread_count), 0)).filter(
        Conversation.status != "closed").scalar() or 0


# ---------------------------------------------------------------------- inbound

def stop_sequences(session, person, reason):
    """Any reply, on any channel, stops automatic campaign emails to that person."""
    n = 0
    for e in session.query(Enrollment).filter(Enrollment.person_id == person.id, Enrollment.replied == False):  # noqa: E712
        if (e.stage or 0) >= 1:
            e.replied = True
            session.add(Event(enrollment_id=e.id, stage=e.stage, type="replied", detail=reason[:300]))
            n += 1
    return n


def record_whatsapp_inbound(session, item):
    """Store one parsed inbound WhatsApp message; creates the person if the number is new."""
    phone = normalize.from_whatsapp_id(item["from"])
    person = people_svc.find_by_phone(session, phone)
    created = False
    if person is None:
        first, last = normalize.split_name(item.get("name"))
        person, created = people_svc.upsert(session, phone=phone, first_name=first, last_name=last,
                                            source="whatsapp", actor=audit.Actor("system", label="whatsapp webhook"))
    try:
        people_svc.add_contact_point(session, person, "whatsapp", phone)
    except people_svc.IdentityTaken:
        pass
    conv = get_or_create(session, person, "whatsapp")
    msg, new = add_message(session, conv, direction="in", body=item.get("text"), sender=phone,
                           external_id=item.get("id"), when=item.get("timestamp"),
                           meta={"type": item.get("type"), "context_id": item.get("context_id")})
    if new:
        people_svc.touch(person, item.get("timestamp"))
        stop_sequences(session, person, "Replied on WhatsApp")
    return msg, created


def record_whatsapp_status(session, item):
    m = (session.query(ConversationMessage)
         .filter(ConversationMessage.channel == "whatsapp", ConversationMessage.external_id == item["id"]).first())
    if m is None:
        return False
    rank = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
    new = item.get("status")
    if new == "failed":
        m.status, m.error = "failed", item.get("error") or "Delivery failed"
    elif rank.get(new, -1) > rank.get(m.status, -1):
        m.status = new
    if new == "delivered":
        m.delivered_at = item.get("timestamp")
    if new == "read":
        m.read_at = item.get("timestamp")
        m.delivered_at = m.delivered_at or item.get("timestamp")
    return True


def process_whatsapp_webhook(session, payload):
    """Returns counts. Every item is first written to webhook_events, so Meta's retries are no-ops."""
    from app.models import WebhookEvent
    counts = {"messages": 0, "statuses": 0, "duplicates": 0}
    for kind, item in whatsapp.parse_webhook(payload):
        if not item.get("id"):
            continue
        key = item["id"] if kind == "message" else f"{item['id']}:{item.get('status')}"
        if session.query(WebhookEvent).filter_by(provider="whatsapp", external_id=key).first():
            counts["duplicates"] += 1
            continue
        ev = WebhookEvent(provider="whatsapp", external_id=key, event_type=kind,
                          payload=item.get("raw") or {k: v for k, v in item.items() if k != "timestamp"})
        session.add(ev)
        session.flush()
        try:
            with session.begin_nested():
                if kind == "message":
                    record_whatsapp_inbound(session, item)
                    counts["messages"] += 1
                else:
                    record_whatsapp_status(session, item)
                    counts["statuses"] += 1
            ev.status, ev.processed_at = "processed", utcnow()
        except Exception as e:  # noqa: BLE001 - keep the payload, surface the error
            ev.status, ev.error, ev.processed_at = "failed", str(e)[:1000], utcnow()
    return counts


def record_email_inbound(session, person, *, subject, body, message_id, in_reply_to=None, when=None,
                         sender=None):
    conv = get_or_create(session, person, "email", subject=subject, thread_id=in_reply_to)
    msg, new = add_message(session, conv, direction="in", body=body, subject=subject, sender=sender,
                           external_id=message_id, when=when, meta={"in_reply_to": in_reply_to} if in_reply_to else None)
    if new:
        people_svc.touch(person, when)
    return msg


def record_campaign_email(session, enrollment, message, subject, external_id, when):
    """Link a sent campaign email into the person's email thread (body stays on the sequence message)."""
    person = enrollment.person
    if person is None:
        return None
    conv = get_or_create(session, person, "email", subject=subject, thread_id=enrollment.message_id)
    m, _ = add_message(session, conv, direction="out", subject=subject, recipient=enrollment.email, status="sent",
                       external_id=external_id, sequence_message_id=message.id, when=when,
                       meta={"campaign_id": enrollment.campaign_id, "stage": message.position})
    people_svc.touch(person, when, contacted=True)
    return m


# ---------------------------------------------------------------------- outbound

def whatsapp_window_open(conv, now=None):
    now = now or utcnow()
    return bool(conv.last_inbound_at and now - conv.last_inbound_at < timedelta(hours=whatsapp.SERVICE_WINDOW_HOURS))


def send(session, person, channel, body, *, subject=None, conversation=None, user=None, actor=None,
         template=None, template_language="en_US", template_params=None, settings=None):
    """Send a one-to-one message now and record it. Raises SendBlocked / mailer.SendError /
    whatsapp.WhatsAppError. A failed attempt is still recorded (status failed) before re-raising."""
    if person.do_not_contact:
        raise SendBlocked(f"{person.display_name} is marked do-not-contact.")
    if people_svc.consent_status(session, person, channel) == "revoked":
        raise SendBlocked(f"{person.display_name} has opted out of {channel} messages.")
    body = (body or "").strip()
    conv = conversation or get_or_create(session, person, channel, subject=subject)
    if conv.person_id != person.id:
        raise SendBlocked("That conversation belongs to someone else.")
    now = utcnow()
    s = settings or app_settings.load(session)

    if channel == "email":
        if not person.email:
            raise SendBlocked("This person has no email address.")
        if not body:
            raise SendBlocked("Write a message first.")
        from app.services import is_suppressed
        if is_suppressed(session, person.email):
            raise SendBlocked(f"{person.email} is on the do-not-contact list.")
        last_in = (session.query(ConversationMessage)
                   .filter(ConversationMessage.conversation_id == conv.id, ConversationMessage.direction == "in",
                           ConversationMessage.external_id.isnot(None))
                   .order_by(ConversationMessage.id.desc()).first())
        subj = (subject or "").strip()
        if not subj:
            base = (last_in.subject if last_in and last_in.subject else conv.subject) or ""
            subj = base if base.lower().startswith("re:") or not base else f"Re: {base}"
        plain, html_doc = mailer.simple_bodies(body, s["signature"])
        msg, new_id = mailer.build_message(
            to_email=person.email, to_name=person.full_name, subject=subj, plain=plain, html_doc=html_doc,
            sender_name=s["sender_name"], reply_to=s["reply_to"] or None,
            in_reply_to=last_in.external_id if last_in else None)
        try:
            mailer.deliver(msg, person.email)
        except mailer.SendError as e:
            add_message(session, conv, direction="out", body=body, subject=subj, recipient=person.email,
                        status="failed", error=str(e)[:1000], user_id=user.id if user else None, when=now)
            raise
        m, _ = add_message(session, conv, direction="out", body=body, subject=subj, recipient=person.email,
                           sender=config.SENDER_EMAIL, status="sent", external_id=new_id,
                           user_id=user.id if user else None, when=now)
        if not conv.subject:
            conv.subject = subj[:500]

    elif channel == "whatsapp":
        number = people_svc.whatsapp_number(person)
        wa_id = normalize.whatsapp_id(number)
        if not wa_id:
            raise SendBlocked("This person has no phone / WhatsApp number in international format.")
        if not template and not body:
            raise SendBlocked("Write a message first.")
        if not template and not whatsapp_window_open(conv, now):
            raise SendBlocked("More than 24 hours since this person last wrote on WhatsApp: "
                              "send an approved template message instead.")
        shown = body if not template else f"[template {template}] " + " | ".join(map(str, template_params or []))
        try:
            if template:
                wamid = whatsapp.send_template(wa_id, template, template_language, template_params)
            else:
                wamid = whatsapp.send_text(wa_id, body)
        except whatsapp.WhatsAppError as e:
            add_message(session, conv, direction="out", body=shown, recipient=number, status="failed",
                        error=str(e)[:1000], user_id=user.id if user else None, when=now,
                        meta={"template": template} if template else None)
            raise
        m, _ = add_message(session, conv, direction="out", body=shown, recipient=number, status="sent",
                           external_id=wamid, user_id=user.id if user else None, when=now,
                           meta={"template": template, "language": template_language} if template else None)
    else:
        raise SendBlocked(f"Channel '{channel}' is not available yet.")

    mark_read(conv)
    people_svc.touch(person, now, contacted=True)
    audit.record(session, actor, "send", "conversation", conv.id, {"channel": channel, "message_id": m.id,
                                                                    "person_id": person.id})
    return m
