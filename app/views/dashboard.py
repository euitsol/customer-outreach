from datetime import datetime, timedelta

from flask import Blueprint, render_template
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app import config, mailer, settings as app_settings
from app.channels import whatsapp
from app.clock import utcnow
from app.crm import activities as activities_svc, conversations as conv_svc, deals as deals_svc
from app.models import Campaign, Company, Conversation, Enrollment, Event, Message, Person
from app.queries import campaign_summaries, daily_activity, engagement_stats, status_counts
from app.services import email_consent_ok, sendable
from app.views.common import current_user, db, settings_now

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
        "whatsapp_configured": whatsapp.configured(),
        "worker_alive": worker_age is not None and worker_age < max(300, s["poll_interval"] * 3 + 600),
        "worker_status": app_settings.get_raw(sess, "worker_status", "No heartbeat yet"),
        "reply_alive": reply_age is not None and reply_age < max(600, s["poll_interval"] * 3 + 300),
        "reply_status": app_settings.get_raw(sess, "reply_status", "No heartbeat yet"),
        "paused": s["sending_paused"],
    }


def crm_stats(sess, now):
    week_ago = now - timedelta(days=7)
    deal_sum = deals_svc.summary(sess)
    me = current_user()
    tasks_q = activities_svc.task_query(sess, "today", now=now)
    overdue_q = activities_svc.task_query(sess, "overdue", now=now)
    return {
        "people": sess.query(func.count(Person.id)).scalar() or 0,
        "people_week": sess.query(func.count(Person.id)).filter(Person.created_at >= week_ago).scalar() or 0,
        "companies": sess.query(func.count(Company.id)).scalar() or 0,
        "open_deals": deal_sum["open"][0], "open_value": deal_sum["open"][1], "weighted": deal_sum["weighted"],
        "won_value": deal_sum["won"][1],
        "tasks_today": tasks_q.count(), "tasks_overdue": overdue_q.count(),
        "my_tasks": (tasks_q.filter_by(assignee_id=me.id) if me else tasks_q).limit(6).all(),
        "unread": conv_svc.unread_total(sess),
        "open_conversations": sess.query(func.count(Conversation.id)).filter(Conversation.status == "open").scalar() or 0,
    }


@bp.route("/")
def index():
    sess = db()
    s = settings_now()
    now = utcnow()
    sent_today = sess.query(func.count(Event.id)).filter(
        Event.type == "sent", Event.created_at >= app_settings.local_midnight_utc(s)).scalar() or 0
    counts = status_counts(sess)
    eng = engagement_stats(sess)
    activity = daily_activity(sess, 14)
    peak = max([max(d["sent"], d["opened"], d["clicked"], d["replied"]) for d in activity] + [1])
    summaries = campaign_summaries(sess)
    campaigns = sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.id.desc()).all()
    upcoming = (sess.query(Enrollment).join(Campaign, Campaign.id == Enrollment.campaign_id)
                .filter(Campaign.status == "active", sendable(), email_consent_ok())
                .options(selectinload(Enrollment.person).selectinload(Person.company))
                .order_by(Enrollment.next_due_at.asc()).limit(8).all())
    recent = (sess.query(Event, Enrollment).join(Enrollment, Enrollment.id == Event.enrollment_id)
              .filter(Event.type.in_(("sent", "failed", "opened", "clicked", "replied", "bounced", "unsubscribed")))
              .order_by(Event.id.desc()).limit(12).all())
    inbox = (conv_svc.inbox_query(sess, "unread").options(selectinload(Conversation.person)).limit(6).all())
    failed = sess.query(func.count(Message.id)).filter(Message.status == "failed").scalar() or 0
    return render_template(
        "dashboard.html", s=s, sent_today=sent_today, counts=counts, eng=eng, activity=activity, peak=peak,
        campaigns=campaigns, summaries=summaries, upcoming=upcoming, recent=recent, failed=failed,
        health=service_health(s), now=now, crm=crm_stats(sess, now), inbox=inbox)
