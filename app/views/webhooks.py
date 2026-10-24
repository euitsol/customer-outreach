"""Inbound provider webhooks (public; authenticated by signature)."""
import json

from flask import Blueprint, Response, abort, jsonify, request

from app.channels import whatsapp
from app.crm import conversations
from app.views.common import db

bp = Blueprint("webhooks", __name__)


@bp.route("/webhooks/whatsapp", methods=["GET"])
def whatsapp_verify():
    """Meta's subscription handshake."""
    challenge = whatsapp.verify_subscription(request.args)
    if challenge is None:
        abort(403)
    return Response(challenge, mimetype="text/plain")


@bp.route("/webhooks/whatsapp", methods=["POST"])
def whatsapp_receive():
    raw = request.get_data()
    if not whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        print("whatsapp webhook: bad or missing signature (is WHATSAPP_APP_SECRET set?)", flush=True)
        abort(403)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        abort(400)
    sess = db()
    counts = conversations.process_whatsapp_webhook(sess, payload)
    sess.commit()
    return jsonify(ok=True, **counts)
