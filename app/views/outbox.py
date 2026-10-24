import csv
import io
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, render_template, request
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload

from app import settings as app_settings
from app.clock import utcnow
from app.models import Campaign, CampaignStep, Company, Enrollment, Event, Message, Person
from app.sender import render_for
from app.services import email_consent_ok, not_suppressed, sendable, skip_stage, steps_of
from app.views.common import back, db, form_ids, get_or_404, int_arg, ok, paginate, settings_now, warn

bp = Blueprint("outbox", __name__)

EVENT_TYPES = [("sent", "Sent"), ("failed", "Failed"), ("opened", "Opened"), ("clicked", "Clicked"),
               ("replied", "Replied"), ("bounced", "Bounced"), ("unsubscribed", "Unsubscribed")]


def _outbox_context(campaign=None):
    sess = db()
    view = request.args.get("view", "upcoming")
    if view not in ("upcoming", "queued", "failed"):
        view = "upcoming"
    cid = campaign.id if campaign else int_arg("campaign")
    stage = int_arg("stage")
    term = (request.args.get("q") or "").strip().lower()
    now = utcnow()
    counts = {}

    def with_filters(q):
        if cid:
            q = q.filter(Enrollment.campaign_id == cid)
        if term:
            q = (q.join(Person, Person.id == Enrollment.person_id)
                 .outerjoin(Company, Company.id == Person.company_id)
                 .filter(or_(func.lower(Enrollment.email).like(f"%{term}%"),
                             func.lower(func.coalesce(Company.name, "")).like(f"%{term}%"))))
        return q

    base_msg = sess.query(Message).join(Enrollment, Enrollment.id == Message.enrollment_id)
    counts["queued"] = with_filters(base_msg).filter(Message.queued == True).count()  # noqa: E712
    counts["failed"] = with_filters(base_msg).filter(Message.status == "failed").count()

    def upcoming_query():
        nsteps = (sess.query(func.count(CampaignStep.id)).filter(CampaignStep.campaign_id == Enrollment.campaign_id)
                  .correlate(Enrollment).scalar_subquery())
        q = (sess.query(Enrollment).join(Campaign, Campaign.id == Enrollment.campaign_id)
             .filter(Campaign.status == "active", sendable(), Enrollment.stage < nsteps, not_suppressed(),
                     email_consent_ok()))
        q = with_filters(q)
        if stage:
            q = q.filter(Enrollment.stage == stage - 1)
        return q

    counts["upcoming"] = upcoming_query().count()
    if view == "upcoming":
        q = (upcoming_query().options(selectinload(Enrollment.person).selectinload(Person.company))
             .order_by(Enrollment.next_due_at.asc(), Enrollment.id))
    else:
        q = with_filters(base_msg)
        q = q.filter(Message.queued == True) if view == "queued" else q.filter(Message.status == "failed")  # noqa: E712
        if stage:
            q = q.filter(Message.position == stage)
        q = q.order_by(Message.updated_at.desc(), Message.id.desc())
    page = paginate(q)

    rows = []
    if view == "upcoming":
        cache = {}
        for c in page.items:
            steps = cache.setdefault(c.campaign_id, steps_of(sess, c.campaign_id))
            nxt = next((x for x in steps if x.position == (c.stage or 0) + 1), None)
            rows.append(dict(enrollment=c, step=nxt, due=c.next_due_at, camp=c.campaign))
    else:
        for m in page.items:
            rows.append(dict(msg=m, enrollment=m.enrollment, step=m.step, camp=m.enrollment.campaign))
    return dict(view=view, page=page, rows=rows, counts=counts, now=now, stage=stage, term=term, cid=cid,
                campaigns=sess.query(Campaign).order_by(Campaign.name).all(), s=settings_now(),
                max_attempts=settings_now()["retry_max"])


@bp.route("/outbox")
def index():
    return render_template("outbox.html", scoped=False, **_outbox_context())


@bp.route("/campaigns/<int:cid>/outbox")
def in_campaign(cid):
    camp = get_or_404(Campaign, cid)
    ctx = _outbox_context(camp)
    return render_template("outbox.html", scoped=True, c=camp, steps=steps_of(db(), cid), tab="outbox", **ctx)


@bp.route("/outbox/action", methods=["POST"])
def action():
    sess = db()
    act = request.form.get("action")
    ids = form_ids("ids")
    if not ids:
        warn("Select at least one row.")
        return back("outbox.index")
    msgs = sess.query(Message).filter(Message.id.in_(ids)).all()
    n = 0
    if act == "retry":
        for m in msgs:
            if m.enrollment.bounced or m.enrollment.unsubscribed:
                continue
            m.status, m.attempts, m.next_attempt_at, m.queued = "pending", 0, None, True
            n += 1
        ok(f"{n} email(s) queued for retry.")
    elif act == "cancel":
        for m in msgs:
            m.queued = False
            n += 1
        ok(f"Cancelled {n} queued send(s).")
    elif act == "skip":
        for m in msgs:
            skip_stage(sess, m.enrollment, m.step)
            n += 1
        ok(f"Skipped {n} stage(s).")
    else:
        abort(400)
    sess.commit()
    return back("outbox.index")


@bp.route("/messages/<int:mid>")
def message_view(mid):
    sess = db()
    m = get_or_404(Message, mid)
    c = m.enrollment
    steps = steps_of(sess, c.campaign_id)
    if m.sent_body:
        subject, body, snapshot = m.sent_subject, m.sent_body, True
    else:
        subject, body = render_for(sess, c, m.step, steps, m, c.campaign, settings_now())
        snapshot = False
    return render_template("message.html", m=m, c=c, subject=subject, body=body, snapshot=snapshot)


# ------------------------------------------------------------------ activity log

def _activity_query(sess):
    q = (sess.query(Event, Enrollment, Campaign)
         .join(Enrollment, Enrollment.id == Event.enrollment_id).join(Campaign, Campaign.id == Enrollment.campaign_id)
         .options(selectinload(Enrollment.person).selectinload(Person.company))
         .filter(Event.type.in_([t for t, _ in EVENT_TYPES])))
    types = [t for t in request.args.getlist("type") if t in dict(EVENT_TYPES)]
    if types:
        q = q.filter(Event.type.in_(types))
    cid = int_arg("campaign")
    if cid:
        q = q.filter(Enrollment.campaign_id == cid)
    stage = int_arg("stage")
    if stage:
        q = q.filter(Event.stage == stage)
    term = (request.args.get("q") or "").strip().lower()
    if term:
        q = q.filter(func.lower(Enrollment.email).like(f"%{term}%"))
    for key, op in (("from", "ge"), ("to", "lt")):
        v = request.args.get(key)
        try:
            d = datetime.strptime(v, "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        q = q.filter(Event.created_at >= d) if op == "ge" else q.filter(Event.created_at < d + timedelta(days=1))
    return q.order_by(Event.id.desc())


@bp.route("/activity")
def activity():
    sess = db()
    page = paginate(_activity_query(sess), 50)
    return render_template("activity.html", page=page, event_types=EVENT_TYPES,
                           campaigns=sess.query(Campaign).order_by(Campaign.name).all(),
                           selected_types=request.args.getlist("type"), s=settings_now())


@bp.route("/activity/export")
def activity_export():
    sess = db()
    s = settings_now()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "event", "stage", "email", "company", "campaign", "detail"])
    for ev, c, camp in _activity_query(sess).limit(50000):
        w.writerow([app_settings.to_local(ev.created_at, s).strftime("%Y-%m-%d %H:%M:%S"), ev.type, ev.stage,
                    c.email, c.company, camp.name, ev.detail])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=activity.csv"})
