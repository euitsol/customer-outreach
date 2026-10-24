"""Rendering and delivery of a single stage email, plus the bookkeeping that goes with it."""
import secrets
from datetime import timedelta

from app import mailer, merge
from app.clock import utcnow
from app.crm import conversations
from app.mailer import SendError
from app.models import Event
from app.services import (
    add_suppression, content_for, get_or_create_message, recompute_enrollment, steps_of,
)


def tracking_opts(campaign, s):
    return {
        "track_opens": bool(s["track_opens"]) and campaign.track_opens is not False,
        "track_clicks": bool(s["track_clicks"]) and campaign.track_clicks is not False,
        "footer_enabled": s["footer_enabled"],
        "footer_text": s["footer_text"],
        "footer_address": s["footer_address"],
        "signature": s["signature"],
    }


def render_for(session, enrollment, step, steps, message, campaign, s):
    """Merged (subject, body) exactly as the recipient would see them."""
    sender_name = campaign.sender_name or s["sender_name"]
    ctx = merge.context_for(enrollment, sender_name)
    subj_raw, body_raw = content_for(session, enrollment, step, steps, message)
    return merge.render(subj_raw, ctx).strip(), merge.render(body_raw, ctx).strip()


def send_stage(session, enrollment, step, *, s, campaign=None, steps=None, message=None, now=None):
    """Send one stage to one enrollment and record the outcome.

    Returns (outcome, error) where outcome is 'sent' | 'failed' | 'system'.
    'system' means SMTP itself is unavailable - nothing is counted against the email.
    """
    now = now or utcnow()
    campaign = campaign or enrollment.campaign
    steps = steps if steps is not None else steps_of(session, enrollment.campaign_id)
    message = message or get_or_create_message(session, enrollment, step)

    subject, body = render_for(session, enrollment, step, steps, message, campaign, s)
    if not body:
        _fail(session, enrollment, message, "No email body: write one for this stage or for this person.",
              s, now, permanent=True)
        return "failed", message.last_error

    message.open_token = message.open_token or secrets.token_urlsafe(16)
    message.unsub_token = message.unsub_token or secrets.token_urlsafe(16)
    plain, html_doc = mailer.prepare_bodies(
        session, body, enrollment.id, step.position, message.open_token, message.unsub_token,
        tracking_opts(campaign, s))
    reply_to = campaign.reply_to or s["reply_to"] or None
    in_reply_to = enrollment.message_id if step.position > 1 else None
    msg, new_id = mailer.build_message(
        to_email=enrollment.email, to_name=enrollment.full_name, subject=subject, plain=plain,
        html_doc=html_doc, sender_name=campaign.sender_name or s["sender_name"],
        reply_to=reply_to, in_reply_to=in_reply_to, unsub_token=message.unsub_token)

    try:
        mailer.deliver(msg, enrollment.email)
    except SendError as e:
        if e.kind == "system":
            message.last_error = str(e)
            return "system", str(e)
        if e.kind == "recipient":
            mark_bounced(session, enrollment, str(e), "Address refused by mail server")
            _fail(session, enrollment, message, str(e), s, now, permanent=True)
        else:
            _fail(session, enrollment, message, str(e), s, now)
        return "failed", str(e)

    message.status = "sent"
    message.queued = False
    message.sent_at = now
    message.last_error = None
    message.attempts = (message.attempts or 0) + 1
    message.next_attempt_at = None
    message.message_id = new_id
    message.sent_subject, message.sent_body = subject, body
    if step.position == 1 or not enrollment.message_id:
        enrollment.message_id = new_id
    if step.position > (enrollment.stage or 0):
        enrollment.stage = step.position
    enrollment.last_step_at = now
    session.add(Event(enrollment_id=enrollment.id, stage=step.position, type="sent", detail=subject))
    conversations.record_campaign_email(session, enrollment, message, subject, new_id, now)
    recompute_enrollment(session, enrollment, steps, campaign, now)
    return "sent", None


def mark_bounced(session, enrollment, detail, reason):
    """Hard bounce: stop this address everywhere and flag the identity."""
    from app.models import ContactPoint, Enrollment
    from sqlalchemy import func
    for e in session.query(Enrollment).filter(func.lower(Enrollment.email) == enrollment.email.lower(),
                                              Enrollment.bounced == False):  # noqa: E712
        e.bounced = True
        session.add(Event(enrollment_id=e.id, stage=e.stage, type="bounced", detail=(detail or "")[:500]))
    enrollment.bounced = True
    for cp in session.query(ContactPoint).filter(ContactPoint.kind == "email",
                                                 ContactPoint.value == enrollment.email.lower()):
        cp.status = "bounced"
    add_suppression(session, enrollment.email, reason)


def _fail(session, enrollment, message, error, s, now, permanent=False):
    message.attempts = (message.attempts or 0) + 1
    message.status = "failed"
    message.queued = False
    message.last_error = error[:1000]
    if permanent:
        message.attempts = max(message.attempts, s["retry_max"])
        message.next_attempt_at = None
    else:
        message.next_attempt_at = now + timedelta(minutes=s["retry_backoff"] * message.attempts)
    session.add(Event(enrollment_id=enrollment.id, stage=message.position, type="failed", detail=error[:500]))


def send_test(session, step, steps, campaign, enrollment, to_email, s):
    """Send a rendered preview to an arbitrary address. Touches no delivery state."""
    subject, body = render_for(session, enrollment, step, steps, None, campaign, s)
    opts = tracking_opts(campaign, s)
    opts.update(track_opens=False, track_clicks=False)
    plain, html_doc = mailer.prepare_bodies(session, body, enrollment.id, step.position, "test", "test", opts)
    msg, _ = mailer.build_message(
        to_email=to_email, to_name="", subject=f"[TEST] {subject}", plain=plain, html_doc=html_doc,
        sender_name=campaign.sender_name or s["sender_name"], reply_to=campaign.reply_to or s["reply_to"] or None)
    mailer.deliver(msg, to_email)
