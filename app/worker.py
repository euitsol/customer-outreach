import random
import threading
import time
import traceback
from datetime import timedelta

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import aliased

from app import config, mailboxes, settings as app_settings
from app import phone_lookup
from app.channels import whatsapp
from app.clock import utcnow
from app.db import SessionLocal, init_db
from app.models import Campaign, CampaignStep, Enrollment, Event, Message
from app.sender import send_stage, send_whatsapp_stage
from app.services import (
    email_consent_ok, not_suppressed, phone_not_suppressed, sendable, steps_of, wa_consent_ok,
)

WA = "whatsapp"


def _of(channel):
    """SQL clause: the campaign sends on this channel (everything that is not WhatsApp is email)."""
    return Campaign.channel == WA if channel == WA else Campaign.channel != WA


def status_text(session, text, channel="email"):
    key = "wa_worker" if channel == WA else "worker"
    app_settings.put(session, f"{key}_status", text)
    app_settings.put(session, f"{key}_heartbeat", utcnow().isoformat())
    session.commit()


def sent_today(session, s, campaign_id=None, channel="email"):
    q = (session.query(func.count(Event.id))
         .join(Enrollment, Enrollment.id == Event.enrollment_id)
         .join(Campaign, Campaign.id == Enrollment.campaign_id)
         .filter(Event.type == "sent", Event.created_at >= app_settings.local_midnight_utc(s)))
    q = q.filter(Enrollment.campaign_id == campaign_id) if campaign_id else q.filter(_of(channel))
    return q.scalar() or 0


def pick_manual(session, channel="email"):
    """A message the user asked to send right now (send-now / retry / resend)."""
    allowed = (phone_not_suppressed(), wa_consent_ok()) if channel == WA else (not_suppressed(), email_consent_ok())
    q = (session.query(Message).join(Enrollment, Message.enrollment_id == Enrollment.id)
         .join(Campaign, Campaign.id == Enrollment.campaign_id)
         .filter(Message.queued == True,  # noqa: E712
                 Enrollment.bounced == False, Enrollment.unsubscribed == False,  # noqa: E712
                 _of(channel), *allowed)
         .order_by(Message.id))
    return q.first()


def capped_campaigns(session, s):
    out = []
    for cid, limit in session.query(Campaign.id, Campaign.daily_limit).filter(Campaign.daily_limit.isnot(None)):
        if limit and sent_today(session, s, cid) >= limit:
            out.append(cid)
    return out


def pick_auto(session, s, now, channel="email"):
    """The next enrollment whose follow-up (or first email) is due in an active campaign."""
    nsteps = (select(func.count(CampaignStep.id))
              .where(CampaignStep.campaign_id == Enrollment.campaign_id).correlate(Enrollment).scalar_subquery())
    blocked_msg = exists().where(and_(
        Message.enrollment_id == Enrollment.id,
        Message.position == Enrollment.stage + 1,
        Message.status == "failed",
        or_(Message.attempts >= s["retry_max"], Message.next_attempt_at > now),
    ))
    priority = case(
        (func.lower(Enrollment.priority) == "high", 0),
        (func.lower(Enrollment.priority).in_(("medium", "med")), 1),
        else_=2)
    q = (session.query(Enrollment).join(Campaign, Enrollment.campaign_id == Campaign.id)
         .filter(Campaign.status == "active", sendable(), _of(channel),
                 Enrollment.stage < nsteps,
                 or_(Enrollment.next_due_at.is_(None), Enrollment.next_due_at <= now),
                 or_(Campaign.start_at.is_(None), Campaign.start_at <= now),
                 ~blocked_msg))
    if channel == WA:
        q = q.filter(phone_not_suppressed(), wa_consent_ok())
        if config.TWILIO_COOLDOWN_DAYS > 0:
            # one WhatsApp thread per person: wait while another WhatsApp campaign messaged them recently
            other_e, other_c = aliased(Enrollment), aliased(Campaign)
            q = q.filter(~exists().where(and_(
                other_e.person_id == Enrollment.person_id, other_e.campaign_id != Enrollment.campaign_id,
                other_c.id == other_e.campaign_id, other_c.channel == WA,
                Message.enrollment_id == other_e.id, Message.status == "sent",
                Message.sent_at >= now - timedelta(days=config.TWILIO_COOLDOWN_DAYS))))
        capped = capped_campaigns(session, s)
    else:
        q = q.filter(not_suppressed(), email_consent_ok())
        full = mailboxes.full_today(session, s)
        if full:
            q = q.filter(~exists().where(and_(Message.enrollment_id == Enrollment.id,
                                              Message.position == Enrollment.stage + 1,
                                              Message.status != "sent", Message.mailbox_id.in_(full))))
        capped = capped_campaigns(session, s) + mailboxes.campaigns_without_room(session, s)
    if capped:
        q = q.filter(Enrollment.campaign_id.notin_(capped))
    return q.order_by(priority, Enrollment.next_due_at.asc(), Enrollment.id).first()


def _delay_for(campaign, s):
    if campaign.channel == WA:            # the API takes messages quickly; a short random gap is enough
        lo = campaign.min_delay if campaign.min_delay is not None else 2
        hi = campaign.max_delay if campaign.max_delay is not None else 6
    else:
        lo = campaign.min_delay if campaign.min_delay is not None else s["min_delay"]
        hi = campaign.max_delay if campaign.max_delay is not None else s["max_delay"]
    return random.randint(max(0, lo), max(lo, hi))


def _sleep(seconds, session):
    """Sleep in short slices so that Pause in the panel takes effect quickly."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(5, max(0, end - time.time())))
        s = app_settings.load(session)
        session.rollback()  # fresh snapshot next time
        if s["sending_paused"]:
            return


def run_cycle(session, channel="email"):
    wa = channel == WA
    s = app_settings.load(session)
    now = utcnow()
    if s["sending_paused"]:
        status_text(session, "Paused: sending switched off in Settings", channel)
        return
    allowed, opens_at = app_settings.in_send_window(s, now)
    if not allowed:
        when = app_settings.to_local(opens_at, s).strftime("%a %H:%M") if opens_at else "?"
        status_text(session, f"Waiting for send window (opens {when} {s['timezone']})", channel)
        return

    while True:
        s = app_settings.load(session)
        now = utcnow()
        if s["sending_paused"]:
            status_text(session, "Paused: sending switched off in Settings", channel)
            return
        if not app_settings.in_send_window(s, now)[0]:
            return
        limit = config.TWILIO_DAILY_LIMIT if wa else s["daily_limit"]
        if limit - sent_today(session, s, channel=channel) <= 0:
            status_text(session, f"Daily limit reached ({limit}). Resumes after midnight {s['timezone']}", channel)
            return

        if wa and not whatsapp.configured():
            status_text(session, "WhatsApp is not configured (TWILIO_* settings)", channel)
            return
        if not wa and not mailboxes.ready(session):
            status_text(session, "No active mailbox: add or enable one under Workspace > Mailboxes")
            return

        message = pick_manual(session, channel)
        enrollment = step = None
        if message:
            enrollment, step = message.enrollment, message.step
        else:
            enrollment = pick_auto(session, s, now, channel)
            if enrollment:
                steps = steps_of(session, enrollment.campaign_id)
                step = next((x for x in steps if x.position == (enrollment.stage or 0) + 1), None)
        if not enrollment or not step:
            status_text(session, "Idle: nothing due right now", channel)
            return

        camp = enrollment.campaign
        steps = steps_of(session, enrollment.campaign_id)
        send = send_whatsapp_stage if wa else send_stage
        outcome, err = send(session, enrollment, step, s=s, campaign=camp, steps=steps, message=message, now=now)
        session.commit()
        label = f"[{camp.name} / stage {step.position}] {enrollment.email}"
        if outcome == "sent":
            print(f"sent   {label}", flush=True)
        elif outcome == "failed":
            print(f"FAILED {label}: {err}", flush=True)
        else:
            what = "WhatsApp (Twilio)" if wa else "SMTP"
            print(f"{what} problem, pausing worker 5 min: {err}", flush=True)
            status_text(session, f"{what} problem: {err}", channel)
            _sleep(300, session)
            return
        status_text(session, f"Last: {outcome} -> {enrollment.email}", channel)
        _sleep(_delay_for(camp, s), session)


def _loop(channel):
    while True:
        session = SessionLocal()
        interval = 60
        try:
            interval = app_settings.load(session)["poll_interval"]
            if channel == WA:
                phone_lookup.run_queue(session)
                phone_lookup.run_daily(session)
            run_cycle(session, channel)
        except Exception:
            session.rollback()
            print(f"worker error ({channel}):", traceback.format_exc(), flush=True)
        finally:
            session.close()
        time.sleep(max(5, interval))


def main():
    init_db()
    print("worker started", flush=True)
    # WhatsApp campaigns have their own loop so a long email run never starves them (and vice versa).
    threading.Thread(target=_loop, args=(WA,), daemon=True, name="whatsapp-worker").start()
    _loop("email")


if __name__ == "__main__":
    main()
