"""WhatsApp through Twilio (Programmable Messaging API).

Sending:   POST https://api.twilio.com/2010-04-01/Accounts/<SID>/Messages.json  (To=whatsapp:+<number>)
Receiving: Twilio POSTs form data to /webhooks/twilio/whatsapp (set it as "incoming messages" URL on the
           WhatsApp sender); delivery updates go to /webhooks/twilio/status, which we pass per message.
           Every request is signed with the auth token (X-Twilio-Signature).

Free-form text can only be sent within 24 hours of the person's last message to you; outside that
window WhatsApp requires an approved template (a Twilio Content SID, HX...).
"""
import base64
import hashlib
import hmac
import json

import httpx

from app import config
from app.clock import utcnow

API = "https://api.twilio.com/2010-04-01"
SERVICE_WINDOW_HOURS = 24
STATUS_PATH = "/webhooks/twilio/status"


class WhatsAppError(Exception):
    def __init__(self, message, code=None, permanent=False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


def configured():
    return bool(config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN
                and (config.TWILIO_WHATSAPP_FROM or config.TWILIO_MESSAGING_SERVICE_SID))


def _post(data, client=None):
    if not configured():
        raise WhatsAppError("WhatsApp is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / "
                            "TWILIO_WHATSAPP_FROM).")
    if config.TWILIO_MESSAGING_SERVICE_SID:
        data["MessagingServiceSid"] = config.TWILIO_MESSAGING_SERVICE_SID
    else:
        data["From"] = config.TWILIO_WHATSAPP_FROM
    data["StatusCallback"] = config.TRACK_BASE_URL + STATUS_PATH
    url = f"{API}/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    auth = (config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    try:
        if client is not None:
            resp = client.post(url, data=data, auth=auth)
        else:
            with httpx.Client(timeout=20) as c:
                resp = c.post(url, data=data, auth=auth)
    except httpx.HTTPError as e:
        raise WhatsAppError(f"Could not reach Twilio: {e}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or body.get("error_code"):
        code = body.get("code") or body.get("error_code")
        msg = body.get("message") or body.get("error_message") or f"HTTP {resp.status_code}"
        raise WhatsAppError(f"Twilio error {code}: {msg}", code=code,
                            permanent=resp.status_code in (400, 401, 403, 404))
    if not body.get("sid"):
        raise WhatsAppError(f"Unexpected Twilio response: {json.dumps(body)[:300]}")
    return body["sid"]


def _to(wa_id):
    return f"whatsapp:+{wa_id}"


def send_text(to_wa_id, body, client=None):
    return _post({"To": _to(to_wa_id), "Body": body[:1600]}, client=client)


def send_template(to_wa_id, name, language=None, body_params=None, client=None):
    """`name` is the template's Content SID (HX...); `language` is unused (a Content SID is one language)."""
    data = {"To": _to(to_wa_id), "ContentSid": name}
    if body_params:
        data["ContentVariables"] = json.dumps({str(i): str(p) for i, p in enumerate(body_params, 1)})
    return _post(data, client=client)


# ---------------------------------------------------------------------- webhooks

def verify_signature(url, form, header):
    """Twilio signs the full public URL followed by the POST fields sorted by name."""
    if not config.TWILIO_AUTH_TOKEN:
        return False
    signed = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = hmac.new(config.TWILIO_AUTH_TOKEN.encode(), signed.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header or "")


def parse_webhook(payload):
    """Flatten a Twilio form (plus `_kind`: 'message' | 'status') into ('message'|'status', dict) items."""
    p = payload or {}
    sid = p.get("MessageSid") or p.get("SmsMessageSid")
    if not sid:
        return []
    if p.get("_kind") == "status":
        status = (p.get("MessageStatus") or "").lower()
        status = "failed" if status == "undelivered" else status
        err = p.get("ErrorCode")
        return [("status", {"id": sid, "status": status, "timestamp": utcnow(), "recipient": p.get("To"),
                            "error": f"{err}: {p.get('ErrorMessage') or 'delivery failed'}" if err else None})]
    text = p.get("Body") or ""
    if int(p.get("NumMedia") or 0) > 0:
        text = f"[{p.get('MediaContentType0') or 'media'}] {text}".strip()
    elif p.get("Latitude"):
        text = f"[location] {p.get('Latitude')},{p.get('Longitude')}"
    return [("message", {"id": sid, "from": (p.get("From") or "").replace("whatsapp:", ""),
                         "name": p.get("ProfileName"), "timestamp": utcnow(), "text": text,
                         "type": "media" if int(p.get("NumMedia") or 0) > 0 else "text",
                         "context_id": p.get("OriginalRepliedMessageSid"), "raw": dict(p)})]
