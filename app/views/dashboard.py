from datetime import datetime, timedelta
from app.clock import utcnow

from flask import Blueprint, render_template
from sqlalchemy import func

from app import config, mailer, settings as app_settings
from app.models import Campaign, Contact, Event, Message
from app.queries import campaign_summaries, daily_activity, engagement_stats, status_counts
from app.views.common import db, settings_now

bp = Blueprint("dashboard", __name__)


def _age(value):
    if not value:
        return None
    try:
        return (utcnow() - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return None


def service_health(s):
    sess = db()
    worker_age = _age(app_settings.get_raw(sess, "worker_heartbeat"))
    reply_age = _age(app_settings.get_raw(sess, "reply_heartbeat"))
    return {
        "smtp_configured": mailer.smtp_configured(),
        "imap_configured": bool(config.IMAP_HOST and config.IMAP_USER),
        "worker_alive": worker_age is not None and worker_age < max(300, s["poll_interval"] * 3 + 600),
        "worker_status": app_settings.get_raw(sess, "worker_status", "No heartbeat yet"),
        "reply_alive": reply_age is not None and reply_age < max(600, s["poll_interval"] * 3 + 300),
        "reply_status": app_settings.get_raw(sess, "reply_status", "No heartbeat yet"),
        "paused": s["sending_paused"],
    }


@bp.route("/")
def index():
    sess = db()
    s = settings_now()
    sent_today = sess.query(func.count(Event.id)).filter(
        Event.type == "sent", Event.created_at >= app_settings.local_midnight_utc(s)).scalar() or 0
    counts = status_counts(sess)
    eng = engagement_stats(sess)
    activity = daily_activity(sess, 14)
    peak = max([max(d["sent"], d["opened"], d["clicked"], d["replied"]) for d in activity] + [1])
    summaries = campaign_summaries(sess)
    campaigns = sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.id.desc()).all()
    now = utcnow()
    upcoming = (sess.query(Contact).join(Campaign, Campaign.id == Contact.campaign_id)
                .filter(Campaign.status == "active", Contact.send_flag == True,  # noqa: E712
                        Contact.unsubscribed == False, Contact.replied == False,  # noqa: E712
                        Contact.bounced == False, Contact.needs_verification == False)  # noqa: E712
                .order_by(Contact.next_due_at.asc()).limit(8).all())
    recent = (sess.query(Event, Contact).join(Contact, Contact.id == Event.contact_id)
              .filter(Event.type.in_(("sent", "failed", "opened", "clicked", "replied", "bounced", "unsubscribed")))
              .order_by(Event.id.desc()).limit(12).all())
    failed = sess.query(func.count(Message.id)).filter(Message.status == "failed").scalar() or 0
    return render_template(
        "dashboard.html", s=s, sent_today=sent_today, counts=counts, eng=eng, activity=activity, peak=peak,
        campaigns=campaigns, summaries=summaries, upcoming=upcoming, recent=recent, failed=failed,
        health=service_health(s), now=now)
