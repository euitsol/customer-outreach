"""Core domain logic shared by the web panel, the worker and the importer."""
import re
from datetime import datetime, timedelta
from app.clock import utcnow

from sqlalchemy import and_, exists, func, literal, or_

from app.models import (
    Campaign, CampaignStep, ClickLink, Contact, Event, Message, Suppression, Tag,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ------------------------------------------------------------------ tags

def parse_tag_names(value):
    parts = re.split(r"[;,]", value) if isinstance(value, str) else list(value or [])
    out = []
    for p in parts:
        name = str(p).strip().lower()[:100]
        if name and name not in out:
            out.append(name)
    return out


def get_or_create_tag(session, name):
    tag = session.query(Tag).filter(Tag.name == name).first()
    if not tag:
        tag = Tag(name=name)
        session.add(tag)
        session.flush()
    return tag


def add_tags(session, contact, value):
    for name in parse_tag_names(value):
        tag = get_or_create_tag(session, name)
        if tag not in contact.tags:
            contact.tags.append(tag)


def remove_tags(session, contact, value):
    names = set(parse_tag_names(value))
    contact.tags = [t for t in contact.tags if t.name not in names]


def set_tags(session, contact, value):
    contact.tags = [get_or_create_tag(session, n) for n in parse_tag_names(value)]


# ------------------------------------------------------------------ suppression

def normalise_suppression(value):
    v = (value or "").strip().lower()
    if not v:
        return None, None
    if "@" in v and not v.startswith("@"):
        return "email", v
    return "domain", v.lstrip("@")


def add_suppression(session, value, reason=""):
    kind, v = normalise_suppression(value)
    if not v:
        return False
    if session.query(Suppression).filter_by(kind=kind, value=v).first():
        return False
    session.add(Suppression(kind=kind, value=v, reason=(reason or "")[:200]))
    return True


def not_suppressed():
    """SQL clause: contact's address is not on the suppression list."""
    return ~exists().where(or_(
        and_(Suppression.kind == "email", Suppression.value == func.lower(Contact.email)),
        and_(Suppression.kind == "domain",
             func.lower(Contact.email).like(literal("%@") + Suppression.value)),
    ))


def is_suppressed(session, email):
    email = (email or "").strip().lower()
    domain = email.split("@")[-1]
    return session.query(Suppression).filter(or_(
        and_(Suppression.kind == "email", Suppression.value == email),
        and_(Suppression.kind == "domain", Suppression.value == domain),
    )).first() is not None


# ------------------------------------------------------------------ steps / content

def steps_of(session, campaign_id):
    return (session.query(CampaignStep).filter(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position).all())


def renumber_steps(session, campaign_id):
    for i, st in enumerate(steps_of(session, campaign_id), start=1):
        st.position = i
        for m in session.query(Message).filter(Message.step_id == st.id):
            m.position = i


def get_message(session, contact_id, step_id):
    return session.query(Message).filter_by(contact_id=contact_id, step_id=step_id).first()


def get_or_create_message(session, contact, step):
    m = get_message(session, contact.id, step.id)
    if not m:
        m = Message(contact_id=contact.id, step_id=step.id, position=step.position, status="pending")
        session.add(m)
        session.flush()
    return m


def thread_subject(session, contact, steps):
    """Subject of the first email, used for 'Re:' follow-ups."""
    if not steps:
        return ""
    first = steps[0]
    m = get_message(session, contact.id, first.id)
    return (m.subject if m and m.subject and m.subject.strip() else first.subject) or ""


def content_for(session, contact, step, steps, message=None):
    """Raw (un-merged) subject and body for a contact at a stage."""
    if message is None:
        message = get_message(session, contact.id, step.id)
    body = message.body if message and message.body and message.body.strip() else step.body
    subj = message.subject if message and message.subject and message.subject.strip() else step.subject
    if not (subj and subj.strip()):
        base = thread_subject(session, contact, steps) if step.position > 1 else ""
        if step.position > 1:
            subj = base if base.lower().startswith("re:") else (f"Re: {base}" if base else "")
        else:
            subj = ""
    return subj, body or ""


def has_custom_content(message):
    return bool(message and ((message.body and message.body.strip()) or (message.subject and message.subject.strip())))


# ------------------------------------------------------------------ scheduling

def recompute_contact(session, contact, steps=None, campaign=None, now=None):
    """Refresh contact.next_due_at from stage / delays / overrides."""
    now = now or utcnow()
    campaign = campaign or contact.campaign
    steps = steps if steps is not None else steps_of(session, contact.campaign_id)
    nxt = next((s for s in steps if s.position == (contact.stage or 0) + 1), None)
    if not nxt:
        contact.next_due_at = None
        return
    msg = get_message(session, contact.id, nxt.id)
    if msg and msg.due_at:
        contact.next_due_at = msg.due_at
        return
    delay = timedelta(hours=nxt.delay_hours or 0)
    if (contact.stage or 0) == 0:
        base = campaign.start_at if campaign and campaign.start_at else None
        if delay:
            base = (base or contact.created_at or now)
            contact.next_due_at = base + delay
        else:
            contact.next_due_at = base
    else:
        contact.next_due_at = (contact.last_step_at or now) + delay


def recompute_campaign(session, campaign):
    steps = steps_of(session, campaign.id)
    for c in session.query(Contact).filter(Contact.campaign_id == campaign.id):
        recompute_contact(session, c, steps, campaign)


def contact_blocker(contact):
    """Why this contact will not receive automatic emails (None = can receive)."""
    if contact.bounced:
        return "bounced"
    if contact.unsubscribed:
        return "unsubscribed"
    if contact.replied:
        return "replied"
    if contact.needs_verification:
        return "needs verification"
    if not contact.send_flag:
        return "paused"
    return None


# ------------------------------------------------------------------ actions on messages

def queue_next(session, contact, steps=None):
    """Mark the contact's next stage to go out at the next worker cycle."""
    if contact.bounced or contact.unsubscribed or contact.replied or contact.needs_verification:
        return False
    steps = steps if steps is not None else steps_of(session, contact.campaign_id)
    nxt = next((s for s in steps if s.position == (contact.stage or 0) + 1), None)
    if not nxt:
        return False
    content = content_for(session, contact, nxt, steps)
    if not content[1].strip():
        return False
    m = get_or_create_message(session, contact, nxt)
    m.queued = True
    m.status = "pending" if m.status != "failed" else "failed"
    contact.send_flag = True
    return True


def queue_stage(session, contact, step):
    """Queue a *specific* stage (re-send of a sent one, or a retry of a failed one)."""
    if contact.bounced or contact.unsubscribed:
        return False
    m = get_or_create_message(session, contact, step)
    m.queued = True
    if m.status in ("sent", "skipped"):
        m.status = "pending"
    m.attempts = 0
    m.next_attempt_at = None
    return True


def cancel_queued(session, contact_ids):
    n = 0
    for m in session.query(Message).filter(Message.contact_id.in_(contact_ids), Message.queued == True):  # noqa: E712
        m.queued = False
        n += 1
    return n


def retry_failed(session, step_id=None, campaign_id=None, contact_ids=None, now=None):
    q = session.query(Message).filter(Message.status == "failed")
    if step_id:
        q = q.filter(Message.step_id == step_id)
    if campaign_id:
        q = q.join(Contact, Message.contact_id == Contact.id).filter(Contact.campaign_id == campaign_id)
    if contact_ids is not None:
        q = q.filter(Message.contact_id.in_(contact_ids))
    n = 0
    for m in q.all():
        if m.contact.bounced or m.contact.unsubscribed:
            continue
        m.status, m.attempts, m.next_attempt_at, m.queued = "pending", 0, None, True
        n += 1
    return n


def skip_stage(session, contact, step, now=None):
    now = now or utcnow()
    m = get_or_create_message(session, contact, step)
    m.status, m.queued = "skipped", False
    if step.position > (contact.stage or 0):
        contact.stage = step.position
        contact.last_step_at = now
    recompute_contact(session, contact, now=now)


def reset_contact(session, contact):
    """Restart the sequence from stage 1 (keeps custom content, forgets delivery state)."""
    for m in contact.messages:
        m.status, m.queued, m.attempts, m.last_error, m.next_attempt_at = "pending", False, 0, None, None
        m.sent_at = m.message_id = m.open_token = m.unsub_token = None
        m.sent_subject = m.sent_body = None
    contact.stage, contact.message_id, contact.replied, contact.bounced = 0, None, False, False
    contact.last_step_at = None
    recompute_contact(session, contact)


def delete_contacts(session, ids):
    ids = list(ids)
    if not ids:
        return 0
    session.query(Message).filter(Message.contact_id.in_(ids)).delete(synchronize_session=False)
    session.query(Event).filter(Event.contact_id.in_(ids)).delete(synchronize_session=False)
    session.query(ClickLink).filter(ClickLink.contact_id.in_(ids)).delete(synchronize_session=False)
    from app.models import contact_tags
    session.execute(contact_tags.delete().where(contact_tags.c.contact_id.in_(ids)))
    n = session.query(Contact).filter(Contact.id.in_(ids)).delete(synchronize_session=False)
    return n


def delete_campaign(session, campaign):
    ids = [i for (i,) in session.query(Contact.id).filter(Contact.campaign_id == campaign.id)]
    for chunk in range(0, len(ids), 500):
        delete_contacts(session, ids[chunk:chunk + 500])
    session.flush()
    session.query(CampaignStep).filter(CampaignStep.campaign_id == campaign.id).delete()
    session.delete(campaign)
    return len(ids)


# ------------------------------------------------------------------ campaigns

def create_default_steps(session, campaign):
    defaults = [("Email 1", 0), ("Follow-up 1", 72), ("Follow-up 2", 120)]
    for i, (name, hours) in enumerate(defaults, start=1):
        session.add(CampaignStep(campaign_id=campaign.id, position=i, name=name, delay_hours=hours))
    session.flush()


def duplicate_campaign(session, campaign, new_name):
    new = Campaign(
        name=new_name, description=campaign.description, status="draft", is_active=False,
        daily_limit=campaign.daily_limit, min_delay=campaign.min_delay, max_delay=campaign.max_delay,
        track_opens=campaign.track_opens, track_clicks=campaign.track_clicks,
        sender_name=campaign.sender_name, reply_to=campaign.reply_to,
    )
    session.add(new)
    session.flush()
    for st in steps_of(session, campaign.id):
        session.add(CampaignStep(campaign_id=new.id, position=st.position, name=st.name,
                                 subject=st.subject, body=st.body, delay_hours=st.delay_hours))
    session.flush()
    return new


def set_campaign_status(campaign, status):
    campaign.status = status
    campaign.is_active = status == "active"
