import random
import time
import traceback
from datetime import datetime

from sqlalchemy import and_, case, exists, func, or_, select

from app import settings as app_settings
from app.db import SessionLocal, init_db
from app.models import Campaign, CampaignStep, Contact, Event, Message
from app.sender import send_stage
from app.services import not_suppressed, steps_of


def status_text(session, text):
    app_settings.put(session, "worker_status", text)
    app_settings.put(session, "worker_heartbeat", datetime.utcnow().isoformat())
    session.commit()


def sent_today(session, s, campaign_id=None):
    q = session.query(func.count(Event.id)).filter(
        Event.type == "sent", Event.created_at >= app_settings.local_midnight_utc(s))
    if campaign_id:
        q = q.join(Contact, Contact.id == Event.contact_id).filter(Contact.campaign_id == campaign_id)
    return q.scalar() or 0


def pick_manual(session):
    """A message the user asked to send right now (send-now / retry / resend)."""
    q = (session.query(Message).join(Contact, Message.contact_id == Contact.id)
         .filter(Message.queued == True,  # noqa: E712
                 Contact.bounced == False, Contact.unsubscribed == False,  # noqa: E712
                 not_suppressed())
         .order_by(Message.id))
    return q.first()


def capped_campaigns(session, s):
    out = []
    for cid, limit in session.query(Campaign.id, Campaign.daily_limit).filter(Campaign.daily_limit.isnot(None)):
        if limit and sent_today(session, s, cid) >= limit:
            out.append(cid)
    return out


def pick_auto(session, s, now):
    """The next contact whose follow-up (or first email) is due in an active campaign."""
    nsteps = (select(func.count(CampaignStep.id))
              .where(CampaignStep.campaign_id == Contact.campaign_id).correlate(Contact).scalar_subquery())
    blocked_msg = exists().where(and_(
        Message.contact_id == Contact.id,
        Message.position == Contact.stage + 1,
        Message.status == "failed",
        or_(Message.attempts >= s["retry_max"], Message.next_attempt_at > now),
    ))
    priority = case(
        (func.lower(Contact.priority) == "high", 0),
        (func.lower(Contact.priority).in_(("medium", "med")), 1),
        else_=2)
    q = (session.query(Contact).join(Campaign, Contact.campaign_id == Campaign.id)
         .filter(Campaign.status == "active",
                 Contact.send_flag == True, Contact.unsubscribed == False,  # noqa: E712
                 Contact.replied == False, Contact.bounced == False,  # noqa: E712
                 Contact.needs_verification == False,  # noqa: E712
                 Contact.stage < nsteps,
                 or_(Contact.next_due_at.is_(None), Contact.next_due_at <= now),
                 or_(Campaign.start_at.is_(None), Campaign.start_at <= now),
                 not_suppressed(), ~blocked_msg))
    capped = capped_campaigns(session, s)
    if capped:
        q = q.filter(Contact.campaign_id.notin_(capped))
    return q.order_by(priority, Contact.next_due_at.asc(), Contact.id).first()


def _delay_for(campaign, s):
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


def run_cycle(session):
    s = app_settings.load(session)
    now = datetime.utcnow()
    if s["sending_paused"]:
        status_text(session, "Paused: sending switched off in Settings")
        return
    allowed, opens_at = app_settings.in_send_window(s, now)
    if not allowed:
        when = app_settings.to_local(opens_at, s).strftime("%a %H:%M") if opens_at else "?"
        status_text(session, f"Waiting for send window (opens {when} {s['timezone']})")
        return

    while True:
        s = app_settings.load(session)
        now = datetime.utcnow()
        if s["sending_paused"]:
            status_text(session, "Paused: sending switched off in Settings")
            return
        if not app_settings.in_send_window(s, now)[0]:
            return
        if s["daily_limit"] - sent_today(session, s) <= 0:
            status_text(session, f"Daily limit reached ({s['daily_limit']}). Resumes after midnight {s['timezone']}")
            return

        message = pick_manual(session)
        contact = step = None
        if message:
            contact, step = message.contact, message.step
        else:
            contact = pick_auto(session, s, now)
            if contact:
                steps = steps_of(session, contact.campaign_id)
                step = next((x for x in steps if x.position == (contact.stage or 0) + 1), None)
        if not contact or not step:
            status_text(session, "Idle: nothing due right now")
            return

        camp = contact.campaign
        steps = steps_of(session, contact.campaign_id)
        outcome, err = send_stage(session, contact, step, s=s, campaign=camp, steps=steps, message=message, now=now)
        session.commit()
        label = f"[{camp.name} / stage {step.position}] {contact.email}"
        if outcome == "sent":
            print(f"sent   {label}", flush=True)
        elif outcome == "failed":
            print(f"FAILED {label}: {err}", flush=True)
        else:
            print(f"SMTP problem, pausing worker 5 min: {err}", flush=True)
            status_text(session, f"SMTP problem: {err}")
            _sleep(300, session)
            return
        status_text(session, f"Last: {outcome} -> {contact.email}")
        _sleep(_delay_for(camp, s), session)


def main():
    init_db()
    print("worker started", flush=True)
    while True:
        session = SessionLocal()
        interval = 60
        try:
            interval = app_settings.load(session)["poll_interval"]
            run_cycle(session)
        except Exception:
            session.rollback()
            print("worker error:", traceback.format_exc(), flush=True)
        finally:
            session.close()
        time.sleep(max(5, interval))


if __name__ == "__main__":
    main()
