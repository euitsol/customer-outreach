"""Outreach domain logic (campaigns, enrollments, sequence messages) shared by the web panel, the
worker, the importer, the REST API and the MCP server."""
from datetime import timedelta

from sqlalchemy import and_, exists, func, literal, or_

from app.clock import utcnow
from app.crm import audit, normalize
from app.models import (
    Campaign, CampaignStep, ClickLink, Consent, ConversationMessage, Enrollment, Event, Message, Person,
    Suppression,
)

EMAIL_RE = normalize.EMAIL_RE


# ------------------------------------------------------------------ suppression

def normalise_suppression(value):
    v = (value or "").strip().lower()
    if not v:
        return None, None
    if "@" in v and not v.startswith("@"):
        return "email", v
    digits = v.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if digits.lstrip("+").isdigit() and len(digits.lstrip("+")) >= 6:
        return "phone", normalize.phone(digits)
    return "domain", v.lstrip("@")


def add_suppression(session, value, reason=""):
    kind, v = normalise_suppression(value)
    if not v:
        return False
    if session.query(Suppression).filter_by(kind=kind, value=v).first():
        return False
    session.add(Suppression(kind=kind, value=v, reason=(reason or "")[:200]))
    session.flush()
    return True


def not_suppressed():
    """SQL clause: the enrollment's address is not on the suppression list."""
    return ~exists().where(or_(
        and_(Suppression.kind == "email", Suppression.value == func.lower(Enrollment.email)),
        and_(Suppression.kind == "domain",
             func.lower(Enrollment.email).like(literal("%@") + Suppression.value)),
    ))


def is_suppressed(session, email):
    email = (email or "").strip().lower()
    domain = email.split("@")[-1]
    return session.query(Suppression).filter(or_(
        and_(Suppression.kind == "email", Suppression.value == email),
        and_(Suppression.kind == "domain", Suppression.value == domain),
    )).first() is not None


def email_consent_ok():
    """SQL clause: the enrolled person may receive campaign email (not do-not-contact, not opted out)."""
    return and_(
        ~exists().where(and_(Person.id == Enrollment.person_id, Person.do_not_contact == True)),  # noqa: E712
        ~exists().where(and_(Consent.person_id == Enrollment.person_id, Consent.channel == "email",
                             Consent.status == "revoked")),
    )


def sendable():
    """SQL clause: enrollment flags that allow automatic sending."""
    return and_(Enrollment.send_flag == True, Enrollment.unsubscribed == False,  # noqa: E712
                Enrollment.replied == False, Enrollment.bounced == False,  # noqa: E712
                Enrollment.needs_verification == False)  # noqa: E712


# ------------------------------------------------------------------ steps / content

def steps_of(session, campaign_id):
    return (session.query(CampaignStep).filter(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position).all())


def renumber_steps(session, campaign_id):
    for i, st in enumerate(steps_of(session, campaign_id), start=1):
        st.position = i
        for m in session.query(Message).filter(Message.step_id == st.id):
            m.position = i


def get_message(session, enrollment_id, step_id):
    return session.query(Message).filter_by(enrollment_id=enrollment_id, step_id=step_id).first()


def get_or_create_message(session, enrollment, step):
    m = get_message(session, enrollment.id, step.id)
    if not m:
        m = Message(enrollment_id=enrollment.id, step_id=step.id, position=step.position, status="pending",
                    attempts=0, queued=False)
        session.add(m)
        session.flush()
    return m


def thread_subject(session, enrollment, steps):
    """Subject of the first email, used for 'Re:' follow-ups."""
    if not steps:
        return ""
    first = steps[0]
    m = get_message(session, enrollment.id, first.id) if enrollment.id else None
    return (m.subject if m and m.subject and m.subject.strip() else first.subject) or ""


def content_for(session, enrollment, step, steps, message=None):
    """Raw (un-merged) subject and body for an enrollment at a stage."""
    if message is None and enrollment.id:
        message = get_message(session, enrollment.id, step.id)
    body = message.body if message and message.body and message.body.strip() else step.body
    subj = message.subject if message and message.subject and message.subject.strip() else step.subject
    if not (subj and subj.strip()):
        base = thread_subject(session, enrollment, steps) if step.position > 1 else ""
        if step.position > 1:
            subj = base if base.lower().startswith("re:") else (f"Re: {base}" if base else "")
        else:
            subj = ""
    return subj, body or ""


def has_custom_content(message):
    return bool(message and ((message.body and message.body.strip()) or (message.subject and message.subject.strip())))


# ------------------------------------------------------------------ enrolling

class EnrollError(ValueError):
    pass


def enroll(session, campaign, person, email=None, variables=None, priority=None, actor=None, steps=None):
    """Put a person into a campaign. Returns (enrollment, created)."""
    existing = session.query(Enrollment).filter_by(campaign_id=campaign.id, person_id=person.id).first()
    if existing:
        if variables:
            existing.custom_fields = {**existing.custom_fields, **variables}
        return existing, False
    address = normalize.email(email) or person.email
    if not address:
        raise EnrollError(f"{person.display_name} has no email address.")
    clash = (session.query(Enrollment)
             .filter(Enrollment.campaign_id == campaign.id, func.lower(Enrollment.email) == address).first())
    if clash:
        raise EnrollError(f"{address} is already in this campaign (as {clash.person.display_name}).")
    e = Enrollment(campaign_id=campaign.id, person_id=person.id, email=address, priority=priority, stage=0,
                   send_flag=True, needs_verification=False, unsubscribed=False, replied=False, bounced=False)
    e.custom_fields = variables or {}
    session.add(e)
    session.flush()
    e.person = person
    e.campaign = campaign
    if actor is not None:
        audit.record(session, actor, "enroll", "person", person.id, {"campaign_id": campaign.id,
                                                                     "enrollment_id": e.id})
    recompute_enrollment(session, e, steps, campaign)
    return e, True


def enroll_people(session, campaign, people, actor=None):
    """Bulk enroll. Returns stats dict."""
    steps = steps_of(session, campaign.id)
    stats = {"enrolled": 0, "already": 0, "no_email": 0, "blocked": 0}
    for p in people:
        if not p.email:
            stats["no_email"] += 1
            continue
        if p.do_not_contact or is_suppressed(session, p.email):
            stats["blocked"] += 1
            continue
        try:
            _, created = enroll(session, campaign, p, steps=steps)
        except EnrollError:
            stats["already"] += 1
            continue
        stats["enrolled" if created else "already"] += 1
    if stats["enrolled"]:
        audit.record(session, actor, "enroll", "campaign", campaign.id, {"enrolled": stats["enrolled"]})
    return stats


# ------------------------------------------------------------------ scheduling

def recompute_enrollment(session, enrollment, steps=None, campaign=None, now=None):
    """Refresh enrollment.next_due_at from stage / delays / overrides."""
    now = now or utcnow()
    campaign = campaign or enrollment.campaign
    steps = steps if steps is not None else steps_of(session, enrollment.campaign_id)
    nxt = next((s for s in steps if s.position == (enrollment.stage or 0) + 1), None)
    if not nxt:
        enrollment.next_due_at = None
        return
    msg = get_message(session, enrollment.id, nxt.id)
    if msg and msg.due_at:
        enrollment.next_due_at = msg.due_at
        return
    delay = timedelta(hours=nxt.delay_hours or 0)
    if (enrollment.stage or 0) == 0:
        base = campaign.start_at if campaign and campaign.start_at else None
        if delay:
            base = (base or enrollment.created_at or now)
            enrollment.next_due_at = base + delay
        else:
            enrollment.next_due_at = base
    else:
        enrollment.next_due_at = (enrollment.last_step_at or now) + delay


def recompute_campaign(session, campaign):
    steps = steps_of(session, campaign.id)
    for e in session.query(Enrollment).filter(Enrollment.campaign_id == campaign.id):
        recompute_enrollment(session, e, steps, campaign)


def enrollment_blocker(enrollment):
    """Why this enrollment will not receive automatic emails (None = can receive)."""
    if enrollment.bounced:
        return "bounced"
    if enrollment.unsubscribed:
        return "unsubscribed"
    if enrollment.replied:
        return "replied"
    if enrollment.needs_verification:
        return "needs verification"
    if not enrollment.send_flag:
        return "paused"
    if enrollment.person and enrollment.person.do_not_contact:
        return "do not contact"
    return None


# ------------------------------------------------------------------ actions on messages

def queue_next(session, enrollment, steps=None):
    """Mark the enrollment's next stage to go out at the next worker cycle."""
    if enrollment.bounced or enrollment.unsubscribed or enrollment.replied or enrollment.needs_verification:
        return False
    steps = steps if steps is not None else steps_of(session, enrollment.campaign_id)
    nxt = next((s for s in steps if s.position == (enrollment.stage or 0) + 1), None)
    if not nxt:
        return False
    content = content_for(session, enrollment, nxt, steps)
    if not content[1].strip():
        return False
    m = get_or_create_message(session, enrollment, nxt)
    m.queued = True
    m.status = "pending" if m.status != "failed" else "failed"
    enrollment.send_flag = True
    return True


def queue_stage(session, enrollment, step):
    """Queue a *specific* stage (re-send of a sent one, or a retry of a failed one)."""
    if enrollment.bounced or enrollment.unsubscribed:
        return False
    m = get_or_create_message(session, enrollment, step)
    m.queued = True
    if m.status in ("sent", "skipped"):
        m.status = "pending"
    m.attempts = 0
    m.next_attempt_at = None
    return True


def cancel_queued(session, enrollment_ids):
    n = 0
    for m in session.query(Message).filter(Message.enrollment_id.in_(enrollment_ids), Message.queued == True):  # noqa: E712
        m.queued = False
        n += 1
    return n


def retry_failed(session, step_id=None, campaign_id=None, enrollment_ids=None, now=None):
    q = session.query(Message).filter(Message.status == "failed")
    if step_id:
        q = q.filter(Message.step_id == step_id)
    if campaign_id:
        q = q.join(Enrollment, Message.enrollment_id == Enrollment.id).filter(Enrollment.campaign_id == campaign_id)
    if enrollment_ids is not None:
        q = q.filter(Message.enrollment_id.in_(enrollment_ids))
    n = 0
    for m in q.all():
        if m.enrollment.bounced or m.enrollment.unsubscribed:
            continue
        m.status, m.attempts, m.next_attempt_at, m.queued = "pending", 0, None, True
        n += 1
    return n


def skip_stage(session, enrollment, step, now=None):
    now = now or utcnow()
    m = get_or_create_message(session, enrollment, step)
    m.status, m.queued = "skipped", False
    if step.position > (enrollment.stage or 0):
        enrollment.stage = step.position
        enrollment.last_step_at = now
    recompute_enrollment(session, enrollment, now=now)


def reset_enrollment(session, enrollment):
    """Restart the sequence from stage 1 (keeps custom content, forgets delivery state)."""
    for m in enrollment.messages:
        m.status, m.queued, m.attempts, m.last_error, m.next_attempt_at = "pending", False, 0, None, None
        m.sent_at = m.message_id = m.open_token = m.unsub_token = None
        m.sent_subject = m.sent_body = None
    enrollment.stage, enrollment.message_id, enrollment.replied, enrollment.bounced = 0, None, False, False
    enrollment.last_step_at = None
    recompute_enrollment(session, enrollment)


def mark_unsubscribed(session, enrollment, source="Unsubscribed via email link", actor=None):
    """Opt the person out of email everywhere: every enrollment on that address, consent, suppression."""
    from app.crm import people as people_svc
    stage = enrollment.stage
    if not enrollment.unsubscribed:
        session.add(Event(enrollment_id=enrollment.id, stage=stage, type="unsubscribed"))
    add_suppression(session, enrollment.email, source)
    others = session.query(Enrollment).filter(or_(func.lower(Enrollment.email) == enrollment.email.lower(),
                                                  Enrollment.person_id == enrollment.person_id))
    ids = []
    for other in others:
        other.unsubscribed = True
        ids.append(other.id)
    cancel_queued(session, ids)
    if enrollment.person:
        people_svc.set_consent(session, enrollment.person, "email", "revoked", source, actor)


def delete_enrollments(session, ids):
    ids = list(ids)
    if not ids:
        return 0
    msg_ids = session.query(Message.id).filter(Message.enrollment_id.in_(ids))
    session.query(ConversationMessage).filter(ConversationMessage.sequence_message_id.in_(msg_ids)).update(
        {ConversationMessage.sequence_message_id: None}, synchronize_session=False)
    session.query(Message).filter(Message.enrollment_id.in_(ids)).delete(synchronize_session=False)
    session.query(Event).filter(Event.enrollment_id.in_(ids)).delete(synchronize_session=False)
    session.query(ClickLink).filter(ClickLink.enrollment_id.in_(ids)).delete(synchronize_session=False)
    n = session.query(Enrollment).filter(Enrollment.id.in_(ids)).delete(synchronize_session=False)
    return n


def delete_campaign(session, campaign):
    ids = [i for (i,) in session.query(Enrollment.id).filter(Enrollment.campaign_id == campaign.id)]
    for chunk in range(0, len(ids), 500):
        delete_enrollments(session, ids[chunk:chunk + 500])
    session.flush()
    session.query(CampaignStep).filter(CampaignStep.campaign_id == campaign.id).delete()
    session.expire(campaign)
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
        name=new_name, description=campaign.description, status="draft", is_active=False, channel=campaign.channel,
        daily_limit=campaign.daily_limit, min_delay=campaign.min_delay, max_delay=campaign.max_delay,
        track_opens=campaign.track_opens, track_clicks=campaign.track_clicks,
        sender_name=campaign.sender_name, reply_to=campaign.reply_to, owner_id=campaign.owner_id,
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
