"""Public endpoints hit by recipients: open pixel, click redirect, unsubscribe."""
from datetime import timedelta

from flask import Blueprint, Response, abort, redirect, render_template, request

from app.clock import utcnow
from app.crm import people as people_svc
from app.models import ClickLink, Enrollment, Event, Message
from app.services import mark_unsubscribed
from app.views.common import db

bp = Blueprint("tracking", __name__)

PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


@bp.route("/t/o/<token>.gif")
def track_open(token):
    s = db()
    msg = s.query(Message).filter(Message.open_token == token).first()
    enrollment_id = stage = sent_at = None
    if msg:
        enrollment_id, stage, sent_at = msg.enrollment_id, msg.position, msg.sent_at
    else:  # emails sent before the stages upgrade stored the token on an event
        ev = s.query(Event).filter(Event.type == "open_token", Event.detail == token).first()
        if ev:
            enrollment_id, stage = ev.enrollment_id, ev.stage
    # ignore the instant "open" caused by mail servers scanning the message on arrival
    scanner = sent_at is not None and utcnow() - sent_at < timedelta(seconds=8)
    if enrollment_id and not scanner:
        s.add(Event(enrollment_id=enrollment_id, stage=stage, type="opened"))
        e = s.get(Enrollment, enrollment_id)
        if e and e.person:
            people_svc.touch(e.person)
        s.commit()
    resp = Response(PIXEL_GIF, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/t/c/<token>")
def track_click(token):
    s = db()
    link = s.query(ClickLink).filter(ClickLink.token == token).first()
    if not link or not link.target_url.lower().startswith(("http://", "https://")):
        abort(404)
    s.add(Event(enrollment_id=link.enrollment_id, stage=link.stage, type="clicked", detail=link.target_url[:300]))
    e = s.get(Enrollment, link.enrollment_id)
    if e and e.person:
        people_svc.touch(e.person)
    s.commit()
    return redirect(link.target_url, code=302)


def _enrollment_for_unsub(token):
    s = db()
    msg = s.query(Message).filter(Message.unsub_token == token).first()
    if msg:
        return msg.enrollment
    ev = s.query(Event).filter(Event.type == "unsub_token", Event.detail == token).first()
    if ev:
        return s.get(Enrollment, ev.enrollment_id)
    return None


@bp.route("/t/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    enrollment = _enrollment_for_unsub(token)
    if not enrollment:
        abort(404)
    # GET only asks for confirmation (link scanners fetch GET URLs); POST - from the button or
    # a mail client's one-click List-Unsubscribe - does the actual work.
    if request.method == "GET":
        return render_template("unsubscribe.html", token=token, done=enrollment.unsubscribed)
    s = db()
    mark_unsubscribed(s, enrollment)
    s.commit()
    return render_template("unsubscribe.html", token=token, done=True)
