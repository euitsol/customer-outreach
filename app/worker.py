import time
import random
from datetime import date, datetime

from sqlalchemy import or_

from app.db import SessionLocal, init_db
from app.models import Contact, Campaign, Event
from app.mailer import send_stage_email
from app.services import next_step
from app.config import MIN_DELAY_SECONDS, MAX_DELAY_SECONDS, MAX_PER_DAY, CHECK_INTERVAL_SECONDS


def sent_today_count(session):
    start_of_day = datetime.combine(date.today(), datetime.min.time())
    return session.query(Event).filter(
        Event.type == "sent", Event.created_at >= start_of_day
    ).count()

def eligible_contacts(session):
    return (
        session.query(Contact)
        .join(Campaign, Contact.campaign_id == Campaign.id)
        .filter(
            Contact.send_flag == True,  # noqa: E712
            Contact.unsubscribed == False,  # noqa: E712
            Contact.replied == False,  # noqa: E712
            Contact.needs_verification == False,  # noqa: E712
            or_(Contact.queued == True, Campaign.is_active == True),  # noqa: E712
        )
        .order_by(Contact.queued.desc(), Contact.id)
        .all()
    )

def run_once(session):
    remaining = MAX_PER_DAY - sent_today_count(session)
    if remaining <= 0:
        print("Daily send limit reached, waiting for next cycle.")
        return

    for contact in eligible_contacts(session):
        if remaining <= 0:
            break

        # re-read fresh state: the user may have paused/unsubscribed while we were sleeping
        session.refresh(contact)
        camp = contact.campaign
        session.refresh(camp)
        if contact.unsubscribed or contact.replied or contact.needs_verification or not contact.send_flag:
            continue
        if not contact.queued and not camp.is_active:
            continue

        step = next_step(contact, date.today(), force=bool(contact.queued))
        if not step:
            if contact.queued:
                contact.queued = False
                session.commit()
            continue

        stage, subject, body = step
        ok, msgid = send_stage_email(session, contact, stage, subject, body)
        if ok:
            contact.stage = stage
            if stage == 1:
                contact.message_id = msgid
            print(f"[stage {stage}] sent -> {contact.email}")
        else:
            print(f"[stage {stage}] FAILED -> {contact.email}")
        contact.queued = False
        session.commit()
        remaining -= 1
        time.sleep(random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

def main():
    init_db()
    while True:
        session = SessionLocal()
        try:
            run_once(session)
        except Exception as e:
            print("worker error:", e)
        finally:
            session.close()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()