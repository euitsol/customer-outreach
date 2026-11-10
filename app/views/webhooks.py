"""Inbound provider webhooks (public; authenticated by signature)."""
from flask import Blueprint, abort, request

from app import config
from app.channels import whatsapp
from app.crm import conversations
from app.views.common import db

bp = Blueprint("webhooks", __name__)


def _handle(kind):
    form = request.form.to_dict()
    # Twilio signs the public URL; behind the proxy request.url is the internal one.
    url = config.TRACK_BASE_URL + request.path
    if not whatsapp.verify_signature(url, form, request.headers.get("X-Twilio-Signature")):
        print("twilio webhook: bad or missing signature (check TWILIO_AUTH_TOKEN and TRACK_BASE_URL)", flush=True)
        abort(403)
    form["_kind"] = kind
    sess = db()
    conversations.process_whatsapp_webhook(sess, form)
    sess.commit()
    return "", 200


@bp.route("/webhooks/twilio/whatsapp", methods=["POST"])
def whatsapp_receive():
    """Incoming WhatsApp message (the sender's "when a message comes in" URL)."""
    return _handle("message")


@bp.route("/webhooks/twilio/status", methods=["POST"])
def whatsapp_status():
    """Delivery updates for messages we sent (URL is passed with every send)."""
    return _handle("status")
