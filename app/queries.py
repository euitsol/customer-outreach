"""Reusable SQL fragments, filters and statistics for the panel."""
from datetime import datetime, timedelta
from app.clock import utcnow

from sqlalchemy import and_, case, distinct, exists, func, or_, select

from app.models import (
    Campaign, CampaignStep, Contact, Event, Message, Tag, contact_tags,
)
from app.services import not_suppressed

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
            .where(CampaignStep.campaign_id == Contact.campaign_id).correlate(Contact).scalar_subquery())


def status_expr():
    n = nsteps_expr()
    return case(
        (Contact.bounced == True, "bounced"),  # noqa: E712
        (Contact.unsubscribed == True, "unsubscribed"),  # noqa: E712
        (Contact.replied == True, "replied"),  # noqa: E712
        (Contact.needs_verification == True, "verify"),  # noqa: E712
        (Contact.send_flag == False, "paused"),  # noqa: E712
        (and_(n > 0, Contact.stage >= n), "finished"),
        (Contact.stage >= 1, "in_sequence"),
        else_="ready",
    )


def status_counts(session, campaign_id=None):
    st = status_expr()
    q = session.query(st, func.count(Contact.id))
    if campaign_id:
        q = q.filter(Contact.campaign_id == campaign_id)
    out = {k: 0 for k in STATUS_LABELS}
    for label, n in q.group_by(st):
        out[label] = n
    out["total"] = sum(out.values())
    return out


def contact_status(c, nsteps):
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


# ------------------------------------------------------------------ contact list filters

SORTS = {
    "newest": (Contact.id.desc(),),
    "oldest": (Contact.id.asc(),),
    "email": (func.lower(Contact.email).asc(),),
    "company": (func.lower(func.coalesce(Contact.company, "")).asc(), Contact.id.asc()),
    "name": (func.lower(func.coalesce(Contact.first_name, "")).asc(), Contact.id.asc()),
    "stage": (Contact.stage.desc(), Contact.id.asc()),
    "due": (Contact.next_due_at.asc(), Contact.id.asc()),
    "last_step": (Contact.last_step_at.desc(), Contact.id.desc()),
}


def _ev_exists(event_type):
    return exists().where(and_(Event.contact_id == Contact.id, Event.type == event_type))


def contact_query(session, args, campaign_id=None):
    """Build a Contact query from request args (a dict-like of strings)."""
    q = session.query(Contact)
    cid = campaign_id or _int(args.get("campaign"))
    if cid:
        q = q.filter(Contact.campaign_id == cid)
    term = (args.get("q") or "").strip()
    if term:
        like = f"%{term.lower()}%"
        q = q.filter(or_(
            func.lower(Contact.email).like(like), func.lower(func.coalesce(Contact.company, "")).like(like),
            func.lower(func.coalesce(Contact.first_name, "")).like(like),
            func.lower(func.coalesce(Contact.last_name, "")).like(like),
            func.lower(func.coalesce(Contact.notes, "")).like(like),
            func.lower(func.coalesce(Contact.phone, "")).like(like),
        ))
    tag = (args.get("tag") or "").strip().lower()
    if tag == "__none__":
        q = q.filter(~exists().where(contact_tags.c.contact_id == Contact.id))
    elif tag:
        q = q.filter(exists().where(and_(
            contact_tags.c.contact_id == Contact.id, contact_tags.c.tag_id == Tag.id, Tag.name == tag)))
    status = args.get("status") or ""
    now = utcnow()
    if status in STATUS_LABELS:
        q = q.filter(status_expr() == status)
    elif status == "due":
        q = q.filter(status_expr().in_(("ready", "in_sequence")),
                     or_(Contact.next_due_at.is_(None), Contact.next_due_at <= now))
    elif status == "scheduled":
        q = q.filter(status_expr().in_(("ready", "in_sequence")), Contact.next_due_at > now)
    elif status == "queued":
        q = q.filter(exists().where(and_(Message.contact_id == Contact.id, Message.queued == True)))  # noqa: E712
    elif status == "failed":
        q = q.filter(exists().where(and_(Message.contact_id == Contact.id, Message.status == "failed")))
    elif status == "suppressed":
        q = q.filter(~not_suppressed())
    stage = args.get("stage")
    if stage not in (None, ""):
        q = q.filter(Contact.stage == _int(stage, 0))
    eng = args.get("engagement") or ""
    if eng == "opened":
        q = q.filter(_ev_exists("opened"))
    elif eng == "not_opened":
        q = q.filter(Contact.stage >= 1, ~_ev_exists("opened"))
    elif eng == "clicked":
        q = q.filter(_ev_exists("clicked"))
    elif eng == "no_activity":
        q = q.filter(Contact.stage >= 1, ~_ev_exists("opened"), ~_ev_exists("clicked"), Contact.replied == False)  # noqa: E712
    if args.get("custom") == "1":
        q = q.filter(exists().where(and_(
            Message.contact_id == Contact.id,
            or_(func.coalesce(Message.body, "") != "", func.coalesce(Message.subject, "") != ""))))
    elif args.get("custom") == "0":
        q = q.filter(~exists().where(and_(
            Message.contact_id == Contact.id,
            or_(func.coalesce(Message.body, "") != "", func.coalesce(Message.subject, "") != ""))))
    prio = (args.get("priority") or "").strip().lower()
    if prio:
        q = q.filter(func.lower(func.coalesce(Contact.priority, "")) == prio)
    d_from, d_to = _date(args.get("from")), _date(args.get("to"))
    if d_from:
        q = q.filter(Contact.created_at >= d_from)
    if d_to:
        q = q.filter(Contact.created_at < d_to + timedelta(days=1))
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


# ------------------------------------------------------------------ per-contact metrics

def metrics_for(session, ids):
    """{contact_id: {sent, opens, clicks, last_sent}} for a page of contacts."""
    out = {i: {"sent": 0, "opens": 0, "clicks": 0, "last_sent": None, "failed": 0, "queued": False}
           for i in ids}
    if not ids:
        return out
    for cid, typ, n, last in (session.query(Event.contact_id, Event.type, func.count(Event.id), func.max(Event.created_at))
                              .filter(Event.contact_id.in_(ids), Event.type.in_(("sent", "opened", "clicked")))
                              .group_by(Event.contact_id, Event.type)):
        d = out[cid]
        if typ == "sent":
            d["sent"], d["last_sent"] = n, last
        elif typ == "opened":
            d["opens"] = n
        else:
            d["clicks"] = n
    for cid, status, queued, n in (session.query(Message.contact_id, Message.status, Message.queued, func.count(Message.id))
                                    .filter(Message.contact_id.in_(ids)).group_by(Message.contact_id, Message.status, Message.queued)):
        if status == "failed":
            out[cid]["failed"] += n
        if queued:
            out[cid]["queued"] = True
    return out


# ------------------------------------------------------------------ statistics

def engagement_stats(session, campaign_id=None):
    def distinct_contacts(event_type):
        q = session.query(func.count(distinct(Event.contact_id))).filter(Event.type == event_type)
        if campaign_id:
            q = q.join(Contact, Contact.id == Event.contact_id).filter(Contact.campaign_id == campaign_id)
        return q.scalar() or 0

    def total(event_type):
        q = session.query(func.count(Event.id)).filter(Event.type == event_type)
        if campaign_id:
            q = q.join(Contact, Contact.id == Event.contact_id).filter(Contact.campaign_id == campaign_id)
        return q.scalar() or 0

    contacted_q = session.query(func.count(Contact.id)).filter(Contact.stage >= 1)
    failed_q = session.query(func.count(Message.id)).filter(Message.status == "failed")
    queued_q = session.query(func.count(Message.id)).filter(Message.queued == True)  # noqa: E712
    if campaign_id:
        contacted_q = contacted_q.filter(Contact.campaign_id == campaign_id)
        failed_q = failed_q.join(Contact, Contact.id == Message.contact_id).filter(Contact.campaign_id == campaign_id)
        queued_q = queued_q.join(Contact, Contact.id == Message.contact_id).filter(Contact.campaign_id == campaign_id)
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
    ok = and_(Contact.send_flag == True, Contact.unsubscribed == False, Contact.replied == False,  # noqa: E712
              Contact.bounced == False, Contact.needs_verification == False)  # noqa: E712
    msg_counts = {}
    for step_id, status, n in (session.query(Message.step_id, Message.status, func.count(Message.id))
                               .join(Contact, Contact.id == Message.contact_id)
                               .filter(Contact.campaign_id == campaign.id).group_by(Message.step_id, Message.status)):
        msg_counts[(step_id, status)] = n
    queued = dict(session.query(Message.step_id, func.count(Message.id))
                  .join(Contact, Contact.id == Message.contact_id)
                  .filter(Contact.campaign_id == campaign.id, Message.queued == True)  # noqa: E712
                  .group_by(Message.step_id).all())
    custom = dict(session.query(Message.step_id, func.count(Message.id))
                  .join(Contact, Contact.id == Message.contact_id)
                  .filter(Contact.campaign_id == campaign.id,
                          or_(func.coalesce(Message.body, "") != "", func.coalesce(Message.subject, "") != ""))
                  .group_by(Message.step_id).all())
    ev = {}
    for stage, typ, n in (session.query(Event.stage, Event.type, func.count(distinct(Event.contact_id)))
                          .join(Contact, Contact.id == Event.contact_id)
                          .filter(Contact.campaign_id == campaign.id, Event.type.in_(("opened", "clicked", "replied")))
                          .group_by(Event.stage, Event.type)):
        ev[(stage, typ)] = n
    waiting = dict(session.query(Contact.stage, func.count(Contact.id))
                   .filter(Contact.campaign_id == campaign.id, ok).group_by(Contact.stage).all())
    due = dict(session.query(Contact.stage, func.count(Contact.id))
               .filter(Contact.campaign_id == campaign.id, ok,
                       or_(Contact.next_due_at.is_(None), Contact.next_due_at <= now))
               .group_by(Contact.stage).all())
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
        q = q.join(Contact, Contact.id == Event.contact_id).filter(Contact.campaign_id == campaign_id)
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
    for cid, label, n in (session.query(Contact.campaign_id, st, func.count(Contact.id))
                          .group_by(Contact.campaign_id, st)):
        d = out.setdefault(cid, {k: 0 for k in STATUS_LABELS})
        d[label] = n
    for cid, d in out.items():
        d["total"] = sum(d[k] for k in STATUS_LABELS)
    sent = dict(session.query(Contact.campaign_id, func.count(Event.id))
                .join(Contact, Contact.id == Event.contact_id).filter(Event.type == "sent")
                .group_by(Contact.campaign_id).all())
    opened = dict(session.query(Contact.campaign_id, func.count(distinct(Event.contact_id)))
                  .join(Contact, Contact.id == Event.contact_id).filter(Event.type == "opened")
                  .group_by(Contact.campaign_id).all())
    contacted = dict(session.query(Contact.campaign_id, func.count(Contact.id))
                     .filter(Contact.stage >= 1).group_by(Contact.campaign_id).all())
    failed = dict(session.query(Contact.campaign_id, func.count(Message.id))
                  .join(Contact, Contact.id == Message.contact_id).filter(Message.status == "failed")
                  .group_by(Contact.campaign_id).all())
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
