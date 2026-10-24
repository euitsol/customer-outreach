"""WhatsApp Business Platform (Meta Cloud API).

Sending:   POST https://graph.facebook.com/<version>/<phone_number_id>/messages
Receiving: Meta calls POST /webhooks/whatsapp (configure it in the Meta app dashboard together with
           WHATSAPP_VERIFY_TOKEN); every payload is signed with the app secret (X-Hub-Signature-256).

Free-form text can only be sent within 24 hours of the person's last message to you; outside that
window WhatsApp requires an approved message template (send_template).
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx

from app import config

GRAPH = "https://graph.facebook.com"
SERVICE_WINDOW_HOURS = 24
REENGAGEMENT_ERROR = 131047   # "more than 24 hours have passed since the customer last replied"


class WhatsAppError(Exception):
    def __init__(self, message, code=None, permanent=False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


def configured():
    return bool(config.WHATSAPP_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID)


def _post(payload, phone_number_id=None, client=None):
    if not configured():
        raise WhatsAppError("WhatsApp is not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID).")
    url = f"{GRAPH}/{config.WHATSAPP_API_VERSION}/{phone_number_id or config.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {config.WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try:
        if client is not None:
            resp = client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=20) as c:
                resp = c.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise WhatsAppError(f"Could not reach WhatsApp: {e}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error") or {}
        code = err.get("code")
        msg = err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}"
        raise WhatsAppError(f"WhatsApp error {code}: {msg}", code=code,
                            permanent=resp.status_code in (400, 403, 404))
    try:
        return data["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        raise WhatsAppError(f"Unexpected WhatsApp response: {json.dumps(data)[:300]}")


def send_text(to_wa_id, body, preview_url=True, client=None):
    return _post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_wa_id,
                  "type": "text", "text": {"preview_url": preview_url, "body": body[:4096]}}, client=client)


def send_template(to_wa_id, name, language="en_US", body_params=None, client=None):
    template = {"name": name, "language": {"code": language}}
    if body_params:
        template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": str(p)}
                                                                 for p in body_params]}]
    return _post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_wa_id,
                  "type": "template", "template": template}, client=client)


# ---------------------------------------------------------------------- webhooks

def verify_subscription(args):
    """GET handshake: returns the challenge to echo, or None."""
    if (args.get("hub.mode") == "subscribe" and config.WHATSAPP_VERIFY_TOKEN
            and hmac.compare_digest(args.get("hub.verify_token", ""), config.WHATSAPP_VERIFY_TOKEN)):
        return args.get("hub.challenge", "")
    return None


def verify_signature(raw_body, header):
    if not config.WHATSAPP_APP_SECRET:
        return False
    expected = "sha256=" + hmac.new(config.WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _ts(value):
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _message_text(m):
    kind = m.get("type")
    if kind == "text":
        return (m.get("text") or {}).get("body", "")
    if kind in ("image", "video", "document", "audio", "sticker"):
        media = m.get(kind) or {}
        caption = media.get("caption") or media.get("filename") or ""
        return f"[{kind}] {caption}".strip()
    if kind == "location":
        loc = m.get("location") or {}
        return f"[location] {loc.get('name') or ''} {loc.get('latitude')},{loc.get('longitude')}".strip()
    if kind == "button":
        return (m.get("button") or {}).get("text", "[button]")
    if kind == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title") or "[interactive]"
    if kind == "reaction":
        return f"[reaction] {(m.get('reaction') or {}).get('emoji', '')}"
    if kind == "contacts":
        return "[contact card]"
    return f"[{kind or 'message'}]"


def parse_webhook(payload):
    """Flatten a webhook body into ('message', dict) / ('status', dict) items."""
    out = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            meta = value.get("metadata") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                out.append(("message", {
                    "id": m.get("id"), "from": m.get("from"), "name": names.get(m.get("from")),
                    "timestamp": _ts(m.get("timestamp")), "type": m.get("type"), "text": _message_text(m),
                    "phone_number_id": meta.get("phone_number_id"),
                    "context_id": (m.get("context") or {}).get("id"), "raw": m,
                }))
            for s in value.get("statuses") or []:
                errors = s.get("errors") or []
                out.append(("status", {
                    "id": s.get("id"), "status": s.get("status"), "timestamp": _ts(s.get("timestamp")),
                    "recipient": s.get("recipient_id"),
                    "error": "; ".join(f"{e.get('code')}: {e.get('title') or e.get('message')}" for e in errors) or None,
                }))
    return out
