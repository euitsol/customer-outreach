import re
import smtplib
import ssl
import uuid
import secrets
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

from app.config import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
    SENDER_NAME, SENDER_EMAIL, TRACK_BASE_URL
)
from app.models import Event, ClickLink

URL_RE = re.compile(r'(https?://[^\s"<]+)')

def wrap_links(body_text, contact_id, stage, session):
    """Replace raw URLs with tracked redirect links."""
    def repl(match):
        url = match.group(1)
        token = secrets.token_urlsafe(16)
        session.add(ClickLink(token=token, contact_id=contact_id, stage=stage, target_url=url))
        return f"{TRACK_BASE_URL}/t/c/{token}"
    return URL_RE.sub(repl, body_text)

def build_html(body_text, open_token, unsubscribe_token):
    html_body = body_text.replace("\n", "<br>")
    pixel = f'<img src="{TRACK_BASE_URL}/t/o/{open_token}.gif" width="1" height="1" style="display:none">'
    footer = (
        f'<p style="font-size:11px;color:#888;margin-top:20px">'
        f'Don\'t want further emails from us? '
        f'<a href="{TRACK_BASE_URL}/t/u/{unsubscribe_token}">Unsubscribe here</a>.</p>'
    )
    return f"{html_body}{pixel}{footer}"

def send_stage_email(session, contact, stage, subject, body_text):
    open_token = secrets.token_urlsafe(16)
    unsubscribe_token = secrets.token_urlsafe(16)

    tracked_body = wrap_links(body_text, contact.id, stage, session)
    html = build_html(tracked_body, open_token, unsubscribe_token)

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = contact.email
    msg["Subject"] = subject

    new_msgid = make_msgid()
    msg["Message-ID"] = new_msgid

    if stage > 1 and contact.message_id:
        msg["In-Reply-To"] = contact.message_id
        msg["References"] = contact.message_id

    msg.attach(MIMEText(tracked_body, "plain"))
    msg.attach(MIMEText(html, "html"))

    # store open_token/unsubscribe_token on the event row's detail, so /t/o and /t/u can look up the contact
    session.add(Event(contact_id=contact.id, stage=stage, type="open_token", detail=open_token))
    session.add(Event(contact_id=contact.id, stage=stage, type="unsub_token", detail=unsubscribe_token))

    context = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SENDER_EMAIL, contact.email, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls(context=context)
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SENDER_EMAIL, contact.email, msg.as_string())
        session.add(Event(contact_id=contact.id, stage=stage, type="sent", detail=subject))
        return True, new_msgid
    except Exception as e:
        session.add(Event(contact_id=contact.id, stage=stage, type="failed", detail=str(e)))
        return False, None