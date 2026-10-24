"""Use-case functions behind the REST API and the MCP server (Claude).

Each takes a session and an Actor, validates its input, does the work through the service layer,
and returns plain JSON-able data. They raise:
    NotFound         - a referenced record does not exist        (HTTP 404)
    ValueError       - bad input / rule violation                (HTTP 400)
    PermissionError  - the key lacks the needed scope            (HTTP 403)
The caller commits.
"""
from datetime import datetime

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import selectinload

from app import mailer, settings as app_settings
from app.channels import whatsapp
from app.clock import utcnow
from app.crm import (
    activities as activities_svc, companies as companies_svc, conversations as conv_svc,
    custom_fields as cf_svc, deals as deals_svc, people as people_svc, serialize as ser,
)
from app.models import (
    Activity, Campaign, Company, Conversation, ConversationMessage, Deal, Enrollment, Person, Pipeline,
    PipelineStage, Tag, Task, person_tags,
)
from app.queries import campaign_summaries, engagement_stats, stage_stats, status_counts
from app.services import EnrollError, enroll, set_campaign_status, steps_of

MAX_LIMIT = 200


class NotFound(LookupError):
    pass


def _get(session, model, id_, label):
    try:
        obj = session.get(model, int(id_)) if id_ not in (None, "") else None
    except (TypeError, ValueError):
        raise ValueError(f"{label} id must be a number, got {id_!r}.")
    if obj is None:
        raise NotFound(f"{label} {id_} not found.")
    return obj


def _limit(limit):
    try:
        return max(1, min(MAX_LIMIT, int(limit or 25)))
    except (TypeError, ValueError):
        return 25


def _dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    v = str(value).strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[:19], fmt)
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a date/time (use ISO 8601, e.g. 2026-11-03T09:30).")


def require(scopes, needed):
    if needed not in (scopes or []):
        raise PermissionError(f"This API key lacks the '{needed}' scope.")


# ---------------------------------------------------------------------- search

def search(session, query, limit=10):
    q = (query or "").strip()
    if not q:
        raise ValueError("Give something to search for.")
    n = _limit(limit)
    people = people_svc.search_query(session, q).options(selectinload(Person.company)).limit(n).all()
    like = f"%{q.lower()}%"
    companies = (session.query(Company).filter(or_(func.lower(Company.name).like(like),
                                                   func.lower(func.coalesce(Company.domain, "")).like(like)))
                 .limit(n).all())
    deals = session.query(Deal).filter(func.lower(Deal.title).like(like)).limit(n).all()
    return {"people": [ser.person(p) for p in people], "companies": [ser.company(c) for c in companies],
            "deals": [ser.deal(d) for d in deals]}


# ---------------------------------------------------------------------- people

def list_people(session, query=None, lifecycle_stage=None, tag=None, company_id=None, campaign_id=None,
                owner_id=None, limit=25, offset=0):
    q = people_svc.search_query(session, query).options(selectinload(Person.company), selectinload(Person.tags))
    if lifecycle_stage:
        q = q.filter(Person.lifecycle_stage == lifecycle_stage)
    if tag:
        q = q.filter(exists().where(and_(person_tags.c.person_id == Person.id, person_tags.c.tag_id == Tag.id,
                                         Tag.name == tag.strip().lower())))
    if company_id:
        q = q.filter(Person.company_id == int(company_id))
    if campaign_id:
        q = q.filter(exists().where(and_(Enrollment.person_id == Person.id,
                                         Enrollment.campaign_id == int(campaign_id))))
    if owner_id:
        q = q.filter(Person.owner_id == int(owner_id))
    total = q.order_by(None).count()
    rows = q.order_by(Person.id.desc()).limit(_limit(limit)).offset(max(0, int(offset or 0))).all()
    return {"total": total, "people": [ser.person(p) for p in rows]}


def get_person(session, person_id=None, email=None, timeline_limit=25):
    p = people_svc.find_by_email(session, email) if email else None
    if p is None:
        p = _get(session, Person, person_id, "Person")
    out = ser.person(p, detail=True)
    out["open_tasks"] = [ser.task(t) for t in activities_svc.task_query(session, "open", person_id=p.id).limit(20)]
    out["deals"] = [ser.deal(d) for d in session.query(Deal).filter(
        or_(Deal.person_id == p.id, Deal.people.any(Person.id == p.id))).limit(20)]
    out["timeline"] = [{"when": i["when"].isoformat(), "kind": i["kind"], "title": i["title"],
                        "body": (i.get("body") or "")[:1000] or None}
                       for i in people_svc.timeline(session, p, _limit(timeline_limit))]
    return out


def create_person(session, actor, data):
    email = (data.get("email") or "").strip() or None
    if not (email or data.get("phone") or data.get("first_name")):
        raise ValueError("Give at least a name, an email or a phone number.")
    if email and people_svc.find_by_email(session, email):
        raise ValueError(f"{email} already belongs to person {people_svc.find_by_email(session, email).id}.")
    attrs = cf_svc.values_from_api(session, "person", data.get("custom_fields") or {})
    person, _ = people_svc.upsert(
        session, email=email, phone=data.get("phone"), first_name=data.get("first_name"),
        last_name=data.get("last_name"), job_title=data.get("job_title"), company_name=data.get("company"),
        company_website=data.get("company_website"), source=data.get("source") or actor.type,
        lifecycle_stage=data.get("lifecycle_stage"), owner_id=data.get("owner_id"), attributes=attrs, actor=actor)
    if data.get("lifecycle_stage") in people_svc.LIFECYCLE_KEYS:
        person.lifecycle_stage = data["lifecycle_stage"]
    if data.get("lead_status"):
        people_svc.update(session, person, {"lead_status": data["lead_status"]}, actor)
    if data.get("tags"):
        people_svc.add_tags(session, person, data["tags"])
    session.flush()
    return ser.person(person, detail=True)


def update_person(session, actor, person_id, data):
    p = _get(session, Person, person_id, "Person")
    payload = {k: data[k] for k in ("first_name", "last_name", "job_title", "email", "phone", "lifecycle_stage",
                                    "lead_status", "source", "owner_id", "do_not_contact", "company_id")
               if k in data}
    if "company" in data:
        payload["company"] = data["company"]
        payload["company_website"] = data.get("company_website")
    if "custom_fields" in data:
        payload["attributes"] = cf_svc.values_from_api(session, "person", data["custom_fields"])
    changes = people_svc.update(session, p, payload, actor)
    if "tags" in data:
        people_svc.set_tags(session, p, data["tags"])
    if data.get("add_tags"):
        people_svc.add_tags(session, p, data["add_tags"])
    if data.get("remove_tags"):
        people_svc.remove_tags(session, p, data["remove_tags"])
    session.flush()
    return {"person": ser.person(p, detail=True), "changed": sorted(changes)}


def delete_person(session, actor, person_id):
    p = _get(session, Person, person_id, "Person")
    people_svc.delete(session, p, actor)
    return {"deleted": int(person_id)}


def merge_people(session, actor, keep_id, other_id):
    keep = _get(session, Person, keep_id, "Person")
    other = _get(session, Person, other_id, "Person")
    people_svc.merge(session, keep, other, actor)
    return ser.person(keep, detail=True)


# ---------------------------------------------------------------------- companies

def list_companies(session, query=None, limit=25, offset=0):
    q = session.query(Company)
    if query:
        like = f"%{query.strip().lower()}%"
        q = q.filter(or_(func.lower(Company.name).like(like), func.lower(func.coalesce(Company.domain, "")).like(like)))
    total = q.count()
    rows = q.order_by(func.lower(Company.name)).limit(_limit(limit)).offset(max(0, int(offset or 0))).all()
    return {"total": total, "companies": [ser.company(c) for c in rows]}


def get_company(session, company_id):
    c = _get(session, Company, company_id, "Company")
    out = ser.company(c, detail=True)
    out["people"] = [ser.person(p) for p in c.people[:100]]
    out["deals"] = [ser.deal(d) for d in session.query(Deal).filter(Deal.company_id == c.id).limit(50)]
    out["recent_activities"] = [ser.activity(a) for a in activities_svc.company_activities(session, c, 20)]
    return out


def create_company(session, actor, data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Company name is required.")
    c, created = companies_svc.get_or_create(session, name, data.get("website"), actor=actor)
    if not created:
        raise ValueError(f"Company already exists (id {c.id}).")
    payload = {k: data[k] for k in companies_svc.FIELDS if k in data and k != "name"}
    if "custom_fields" in data:
        payload["attributes"] = cf_svc.values_from_api(session, "company", data["custom_fields"])
    companies_svc.update(session, c, payload, actor)
    if data.get("tags"):
        people_svc.add_tags(session, c, data["tags"])
    return ser.company(c, detail=True)


def update_company(session, actor, company_id, data):
    c = _get(session, Company, company_id, "Company")
    payload = {k: data[k] for k in companies_svc.FIELDS if k in data}
    if "custom_fields" in data:
        payload["attributes"] = cf_svc.values_from_api(session, "company", data["custom_fields"])
    changes = companies_svc.update(session, c, payload, actor)
    if "tags" in data:
        people_svc.set_tags(session, c, data["tags"])
    return {"company": ser.company(c, detail=True), "changed": sorted(changes)}


# ---------------------------------------------------------------------- deals

def list_pipelines(session):
    return [{"id": p.id, "name": p.name, "default": p.is_default,
             "stages": [{"id": s.id, "name": s.name, "kind": s.kind, "probability": s.probability}
                        for s in p.stages]}
            for p in session.query(Pipeline).order_by(Pipeline.position, Pipeline.id)]


def list_deals(session, status=None, pipeline_id=None, person_id=None, company_id=None, query=None, limit=25,
               offset=0):
    q = session.query(Deal).options(selectinload(Deal.company), selectinload(Deal.person), selectinload(Deal.stage))
    if status:
        q = q.filter(Deal.status == status)
    if pipeline_id:
        q = q.filter(Deal.pipeline_id == int(pipeline_id))
    if person_id:
        q = q.filter(or_(Deal.person_id == int(person_id), Deal.people.any(Person.id == int(person_id))))
    if company_id:
        q = q.filter(Deal.company_id == int(company_id))
    if query:
        q = q.filter(func.lower(Deal.title).like(f"%{query.strip().lower()}%"))
    total = q.order_by(None).count()
    rows = q.order_by(Deal.updated_at.desc().nullslast(), Deal.id.desc()).limit(_limit(limit)) \
        .offset(max(0, int(offset or 0))).all()
    pipe = deals_svc.default_pipeline(session)
    summary = deals_svc.summary(session, pipe) if pipe else None
    return {"total": total, "deals": [ser.deal(d) for d in rows],
            "pipeline_summary": {k: [v[0], float(v[1])] if isinstance(v, tuple) else float(v)
                                 for k, v in summary.items()} if summary else None}


def _stage_from(session, pipeline, data):
    if data.get("stage_id"):
        return _get(session, PipelineStage, data["stage_id"], "Stage")
    if data.get("stage"):
        pipeline = pipeline or deals_svc.default_pipeline(session)
        st = next((s for s in pipeline.stages if s.name.lower() == str(data["stage"]).strip().lower()), None)
        if st is None:
            raise ValueError(f"No stage called '{data['stage']}' in pipeline '{pipeline.name}'. "
                             f"Stages: {', '.join(s.name for s in pipeline.stages)}.")
        return st
    return None


def create_deal(session, actor, data):
    pipeline = _get(session, Pipeline, data["pipeline_id"], "Pipeline") if data.get("pipeline_id") else None
    stage = _stage_from(session, pipeline, data)
    person = _get(session, Person, data["person_id"], "Person") if data.get("person_id") else None
    company_id = data.get("company_id") or (person.company_id if person else None)
    d = deals_svc.create(
        session, title=data.get("title"), pipeline_id=(pipeline.id if pipeline else (stage.pipeline_id if stage else None)),
        stage_id=stage.id if stage else None, company_id=company_id, person_id=person.id if person else None,
        owner_id=data.get("owner_id") or actor.user_id, amount=data.get("amount"), currency=data.get("currency"),
        expected_close_date=_dt(data.get("expected_close_date")).date() if data.get("expected_close_date") else None,
        source=data.get("source"), attributes=cf_svc.values_from_api(session, "deal", data.get("custom_fields")),
        actor=actor)
    return ser.deal(d, detail=True)


def update_deal(session, actor, deal_id, data):
    d = _get(session, Deal, deal_id, "Deal")
    payload = {k: data[k] for k in ("title", "amount", "currency", "source", "lost_reason", "owner_id",
                                    "company_id", "person_id") if k in data}
    if "expected_close_date" in data:
        payload["expected_close_date"] = _dt(data["expected_close_date"]).date() if data["expected_close_date"] else None
    if "custom_fields" in data:
        payload["attributes"] = cf_svc.values_from_api(session, "deal", data["custom_fields"])
    stage = _stage_from(session, d.pipeline, data)
    changes = deals_svc.update(session, d, payload, actor)
    if stage is not None:
        deals_svc.move(session, d, stage, data.get("lost_reason"), actor)
    session.flush()
    return {"deal": ser.deal(d, detail=True), "changed": sorted(changes) + (["stage"] if stage is not None else [])}


# ---------------------------------------------------------------------- notes & tasks

def log_activity(session, actor, kind="note", body=None, subject=None, person_id=None, company_id=None,
                 deal_id=None, occurred_at=None):
    for model, id_, label in ((Person, person_id, "Person"), (Company, company_id, "Company"), (Deal, deal_id, "Deal")):
        if id_:
            _get(session, model, id_, label)
    a = activities_svc.log(session, kind=kind, body=body, subject=subject, person_id=person_id,
                           company_id=company_id, deal_id=deal_id, user_id=actor.user_id,
                           occurred_at=_dt(occurred_at), meta={"via": actor.type}, actor=actor)
    return ser.activity(a)


def list_tasks(session, view="open", person_id=None, company_id=None, deal_id=None, limit=50):
    q = activities_svc.task_query(session, view, person_id=person_id, company_id=company_id, deal_id=deal_id)
    return {"tasks": [ser.task(t) for t in q.limit(_limit(limit))]}


def create_task(session, actor, title, due_at=None, priority="normal", notes=None, person_id=None,
                company_id=None, deal_id=None, assignee_id=None):
    t = activities_svc.create_task(session, title=title, due_at=_dt(due_at), priority=priority, notes=notes,
                                   assignee_id=assignee_id or actor.user_id, person_id=person_id,
                                   company_id=company_id, deal_id=deal_id, created_by_id=actor.user_id, actor=actor)
    return ser.task(t)


def update_task(session, actor, task_id, data):
    t = _get(session, Task, task_id, "Task")
    if data.get("status"):
        activities_svc.set_task_status(session, t, data["status"], actor)
    payload = {k: data[k] for k in ("title", "notes", "priority", "assignee_id") if k in data}
    if "due_at" in data:
        payload["due_at"] = _dt(data["due_at"])
    activities_svc.update_task(session, t, payload, actor)
    return ser.task(t)


# ---------------------------------------------------------------------- campaigns

def list_campaigns(session, include_archived=False):
    q = session.query(Campaign)
    if not include_archived:
        q = q.filter(Campaign.status != "archived")
    summaries = campaign_summaries(session)
    return [ser.campaign(c, summaries.get(c.id)) for c in q.order_by(Campaign.id.desc())]


def get_campaign(session, campaign_id):
    c = _get(session, Campaign, campaign_id, "Campaign")
    steps = steps_of(session, c.id)
    out = ser.campaign(c)
    out["status_counts"] = status_counts(session, c.id)
    out["engagement"] = engagement_stats(session, c.id)
    out["stages"] = [{"position": r["step"].position, "name": r["step"].name, "subject": r["step"].subject,
                      "delay_hours": r["step"].delay_hours, "sent": r["sent"], "failed": r["failed"],
                      "waiting": r["waiting"], "opened": r["opened"], "replied": r["replied"]}
                     for r in stage_stats(session, c, steps)]
    return out


def enroll_in_campaign(session, actor, campaign_id, person_ids=None, emails=None):
    camp = _get(session, Campaign, campaign_id, "Campaign")
    people = []
    for pid in person_ids or []:
        people.append(_get(session, Person, pid, "Person"))
    missing = []
    for e in emails or []:
        p = people_svc.find_by_email(session, e)
        if p is None:
            missing.append(e)
        else:
            people.append(p)
    results = []
    steps = steps_of(session, camp.id)
    for p in people:
        try:
            e, created = enroll(session, camp, p, actor=actor, steps=steps)
            results.append({"person_id": p.id, "enrollment_id": e.id, "status": "enrolled" if created else "already"})
        except EnrollError as ex:
            results.append({"person_id": p.id, "status": "skipped", "reason": str(ex)})
    return {"campaign": camp.name, "campaign_status": camp.status, "results": results,
            "unknown_emails": missing,
            "note": None if camp.status == "active" else "Campaign is not active, so nothing is sent until it is started."}


def set_campaign_state(session, actor, campaign_id, status):
    from app.crm import audit
    camp = _get(session, Campaign, campaign_id, "Campaign")
    if status not in ("active", "paused", "archived"):
        raise ValueError("status must be active, paused or archived.")
    if status == "active" and not steps_of(session, camp.id):
        raise ValueError("Add at least one email stage before starting.")
    old = camp.status
    set_campaign_status(camp, status)
    if status == "active":
        from app.services import recompute_campaign
        recompute_campaign(session, camp)
    audit.record(session, actor, "status", "campaign", camp.id, {"status": [old, status]})
    return ser.campaign(camp)


# ---------------------------------------------------------------------- conversations

def list_conversations(session, status="open", channel=None, person_id=None, limit=25):
    q = conv_svc.inbox_query(session, status, channel).options(selectinload(Conversation.person))
    if person_id:
        q = q.filter(Conversation.person_id == int(person_id))
    return {"conversations": [ser.conversation(c) for c in q.limit(_limit(limit))]}


def get_conversation(session, conversation_id, limit=50, mark_read=False):
    c = _get(session, Conversation, conversation_id, "Conversation")
    msgs = (session.query(ConversationMessage).filter(ConversationMessage.conversation_id == c.id)
            .options(selectinload(ConversationMessage.sequence_message))
            .order_by(ConversationMessage.id.desc()).limit(_limit(limit)).all())[::-1]
    if mark_read:
        conv_svc.mark_read(c)
    out = ser.conversation(c, msgs)
    if c.channel == "whatsapp":
        out["whatsapp_window_open"] = conv_svc.whatsapp_window_open(c)
    return out


def send_message(session, actor, person_id, channel, body=None, subject=None, conversation_id=None,
                 template=None, template_language="en_US", template_params=None):
    p = _get(session, Person, person_id, "Person")
    conv = _get(session, Conversation, conversation_id, "Conversation") if conversation_id else None
    try:
        m = conv_svc.send(session, p, channel, body, subject=subject, conversation=conv, actor=actor,
                          template=template, template_language=template_language,
                          template_params=template_params, settings=app_settings.load(session))
    except conv_svc.SendBlocked as e:
        raise ValueError(str(e))
    except (mailer.SendError, whatsapp.WhatsAppError) as e:
        session.commit()                      # keep the failed attempt in the thread
        raise ValueError(f"Delivery failed: {e}")
    return {"sent": True, "conversation_id": m.conversation_id, "message": ser.conversation_message(m)}


# ---------------------------------------------------------------------- overview

def overview(session):
    now = utcnow()
    summary = deals_svc.summary(session)
    return {
        "people": session.query(func.count(Person.id)).scalar() or 0,
        "companies": session.query(func.count(Company.id)).scalar() or 0,
        "open_deals": {"count": summary["open"][0], "value": float(summary["open"][1]),
                       "weighted": float(summary["weighted"])},
        "won_value": float(summary["won"][1]),
        "tasks": {"overdue": activities_svc.task_query(session, "overdue", now=now).count(),
                  "due_today": activities_svc.task_query(session, "today", now=now).count()},
        "inbox_unread": conv_svc.unread_total(session),
        "email": engagement_stats(session),
        "campaigns": [{"id": c.id, "name": c.name, "status": c.status}
                      for c in session.query(Campaign).filter(Campaign.status == "active")],
        "recent_notes": [ser.activity(a) for a in
                         session.query(Activity).order_by(Activity.occurred_at.desc()).limit(5)],
    }
