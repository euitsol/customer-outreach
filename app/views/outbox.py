import csv
import io
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, render_template, request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import selectinload

from app import config, mailboxes, settings as app_settings
from app.clock import utcnow
from app.models import (
    Campaign, CampaignStep, Company, Conversation, ConversationMessage, Enrollment, Event, Mailbox, Message,
    Person,
)
from app.sender import render_for
from app.services import (
    email_consent_ok, not_suppressed, phone_not_suppressed, sendable, skip_stage, steps_of, wa_consent_ok,
)
from app.views.common import Page, back, db, form_ids, get_or_404, int_arg, ok, paginate, settings_now, warn

bp = Blueprint("outbox", __name__)

EVENT_TYPES = [("sent", "Sent"), ("delivered", "Delivered"), ("failed", "Failed"), ("opened", "Opened"), ("clicked", "Clicked"),
               ("replied", "Replied"), ("bounced", "Bounced"), ("unsubscribed", "Unsubscribed")]


def _channel_clause():
    """Campaign filter from ?channel=email|whatsapp (None = both)."""
    ch = request.args.get("channel")
    if ch == "whatsapp":
        return Campaign.channel == "whatsapp"
    if ch == "email":
        return Campaign.channel != "whatsapp"
    return None


def _sending_state(sess, s, now):
    """Why scheduled emails may be waiting right now: limits, mailboxes, send window."""
    from app.worker import sent_today as email_sent_today
    counts = mailboxes.sent_today(sess, s)
    boxes = mailboxes.usable(sess)
    state = dict(sent=email_sent_today(sess, s), limit=s["daily_limit"], reasons=[], hints=[],
                 boxes=[dict(email=m.email, sent=counts.get(m.id, 0), limit=m.daily_limit,
                             full=not mailboxes.has_room(m, counts)) for m in boxes])
    reasons = state["reasons"]
    if s["sending_paused"]:
        reasons.append("Sending is paused (Settings).")
    allowed, opens_at = app_settings.in_send_window(s, now)
    if not allowed:
        when = app_settings.to_local(opens_at, s).strftime("%a %H:%M") if opens_at else "?"
        reasons.append(f"Outside the send window: sending resumes {when} ({s['timezone']}).")
    if state["limit"] and state["sent"] >= state["limit"]:
        reasons.append(f"The daily limit ({state['limit']}) is used up: sending resumes after midnight ({s['timezone']}).")
    if not boxes:
        reasons.append("No active mailbox: add or enable one under Workspace > Mailboxes.")
    full_ids = set(mailboxes.campaigns_without_room(sess, s))
    for c in sess.query(Campaign).filter(Campaign.status == "active", Campaign.channel != "whatsapp"):
        if boxes and c.id in full_ids:
            reasons.append(f"'{c.name}': every mailbox it may use has reached its daily limit.")
        elif not (c.mailbox_ids or []) and len(boxes) > 1:
            state["hints"].append(f"'{c.name}' sends only from the default mailbox. Tick more mailboxes under its "
                                  "Settings > Send from to use their daily limits too.")
    return state


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

    chan = _channel_clause()

    def with_filters(q):
        if cid:
            q = q.filter(Enrollment.campaign_id == cid)
        if chan is not None:
            q = q.filter(Enrollment.campaign_id.in_(sess.query(Campaign.id).filter(chan)))
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
             .filter(Campaign.status == "active", sendable(), Enrollment.stage < nsteps,
                     or_(and_(Campaign.channel != "whatsapp", not_suppressed(), email_consent_ok()),
                         and_(Campaign.channel == "whatsapp", phone_not_suppressed(), wa_consent_ok()))))
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
        cache, auto = {}, {}
        names = {m.id: m.email for m in sess.query(Mailbox)}
        ids = [c.id for c in page.items]
        chosen = {}
        for eid, pos, mb in (sess.query(Message.enrollment_id, Message.position, Message.mailbox_id)
                             .filter(Message.enrollment_id.in_(ids), Message.mailbox_id.isnot(None))
                             .order_by(Message.position)):
            chosen[(eid, pos)] = mb
        sent_before = dict(sess.query(Message.enrollment_id, func.max(Message.position))
                           .filter(Message.enrollment_id.in_(ids), Message.status == "sent").group_by(Message.enrollment_id))
        wa_from = (config.TWILIO_WHATSAPP_FROM or "").replace("whatsapp:", "") or "Twilio"
        for c in page.items:
            steps = cache.setdefault(c.campaign_id, steps_of(sess, c.campaign_id))
            nxt = next((x for x in steps if x.position == (c.stage or 0) + 1), None)
            pos = nxt.position if nxt else None
            if c.campaign.channel == "whatsapp":
                mbox = (f"WhatsApp {wa_from}", "approved template")
            elif pos and chosen.get((c.id, pos)) and (sent_before.get(c.id) or 0) < pos:
                mbox = (names.get(chosen[(c.id, pos)]), "chosen for this email")
            elif sent_before.get(c.id):
                mbox = (names.get(chosen.get((c.id, sent_before[c.id]))), "same as the earlier email")
            else:
                if c.campaign_id not in auto:
                    auto[c.campaign_id] = ", ".join(m.email for m in mailboxes.eligible(sess, c.campaign)) or "no mailbox"
                mbox = (auto[c.campaign_id], "automatic rotation")
            rows.append(dict(enrollment=c, step=nxt, due=c.next_due_at, camp=c.campaign, mailbox=mbox))
    else:
        for m in page.items:
            rows.append(dict(msg=m, enrollment=m.enrollment, step=m.step, camp=m.enrollment.campaign))
    return dict(view=view, page=page, rows=rows, counts=counts, now=now, stage=stage, term=term, cid=cid,
                channel=request.args.get("channel") or "",
                state=(_sending_state(sess, settings_now(), now)
                       if view == "upcoming" and request.args.get("channel") != "whatsapp" else None),
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
    if c.campaign.channel == "whatsapp":
        thread_msg = (sess.query(ConversationMessage).filter(ConversationMessage.sequence_message_id == m.id)
                      .order_by(ConversationMessage.id.desc()).first())
        return render_template("message.html", m=m, c=c, wa=True, thread_msg=thread_msg, subject=None,
                               body=m.sent_body or "", snapshot=bool(m.sent_body))
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
    chan = _channel_clause()
    if chan is not None:
        q = q.filter(chan)
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


# One-to-one messages carry a status, not events; this maps the Event filter onto those statuses.
_DIRECT_STATUS = {"sent": ("sent", "delivered", "read"), "delivered": ("delivered", "read"),
                  "opened": ("read",), "failed": ("failed",)}


def _direct_query(sess):
    """Messages sent one-to-one (Inbox replies, person page, API, MCP). Campaign sends are Events already.
    None when the current filters cannot match any of them."""
    if request.args.get("source") == "campaign" or int_arg("campaign") or int_arg("stage"):
        return None
    q = (sess.query(ConversationMessage, Conversation, Person)
         .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
         .join(Person, Person.id == Conversation.person_id)
         .filter(ConversationMessage.direction == "out", ConversationMessage.sequence_message_id.is_(None)))
    types = [t for t in request.args.getlist("type") if t in dict(EVENT_TYPES)]
    if types:
        statuses = set()
        for t in types:
            statuses.update(_DIRECT_STATUS.get(t, ()))
        if not statuses:
            return None
        q = q.filter(ConversationMessage.status.in_(statuses))
    ch = request.args.get("channel")
    if ch in ("email", "whatsapp"):
        q = q.filter(ConversationMessage.channel == ch)
    term = (request.args.get("q") or "").strip().lower()
    if term:
        q = q.filter(or_(func.lower(func.coalesce(ConversationMessage.recipient, "")).like(f"%{term}%"),
                         func.lower(func.coalesce(Person.email, "")).like(f"%{term}%"),
                         func.lower(func.coalesce(Person.first_name, "") + " " + func.coalesce(Person.last_name, ""))
                         .like(f"%{term}%")))
    when = func.coalesce(ConversationMessage.sent_at, ConversationMessage.created_at)
    for key, op in (("from", "ge"), ("to", "lt")):
        v = request.args.get(key)
        try:
            d = datetime.strptime(v, "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        q = q.filter(when >= d) if op == "ge" else q.filter(when < d + timedelta(days=1))
    return q.order_by(when.desc(), ConversationMessage.id.desc())


def _event_row(ev, c, camp):
    return dict(when=ev.created_at, kind=ev.type, channel=camp.channel, source="campaign", who=c.email,
                sub=c.company, enrollment_id=c.id, person=None, conversation_id=None, campaign=camp,
                stage=ev.stage, detail=ev.detail or "", bad=ev.type in ("failed", "bounced"))


def _direct_row(m, conv, person):
    failed = m.status == "failed"
    return dict(when=m.sent_at or m.created_at, kind=m.status if m.status in ("sent", "delivered", "read", "failed") else "sent",
                channel=m.channel, source="direct", who=m.recipient or "", sub=m.sender or "", enrollment_id=None,
                person=person, conversation_id=conv.id, campaign=None, stage=None,
                detail=(m.error if failed else (m.subject or m.body or "")) or "", bad=failed)


def _merged_rows(sess, need):
    """(rows newest first, total) over campaign events and one-to-one messages; `need` rows from each source."""
    evq = None if request.args.get("source") == "direct" else _activity_query(sess)
    dmq = _direct_query(sess)
    total, rows = 0, []
    if evq is not None:
        total += evq.order_by(None).count()
        rows += [_event_row(*t) for t in evq.limit(need).all()]
    if dmq is not None:
        total += dmq.order_by(None).count()
        rows += [_direct_row(*t) for t in dmq.limit(need).all()]
    rows.sort(key=lambda r: r["when"] or datetime.min, reverse=True)
    return rows, total


class _Rows(Page):
    """A Page over already-merged rows (same attributes the pager macro reads)."""

    def __init__(self, items, total, page, per_page):
        self.items, self.total, self.page, self.per_page = items, total, page, per_page
        self.pages = max(1, -(-total // per_page))


@bp.route("/activity")
def activity():
    sess = db()
    per = int_arg("per_page", 50)
    per = per if per in (25, 50, 100, 250) else 50
    page_no = max(1, int_arg("page", 1))
    rows, total = _merged_rows(sess, page_no * per)
    pages = max(1, -(-total // per))
    page_no = min(page_no, pages)
    page = _Rows(rows[(page_no - 1) * per:page_no * per], total, page_no, per)
    return render_template("activity.html", page=page, event_types=EVENT_TYPES,
                           campaigns=sess.query(Campaign).order_by(Campaign.name).all(),
                           selected_types=request.args.getlist("type"), s=settings_now())


@bp.route("/activity/export")
def activity_export():
    sess = db()
    s = settings_now()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "event", "source", "channel", "stage", "recipient", "company or sender", "campaign", "detail"])
    rows, _ = _merged_rows(sess, 50000)
    for r in rows[:50000]:
        who = r["person"].display_name if r["person"] is not None else ""
        w.writerow([app_settings.to_local(r["when"], s).strftime("%Y-%m-%d %H:%M:%S") if r["when"] else "",
                    r["kind"], r["source"], r["channel"], r["stage"] or "", r["who"] or who, r["sub"] or "",
                    r["campaign"].name if r["campaign"] else "", r["detail"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=activity.csv"})
