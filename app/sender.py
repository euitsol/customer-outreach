"""Rendering and delivery of a single stage email, plus the bookkeeping that goes with it."""
import secrets
from datetime import datetime, timedelta
from app.clock import utcnow

from app import mailer, merge, settings as app_settings
from app.mailer import SendError
from app.models import Event
from app.services import (
    add_suppression, content_for, get_or_create_message, recompute_contact, steps_of,
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


def render_for(session, contact, step, steps, message, campaign, s):
    """Merged (subject, body) exactly as the recipient would see them."""
    sender_name = campaign.sender_name or s["sender_name"]
    ctx = merge.context_for(contact, sender_name)
    subj_raw, body_raw = content_for(session, contact, step, steps, message)
    return merge.render(subj_raw, ctx).strip(), merge.render(body_raw, ctx).strip()


def send_stage(session, contact, step, *, s, campaign=None, steps=None, message=None, now=None):
    """Send one stage to one contact and record the outcome.

    Returns (outcome, error) where outcome is 'sent' | 'failed' | 'system'.
    'system' means SMTP itself is unavailable - nothing is counted against the email.
    """
    now = now or utcnow()
    campaign = campaign or contact.campaign
    steps = steps if steps is not None else steps_of(session, contact.campaign_id)
    message = message or get_or_create_message(session, contact, step)

    subject, body = render_for(session, contact, step, steps, message, campaign, s)
    if not body:
        _fail(session, contact, message, "No email body: write one for this stage or for this contact.",
              s, now, permanent=True)
        return "failed", message.last_error

    message.open_token = message.open_token or secrets.token_urlsafe(16)
    message.unsub_token = message.unsub_token or secrets.token_urlsafe(16)
    plain, html_doc = mailer.prepare_bodies(
        session, body, contact.id, step.position, message.open_token, message.unsub_token,
        tracking_opts(campaign, s))
    reply_to = campaign.reply_to or s["reply_to"] or None
    in_reply_to = contact.message_id if step.position > 1 else None
    msg, new_id = mailer.build_message(
        to_email=contact.email, to_name=contact.full_name, subject=subject, plain=plain,
        html_doc=html_doc, sender_name=campaign.sender_name or s["sender_name"],
        reply_to=reply_to, in_reply_to=in_reply_to, unsub_token=message.unsub_token)

    try:
        mailer.deliver(msg, contact.email)
    except SendError as e:
        if e.kind == "system":
            message.last_error = str(e)
            return "system", str(e)
        if e.kind == "recipient":
            contact.bounced = True
            session.add(Event(contact_id=contact.id, stage=step.position, type="bounced", detail=str(e)[:500]))
            add_suppression(session, contact.email, "Address refused by mail server")
            _fail(session, contact, message, str(e), s, now, permanent=True)
        else:
            _fail(session, contact, message, str(e), s, now)
        return "failed", str(e)

    message.status = "sent"
    message.queued = False
    message.sent_at = now
    message.last_error = None
    message.attempts = (message.attempts or 0) + 1
    message.next_attempt_at = None
    message.message_id = new_id
    message.sent_subject, message.sent_body = subject, body
    if step.position == 1 or not contact.message_id:
        contact.message_id = new_id
    if step.position > (contact.stage or 0):
        contact.stage = step.position
    contact.last_step_at = now
    session.add(Event(contact_id=contact.id, stage=step.position, type="sent", detail=subject))
    recompute_contact(session, contact, steps, campaign, now)
    return "sent", None


def _fail(session, contact, message, error, s, now, permanent=False):
    message.attempts = (message.attempts or 0) + 1
    message.status = "failed"
    message.queued = False
    message.last_error = error[:1000]
    if permanent:
        message.attempts = max(message.attempts, s["retry_max"])
        message.next_attempt_at = None
    else:
        message.next_attempt_at = now + timedelta(minutes=s["retry_backoff"] * message.attempts)
    session.add(Event(contact_id=contact.id, stage=message.position, type="failed", detail=error[:500]))


def send_test(session, step, steps, campaign, contact, to_email, s):
    """Send a rendered preview to an arbitrary address. Touches no delivery state."""
    message = None
    subject, body = render_for(session, contact, step, steps, message, campaign, s)
    opts = tracking_opts(campaign, s)
    opts.update(track_opens=False, track_clicks=False)
    plain, html_doc = mailer.prepare_bodies(session, body, contact.id, step.position, "test", "test", opts)
    msg, _ = mailer.build_message(
        to_email=to_email, to_name="", subject=f"[TEST] {subject}", plain=plain, html_doc=html_doc,
        sender_name=campaign.sender_name or s["sender_name"], reply_to=campaign.reply_to or s["reply_to"] or None)
    mailer.deliver(msg, to_email)
