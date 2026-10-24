"""Reusable SQL fragments, filters and statistics for the panel (campaign side)."""
from datetime import datetime, timedelta

from sqlalchemy import and_, case, distinct, exists, func, or_, select
from sqlalchemy.orm import selectinload

from app.clock import utcnow
from app.models import (
    CampaignStep, Company, Enrollment, Event, Message, Person, Tag, person_tags,
)
from app.services import not_suppressed, sendable

STATUS_LABELS = {
    "ready": "Ready",
    "in_sequence": "In sequence",
    "finished": "Finished",
    "replied": "Replied",
    "unsubscribed": "Unsubscribed",
    "bounced": "Bounced",
    "verify": "Needs verification",
    "paused": "Paused",
}


def nsteps_expr():
    return (select(func.count(CampaignStep.id))
            .where(CampaignStep.campaign_id == Enrollment.campaign_id).correlate(Enrollment).scalar_subquery())


def status_expr():
    n = nsteps_expr()
    return case(
        (Enrollment.bounced == True, "bounced"),  # noqa: E712
        (Enrollment.unsubscribed == True, "unsubscribed"),  # noqa: E712
        (Enrollment.replied == True, "replied"),  # noqa: E712
        (Enrollment.needs_verification == True, "verify"),  # noqa: E712
        (Enrollment.send_flag == False, "paused"),  # noqa: E712
        (and_(n > 0, Enrollment.stage >= n), "finished"),
        (Enrollment.stage >= 1, "in_sequence"),
        else_="ready",
    )


def status_counts(session, campaign_id=None):
    st = status_expr()
    q = session.query(st, func.count(Enrollment.id))
    if campaign_id:
        q = q.filter(Enrollment.campaign_id == campaign_id)
    out = {k: 0 for k in STATUS_LABELS}
    for label, n in q.group_by(st):
        out[label] = n
    out["total"] = sum(out.values())
    return out


def enrollment_status(c, nsteps):
    if c.bounced:
        return "bounced"
    if c.unsubscribed:
        return "unsubscribed"
    if c.replied:
        return "replied"
    if c.needs_verification:
        return "verify"
    if c.send_flag is False:
        return "paused"
    if nsteps and (c.stage or 0) >= nsteps:
        return "finished"
    if (c.stage or 0) >= 1:
        return "in_sequence"
    return "ready"


# ------------------------------------------------------------------ enrollment list filters

SORTS = {
    "newest": (Enrollment.id.desc(),),
    "oldest": (Enrollment.id.asc(),),
    "email": (func.lower(Enrollment.email).asc(),),
    "company": (func.lower(func.coalesce(Company.name, "")).asc(), Enrollment.id.asc()),
    "name": (func.lower(func.coalesce(Person.first_name, "")).asc(), Enrollment.id.asc()),
    "stage": (Enrollment.stage.desc(), Enrollment.id.asc()),
    "due": (Enrollment.next_due_at.asc(), Enrollment.id.asc()),
    "last_step": (Enrollment.last_step_at.desc(), Enrollment.id.desc()),
}


def _ev_exists(event_type):
    return exists().where(and_(Event.enrollment_id == Enrollment.id, Event.type == event_type))


def _tag_exists(name=None):
    cond = [person_tags.c.person_id == Enrollment.person_id]
    if name:
        cond += [person_tags.c.tag_id == Tag.id, Tag.name == name]
    return exists().where(and_(*cond))


def enrollment_query(session, args, campaign_id=None, load=True):
    """Build an Enrollment query (joined to person and company) from request args."""
    q = (session.query(Enrollment).join(Person, Person.id == Enrollment.person_id)
         .outerjoin(Company, Company.id == Person.company_id))
    if load:
        q = q.options(selectinload(Enrollment.person).selectinload(Person.company),
                      selectinload(Enrollment.person).selectinload(Person.tags))
    cid = campaign_id or _int(args.get("campaign"))
    if cid:
        q = q.filter(Enrollment.campaign_id == cid)
    pid = _int(args.get("person"))
    if pid:
        q = q.filter(Enrollment.person_id == pid)
    term = (args.get("q") or "").strip()
    if term:
        like = f"%{term.lower()}%"
        q = q.filter(or_(
            func.lower(Enrollment.email).like(like), func.lower(func.coalesce(Company.name, "")).like(like),
            func.lower(func.coalesce(Person.first_name, "")).like(like),
            func.lower(func.coalesce(Person.last_name, "")).like(like),
            func.lower(func.coalesce(Person.job_title, "")).like(like),
            func.lower(func.coalesce(Person.phone, "")).like(like),
        ))
    tag = (args.get("tag") or "").strip().lower()
    if tag == "__none__":
        q = q.filter(~_tag_exists())
    elif tag:
        q = q.filter(_tag_exists(tag))
    status = args.get("status") or ""
    now = utcnow()
    if status in STATUS_LABELS:
        q = q.filter(status_expr() == status)
    elif status == "due":
        q = q.filter(status_expr().in_(("ready", "in_sequence")),
                     or_(Enrollment.next_due_at.is_(None), Enrollment.next_due_at <= now))
    elif status == "scheduled":
        q = q.filter(status_expr().in_(("ready", "in_sequence")), Enrollment.next_due_at > now)
    elif status == "queued":
        q = q.filter(exists().where(and_(Message.enrollment_id == Enrollment.id, Message.queued == True)))  # noqa: E712
    elif status == "failed":
        q = q.filter(exists().where(and_(Message.enrollment_id == Enrollment.id, Message.status == "failed")))
    elif status == "suppressed":
        q = q.filter(~not_suppressed())
    stage = args.get("stage")
    if stage not in (None, ""):
        q = q.filter(Enrollment.stage == _int(stage, 0))
    eng = args.get("engagement") or ""
    if eng == "opened":
        q = q.filter(_ev_exists("opened"))
    elif eng == "not_opened":
        q = q.filter(Enrollment.stage >= 1, ~_ev_exists("opened"))
    elif eng == "clicked":
        q = q.filter(_ev_exists("clicked"))
    elif eng == "no_activity":
        q = q.filter(Enrollment.stage >= 1, ~_ev_exists("opened"), ~_ev_exists("clicked"), Enrollment.replied == False)  # noqa: E712
    if args.get("custom") == "1":
        q = q.filter(exists().where(and_(
            Message.enrollment_id == Enrollment.id,
            or_(func.coalesce(Message.body, "") != "", func.coalesce(Message.subject, "") != ""))))
    elif args.get("custom") == "0":
        q = q.filter(~exists().where(and_(
            Message.enrollment_id == Enrollment.id,
            or_(func.coalesce(Message.body, "") != "", func.coalesce(Message.subject, "") != ""))))
    prio = (args.get("priority") or "").strip().lower()
    if prio:
        q = q.filter(func.lower(func.coalesce(Enrollment.priority, "")) == prio)
    d_from, d_to = _date(args.get("from")), _date(args.get("to"))
    if d_from:
        q = q.filter(Enrollment.created_at >= d_from)
    if d_to:
        q = q.filter(Enrollment.created_at < d_to + timedelta(days=1))
    return q


def sort_query(q, key):
    return q.order_by(*SORTS.get(key, SORTS["newest"]))


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _date(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ per-enrollment metrics

def metrics_for(session, ids):
    """{enrollment_id: {sent, opens, clicks, last_sent}} for a page of enrollments."""
    out = {i: {"sent": 0, "opens": 0, "clicks": 0, "last_sent": None, "failed": 0, "queued": False}
           for i in ids}
    if not ids:
        return out
    for cid, typ, n, last in (session.query(Event.enrollment_id, Event.type, func.count(Event.id), func.max(Event.created_at))
                              .filter(Event.enrollment_id.in_(ids), Event.type.in_(("sent", "opened", "clicked")))
                              .group_by(Event.enrollment_id, Event.type)):
        d = out[cid]
        if typ == "sent":
            d["sent"], d["last_sent"] = n, last
        elif typ == "opened":
            d["opens"] = n
        else:
            d["clicks"] = n
    for cid, status, queued, n in (session.query(Message.enrollment_id, Message.status, Message.queued, func.count(Message.id))
                                    .filter(Message.enrollment_id.in_(ids)).group_by(Message.enrollment_id, Message.status, Message.queued)):
        if status == "failed":
            out[cid]["failed"] += n
        if queued:
            out[cid]["queued"] = True
    return out


# ------------------------------------------------------------------ statistics

def engagement_stats(session, campaign_id=None):
    def distinct_contacts(event_type):
        q = session.query(func.count(distinct(Event.enrollment_id))).filter(Event.type == event_type)
        if campaign_id:
            q = q.join(Enrollment, Enrollment.id == Event.enrollment_id).filter(Enrollment.campaign_id == campaign_id)
        return q.scalar() or 0

    def total(event_type):
        q = session.query(func.count(Event.id)).filter(Event.type == event_type)
        if campaign_id:
            q = q.join(Enrollment, Enrollment.id == Event.enrollment_id).filter(Enrollment.campaign_id == campaign_id)
        return q.scalar() or 0

    contacted_q = session.query(func.count(Enrollment.id)).filter(Enrollment.stage >= 1)
    failed_q = session.query(func.count(Message.id)).filter(Message.status == "failed")
    queued_q = session.query(func.count(Message.id)).filter(Message.queued == True)  # noqa: E712
    if campaign_id:
        contacted_q = contacted_q.filter(Enrollment.campaign_id == campaign_id)
        failed_q = failed_q.join(Enrollment, Enrollment.id == Message.enrollment_id).filter(Enrollment.campaign_id == campaign_id)
        queued_q = queued_q.join(Enrollment, Enrollment.id == Message.enrollment_id).filter(Enrollment.campaign_id == campaign_id)
    contacted = contacted_q.scalar() or 0
    opened = distinct_contacts("opened")
    clicked = distinct_contacts("clicked")
    replied = distinct_contacts("replied")
    return {
        "emails_sent": total("sent"), "contacted": contacted,
        "opened": opened, "clicked": clicked, "replied": replied,
        "total_opens": total("opened"), "failed": failed_q.scalar() or 0, "queued": queued_q.scalar() or 0,
        "open_rate": round(100 * opened / contacted, 1) if contacted else 0,
        "click_rate": round(100 * clicked / contacted, 1) if contacted else 0,
        "reply_rate": round(100 * replied / contacted, 1) if contacted else 0,
    }


def stage_stats(session, campaign, steps):
    """Funnel numbers for every stage of one campaign."""
    now = utcnow()
    ok = sendable()
    msg_counts = {}
    for step_id, status, n in (session.query(Message.step_id, Message.status, func.count(Message.id))
                               .join(Enrollment, Enrollment.id == Message.enrollment_id)
                               .filter(Enrollment.campaign_id == campaign.id).group_by(Message.step_id, Message.status)):
        msg_counts[(step_id, status)] = n
    queued = dict(session.query(Message.step_id, func.count(Message.id))
                  .join(Enrollment, Enrollment.id == Message.enrollment_id)
                  .filter(Enrollment.campaign_id == campaign.id, Message.queued == True)  # noqa: E712
                  .group_by(Message.step_id).all())
    custom = dict(session.query(Message.step_id, func.count(Message.id))
                  .join(Enrollment, Enrollment.id == Message.enrollment_id)
                  .filter(Enrollment.campaign_id == campaign.id,
                          or_(func.coalesce(Message.body, "") != "", func.coalesce(Message.subject, "") != ""))
                  .group_by(Message.step_id).all())
    ev = {}
    for stage, typ, n in (session.query(Event.stage, Event.type, func.count(distinct(Event.enrollment_id)))
                          .join(Enrollment, Enrollment.id == Event.enrollment_id)
                          .filter(Enrollment.campaign_id == campaign.id, Event.type.in_(("opened", "clicked", "replied")))
                          .group_by(Event.stage, Event.type)):
        ev[(stage, typ)] = n
    waiting = dict(session.query(Enrollment.stage, func.count(Enrollment.id))
                   .filter(Enrollment.campaign_id == campaign.id, ok).group_by(Enrollment.stage).all())
    due = dict(session.query(Enrollment.stage, func.count(Enrollment.id))
               .filter(Enrollment.campaign_id == campaign.id, ok,
                       or_(Enrollment.next_due_at.is_(None), Enrollment.next_due_at <= now))
               .group_by(Enrollment.stage).all())
    out = []
    for st in steps:
        out.append({
            "step": st,
            "sent": msg_counts.get((st.id, "sent"), 0),
            "failed": msg_counts.get((st.id, "failed"), 0),
            "skipped": msg_counts.get((st.id, "skipped"), 0),
            "queued": queued.get(st.id, 0),
            "custom": custom.get(st.id, 0),
            "waiting": waiting.get(st.position - 1, 0),
            "due": due.get(st.position - 1, 0),
            "opened": ev.get((st.position, "opened"), 0),
            "clicked": ev.get((st.position, "clicked"), 0),
            "replied": ev.get((st.position, "replied"), 0),
        })
    return out


def daily_activity(session, days=14, campaign_id=None, tzname=None):
    """[(date, sent, opened, clicked, replied)] oldest -> newest, in UTC days."""
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    day = func.date(Event.created_at)
    q = (session.query(day, Event.type, func.count(Event.id))
         .filter(Event.created_at >= since, Event.type.in_(("sent", "opened", "clicked", "replied"))))
    if campaign_id:
        q = q.join(Enrollment, Enrollment.id == Event.enrollment_id).filter(Enrollment.campaign_id == campaign_id)
    data = {}
    for d, typ, n in q.group_by(day, Event.type):
        data[(str(d), typ)] = n
    out = []
    for i in range(days):
        d = (since + timedelta(days=i)).date().isoformat()
        out.append({"date": d, **{t: data.get((d, t), 0) for t in ("sent", "opened", "clicked", "replied")}})
    return out


def campaign_summaries(session):
    """{campaign_id: dict} for the campaign list."""
    out = {}
    st = status_expr()
    for cid, label, n in (session.query(Enrollment.campaign_id, st, func.count(Enrollment.id))
                          .group_by(Enrollment.campaign_id, st)):
        d = out.setdefault(cid, {k: 0 for k in STATUS_LABELS})
        d[label] = n
    for cid, d in out.items():
        d["total"] = sum(d[k] for k in STATUS_LABELS)
    sent = dict(session.query(Enrollment.campaign_id, func.count(Event.id))
                .join(Enrollment, Enrollment.id == Event.enrollment_id).filter(Event.type == "sent")
                .group_by(Enrollment.campaign_id).all())
    opened = dict(session.query(Enrollment.campaign_id, func.count(distinct(Event.enrollment_id)))
                  .join(Enrollment, Enrollment.id == Event.enrollment_id).filter(Event.type == "opened")
                  .group_by(Enrollment.campaign_id).all())
    contacted = dict(session.query(Enrollment.campaign_id, func.count(Enrollment.id))
                     .filter(Enrollment.stage >= 1).group_by(Enrollment.campaign_id).all())
    failed = dict(session.query(Enrollment.campaign_id, func.count(Message.id))
                  .join(Enrollment, Enrollment.id == Message.enrollment_id).filter(Message.status == "failed")
                  .group_by(Enrollment.campaign_id).all())
    nsteps = dict(session.query(CampaignStep.campaign_id, func.count(CampaignStep.id))
                  .group_by(CampaignStep.campaign_id).all())
    for cid in set(out) | set(nsteps):
        d = out.setdefault(cid, {**{k: 0 for k in STATUS_LABELS}, "total": 0})
        d["sent"] = sent.get(cid, 0)
        d["contacted"] = contacted.get(cid, 0)
        d["open_rate"] = round(100 * opened.get(cid, 0) / contacted[cid], 1) if contacted.get(cid) else 0
        d["failed"] = failed.get(cid, 0)
        d["steps"] = nsteps.get(cid, 0)
    return out
