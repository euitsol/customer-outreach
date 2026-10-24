"""Deals and pipelines."""
from decimal import Decimal, InvalidOperation

from sqlalchemy import func

from app import config
from app.clock import utcnow
from app.crm import audit
from app.models import Deal, Pipeline, PipelineStage, deal_people

FIELDS = ["title", "pipeline_id", "stage_id", "company_id", "person_id", "owner_id", "amount", "currency", "status",
          "expected_close_date", "lost_reason", "source"]


def default_pipeline(session):
    return (session.query(Pipeline).order_by(Pipeline.is_default.desc(), Pipeline.position, Pipeline.id).first())


def first_open_stage(pipeline):
    return next((s for s in pipeline.stages if s.kind == "open"), pipeline.stages[0] if pipeline.stages else None)


def parse_amount(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("€", "").replace("$", "").replace("£", "").strip())
    except InvalidOperation:
        raise ValueError(f"'{value}' is not an amount.")


def create(session, *, title, pipeline_id=None, stage_id=None, company_id=None, person_id=None, owner_id=None,
           amount=None, currency=None, expected_close_date=None, source=None, attributes=None, actor=None):
    if not (title or "").strip():
        raise ValueError("A deal needs a title.")
    pipeline = session.get(Pipeline, pipeline_id) if pipeline_id else default_pipeline(session)
    if pipeline is None:
        raise ValueError("No pipeline exists.")
    stage = session.get(PipelineStage, stage_id) if stage_id else first_open_stage(pipeline)
    if stage is None or stage.pipeline_id != pipeline.id:
        raise ValueError("That stage is not part of the pipeline.")
    now = utcnow()
    deal = Deal(title=title.strip()[:300], pipeline_id=pipeline.id, stage_id=stage.id, company_id=company_id,
                person_id=person_id, owner_id=owner_id, amount=parse_amount(amount),
                currency=(currency or config.DEFAULT_CURRENCY).upper()[:3], status="open",
                expected_close_date=expected_close_date, source=source, attributes=attributes or {},
                stage_changed_at=now)
    _apply_stage_status(deal, stage, now)
    session.add(deal)
    session.flush()
    if person_id:
        session.execute(deal_people.insert().values(deal_id=deal.id, person_id=person_id, role="primary"))
    audit.record(session, actor, "create", "deal", deal.id, {"title": deal.title, "stage": stage.name,
                                                             "amount": deal.amount})
    return deal


def _apply_stage_status(deal, stage, now):
    if stage.kind in ("won", "lost"):
        deal.status = stage.kind
        deal.closed_at = deal.closed_at or now
    else:
        deal.status = "open"
        deal.closed_at = None
        deal.lost_reason = None


def move(session, deal, stage, lost_reason=None, actor=None):
    if stage.pipeline_id != deal.pipeline_id:
        deal.pipeline_id = stage.pipeline_id
    if stage.id == deal.stage_id:
        return False
    old = deal.stage.name if deal.stage else None
    now = utcnow()
    deal.stage_id, deal.stage, deal.stage_changed_at = stage.id, stage, now
    _apply_stage_status(deal, stage, now)
    if stage.kind == "lost" and lost_reason:
        deal.lost_reason = lost_reason[:300]
    audit.record(session, actor, "move", "deal", deal.id, {"stage": [old, stage.name], "status": deal.status})
    return True


def update(session, deal, data, actor=None):
    before = audit.diff(deal, FIELDS + ["attributes"])
    if "title" in data:
        if not (data["title"] or "").strip():
            raise ValueError("A deal needs a title.")
        deal.title = data["title"].strip()[:300]
    for f in ("company_id", "person_id", "owner_id"):
        if f in data:
            setattr(deal, f, data[f] or None)
    if "amount" in data:
        deal.amount = parse_amount(data["amount"])
    if "currency" in data and data["currency"]:
        deal.currency = data["currency"].upper()[:3]
    for f in ("expected_close_date", "source", "lost_reason"):
        if f in data:
            v = data[f]
            setattr(deal, f, (v.strip() if isinstance(v, str) else v) or None)
    if "attributes" in data and data["attributes"] is not None:
        merged = {**(deal.attributes or {}), **data["attributes"]}
        deal.attributes = {k: v for k, v in merged.items() if v not in (None, "", [])}
    if data.get("stage_id") and int(data["stage_id"]) != deal.stage_id:
        stage = session.get(PipelineStage, int(data["stage_id"]))
        if stage is None:
            raise ValueError("Unknown stage.")
        move(session, deal, stage, data.get("lost_reason"), actor)
    if deal.person_id and not session.query(deal_people).filter_by(deal_id=deal.id, person_id=deal.person_id).first():
        session.execute(deal_people.insert().values(deal_id=deal.id, person_id=deal.person_id, role="primary"))
    session.flush()
    delta = audit.changes(before, audit.diff(deal, FIELDS + ["attributes"]))
    if delta:
        audit.record(session, actor, "update", "deal", deal.id, delta)
    return delta


def delete(session, deal, actor=None):
    audit.record(session, actor, "delete", "deal", deal.id, {"title": deal.title})
    session.execute(deal_people.delete().where(deal_people.c.deal_id == deal.id))
    session.delete(deal)


def board(session, pipeline, filters=None):
    """[(stage, [deals], total_amount)] for the kanban view."""
    filters = filters or {}
    q = session.query(Deal).filter(Deal.pipeline_id == pipeline.id)
    if filters.get("owner_id"):
        q = q.filter(Deal.owner_id == filters["owner_id"])
    if filters.get("q"):
        q = q.filter(func.lower(Deal.title).like(f"%{filters['q'].lower()}%"))
    by_stage = {}
    for d in q.order_by(Deal.updated_at.desc().nullslast(), Deal.id.desc()):
        by_stage.setdefault(d.stage_id, []).append(d)
    out = []
    for st in pipeline.stages:
        deals = by_stage.get(st.id, [])
        out.append((st, deals, sum((d.amount or 0) for d in deals)))
    return out


def summary(session, pipeline=None):
    q = session.query(Deal.status, func.count(Deal.id), func.coalesce(func.sum(Deal.amount), 0))
    if pipeline:
        q = q.filter(Deal.pipeline_id == pipeline.id)
    out = {"open": (0, 0), "won": (0, 0), "lost": (0, 0)}
    for status, n, total in q.group_by(Deal.status):
        out[status] = (n, total)
    weighted = (session.query(func.coalesce(func.sum(Deal.amount * PipelineStage.probability / 100.0), 0))
                .join(PipelineStage, PipelineStage.id == Deal.stage_id).filter(Deal.status == "open"))
    if pipeline:
        weighted = weighted.filter(Deal.pipeline_id == pipeline.id)
    out["weighted"] = weighted.scalar() or 0
    return out
