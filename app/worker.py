import time
import random
from datetime import date, datetime

from app.db import SessionLocal, init_db
from app.models import Contact, Event
from app.mailer import send_stage_email
from app.config import MIN_DELAY_SECONDS, MAX_DELAY_SECONDS, MAX_PER_DAY, CHECK_INTERVAL_SECONDS

def sent_today_count(session):
    start_of_day = datetime.combine(date.today(), datetime.min.time())
    return session.query(Event).filter(
        Event.type == "sent", Event.created_at >= start_of_day
    ).count()

def pick_due_contacts(session):
    today = date.today()
    contacts = session.query(Contact).filter(
        Contact.send_flag == True,  # noqa: E712
        Contact.unsubscribed == False,  # noqa: E712
        Contact.replied == False,  # noqa: E712
        Contact.needs_verification == False,  # noqa: E712
    ).all()

    due = []
    for c in contacts:
        if c.stage == 0 and c.body1:
            due.append((c, 1, c.subject, c.body1))
        elif c.stage == 1 and c.body2 and c.body2_date and c.body2_date <= today:
            subj = c.subject if str(c.subject).lower().startswith("re:") else f"Re: {c.subject}"
            due.append((c, 2, subj, c.body2))
        elif c.stage == 2 and c.body3 and c.body3_date and c.body3_date <= today:
            subj = c.subject if str(c.subject).lower().startswith("re:") else f"Re: {c.subject}"
            due.append((c, 3, subj, c.body3))
    return due

def run_once(session):
    remaining_quota = MAX_PER_DAY - sent_today_count(session)
    if remaining_quota <= 0:
        print("Daily send limit reached, waiting for next cycle.")
        return

    for contact, stage, subject, body in pick_due_contacts(session):
        if remaining_quota <= 0:
            break
        ok, msgid = send_stage_email(session, contact, stage, subject, body)
        if ok:
            contact.stage = stage
            if stage == 1:
                contact.message_id = msgid
            print(f"[stage {stage}] sent -> {contact.email}")
        else:
            print(f"[stage {stage}] FAILED -> {contact.email}")
        session.commit()
        remaining_quota -= 1
        time.sleep(random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

def main():
    init_db()
    while True:
        session = SessionLocal()
        try:
            run_once(session)
        finally:
            session.close()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()