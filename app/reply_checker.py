import time
import email
import imaplib
from email.header import decode_header

from app.db import SessionLocal, init_db
from app.models import Contact, Event
from app.config import IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASSWORD, CHECK_INTERVAL_SECONDS

def extract_sender_email(msg):
    from_header = msg.get("From", "")
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip().lower()
    return from_header.strip().lower()

def check_replies(session):
    if not IMAP_HOST:
        return
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(IMAP_USER, IMAP_PASSWORD)
    imap.select("INBOX")

    status, data = imap.search(None, "UNSEEN")
    if status != "OK":
        imap.logout()
        return

    for num in data[0].split():
        status, msg_data = imap.fetch(num, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        sender = extract_sender_email(msg)

        contact = session.query(Contact).filter(Contact.email.ilike(sender)).first()
        if contact and not contact.replied:
            contact.replied = True
            session.add(Event(contact_id=contact.id, stage=contact.stage, type="replied"))
            session.commit()
            print(f"Marked {sender} as replied — sequence stopped.")

    imap.logout()

def main():
    init_db()
    while True:
        session = SessionLocal()
        try:
            check_replies(session)
        except Exception as e:
            print("reply_checker error:", e)
        finally:
            session.close()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()