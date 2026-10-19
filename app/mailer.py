import html
import re
import secrets
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from app import config
from app.models import ClickLink

URL_RE = re.compile(r"https?://[^\s<>\"']+")
TRAILING = ".,;:!?)]}"


class SendError(Exception):
    """kind: 'recipient' (address refused, permanent) | 'system' (SMTP down / auth) | 'message' (retryable)"""

    def __init__(self, message, kind="message"):
        super().__init__(message)
        self.kind = kind


def smtp_configured():
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def _split_url(raw):
    url, tail = raw, ""
    while url and url[-1] in TRAILING:
        url, tail = url[:-1], url[-1] + tail
    return url, tail


def _link_token(session, contact_id, stage, url):
    token = secrets.token_urlsafe(16)
    session.add(ClickLink(token=token, contact_id=contact_id, stage=stage, target_url=url))
    return f"{config.TRACK_BASE_URL}/t/c/{token}"


def prepare_bodies(session, body_text, contact_id, stage, open_token, unsub_token, opts):
    """Return (plain, html) versions of the email, with tracking applied per opts."""
    track_clicks = opts.get("track_clicks", True)
    sig = (opts.get("signature") or "").strip()
    text_body = body_text.rstrip()
    if sig:
        text_body += "\n\n" + sig

    plain_parts, html_parts, last = [], [], 0
    for m in URL_RE.finditer(text_body):
        url, tail = _split_url(m.group(0))
        shown = _link_token(session, contact_id, stage, url) if track_clicks else url
        plain_parts.append(text_body[last:m.start()] + shown + tail)
        html_parts.append(html.escape(text_body[last:m.start()]) +
                          f'<a href="{html.escape(shown)}">{html.escape(url)}</a>' + html.escape(tail))
        last = m.end()
    plain_parts.append(text_body[last:])
    html_parts.append(html.escape(text_body[last:]))
    plain = "".join(plain_parts)
    html_body = "".join(html_parts).replace("\r\n", "\n").replace("\n", "<br>\n")

    unsub_url = f"{config.TRACK_BASE_URL}/t/u/{unsub_token}"
    footer_html = footer_plain = ""
    if opts.get("footer_enabled", True):
        label = html.escape(opts.get("footer_text") or "Don't want further emails from us?")
        addr = (opts.get("footer_address") or "").strip()
        footer_html = (
            '<p style="font-size:11px;color:#888;margin-top:24px;line-height:1.5">'
            f'{label} <a href="{unsub_url}" style="color:#888">Unsubscribe here</a>.'
            + (f"<br>{html.escape(addr)}" if addr else "") + "</p>"
        )
        footer_plain = f"\n\n--\n{opts.get('footer_text') or chr(68)+'on' + chr(39) + 't want further emails from us?'} Unsubscribe: {unsub_url}"
        if addr:
            footer_plain += f"\n{addr}"
    pixel = ""
    if opts.get("track_opens", True):
        pixel = (f'<img src="{config.TRACK_BASE_URL}/t/o/{open_token}.gif" width="1" height="1" '
                 'alt="" style="display:none">')
    html_doc = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#222">'
                f"{html_body}</div>{footer_html}{pixel}")
    return plain + footer_plain, html_doc


def build_message(*, to_email, to_name, subject, plain, html_doc, sender_name, reply_to,
                  in_reply_to=None, unsub_token=None):
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((sender_name or "", config.SENDER_EMAIL))
    msg["To"] = formataddr((to_name or "", to_email)) if to_name else to_email
    msg["Subject"] = Header(subject, "utf-8") if subject else ""
    msg["Date"] = formatdate(localtime=False)
    domain = config.SENDER_EMAIL.split("@")[-1] if "@" in config.SENDER_EMAIL else None
    new_id = make_msgid(domain=domain)
    msg["Message-ID"] = new_id
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if unsub_token:
        msg["List-Unsubscribe"] = (f"<{config.TRACK_BASE_URL}/t/u/{unsub_token}>, "
                                   f"<mailto:{config.SENDER_EMAIL}?subject=unsubscribe>")
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_doc, "html", "utf-8"))
    return msg, new_id


def deliver(msg, to_email):
    """Send over SMTP. Raises SendError."""
    if not smtp_configured():
        raise SendError("SMTP is not configured (SMTP_HOST / SMTP_USER / SMTP_PASSWORD)", "system")
    context = ssl.create_default_context()
    try:
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context, timeout=45)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=45)
        with server:
            if config.SMTP_PORT != 465:
                server.starttls(context=context)
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SENDER_EMAIL, [to_email], msg.as_string())
    except smtplib.SMTPRecipientsRefused as e:
        permanent = any(code >= 500 for code, _ in e.recipients.values())
        raise SendError(f"Recipient refused: {e}", "recipient" if permanent else "message")
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(f"SMTP login failed: {e}", "system")
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected) as e:
        raise SendError(f"SMTP connection problem: {e}", "system")
    except smtplib.SMTPSenderRefused as e:
        raise SendError(f"Sender refused: {e}", "system")
    except smtplib.SMTPResponseException as e:
        raise SendError(f"SMTP {e.smtp_code}: {e.smtp_error!r}", "message")
    except smtplib.SMTPException as e:
        raise SendError(str(e), "message")
    except OSError as e:  # DNS failure, refused connection, timeout ...
        raise SendError(f"SMTP connection problem: {e}", "system")
