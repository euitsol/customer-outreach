"""Plain-dict views of records, shared by the REST API and the MCP server."""
from datetime import date, datetime
from decimal import Decimal


def _v(x):
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, Decimal):
        return float(x)
    return x


def tag_names(record):
    return [t.name for t in (record.tags or [])]


def company(c, detail=False):
    if c is None:
        return None
    out = {"id": c.id, "name": c.name, "domain": c.domain, "website": c.website, "industry": c.industry,
           "size": c.size, "city": c.city, "country": c.country}
    if detail:
        out.update({"phone": c.phone, "address": c.address, "description": c.description,
                    "owner": c.owner.label if c.owner else None, "tags": tag_names(c),
                    "custom_fields": dict(c.attributes or {}), "created_at": _v(c.created_at),
                    "people_count": len(c.people)})
    return out


def person(p, detail=False):
    if p is None:
        return None
    out = {"id": p.id, "name": p.full_name or None, "first_name": p.first_name, "last_name": p.last_name,
           "email": p.email, "phone": p.phone, "job_title": p.job_title,
           "company": {"id": p.company.id, "name": p.company.name} if p.company else None,
           "lifecycle_stage": p.lifecycle_stage, "lead_status": p.lead_status, "tags": tag_names(p),
           "last_activity_at": _v(p.last_activity_at)}
    if detail:
        out.update({
            "source": p.source, "owner": p.owner.label if p.owner else None, "do_not_contact": p.do_not_contact,
            "custom_fields": dict(p.attributes or {}),
            "contact_points": [{"kind": cp.kind, "value": cp.value, "primary": cp.is_primary, "status": cp.status}
                               for cp in p.contact_points],
            "consents": {c.channel: c.status for c in p.consents},
            "enrollments": [{"id": e.id, "campaign_id": e.campaign_id, "campaign": e.campaign.name if e.campaign else None,
                             "stage": e.stage, "replied": e.replied, "unsubscribed": e.unsubscribed,
                             "bounced": e.bounced} for e in p.enrollments],
            "last_contacted_at": _v(p.last_contacted_at), "created_at": _v(p.created_at),
        })
    return out


def deal(d, detail=False):
    if d is None:
        return None
    out = {"id": d.id, "title": d.title, "amount": _v(d.amount), "currency": d.currency, "status": d.status,
           "pipeline": d.pipeline.name if d.pipeline else None, "stage": d.stage.name if d.stage else None,
           "stage_id": d.stage_id, "company": {"id": d.company.id, "name": d.company.name} if d.company else None,
           "person": {"id": d.person.id, "name": d.person.display_name} if d.person else None,
           "expected_close_date": _v(d.expected_close_date)}
    if detail:
        out.update({"owner": d.owner.label if d.owner else None, "lost_reason": d.lost_reason, "source": d.source,
                    "custom_fields": dict(d.attributes or {}), "closed_at": _v(d.closed_at),
                    "people": [{"id": p.id, "name": p.display_name} for p in d.people],
                    "created_at": _v(d.created_at), "updated_at": _v(d.updated_at)})
    return out


def activity(a):
    return {"id": a.id, "kind": a.kind, "subject": a.subject, "body": a.body, "occurred_at": _v(a.occurred_at),
            "person_id": a.person_id, "company_id": a.company_id, "deal_id": a.deal_id,
            "user": a.user.label if a.user else None}


def task(t):
    return {"id": t.id, "title": t.title, "notes": t.notes, "status": t.status, "priority": t.priority,
            "due_at": _v(t.due_at), "completed_at": _v(t.completed_at),
            "assignee": t.assignee.label if t.assignee else None, "person_id": t.person_id,
            "company_id": t.company_id, "deal_id": t.deal_id}


def conversation(c, messages=None):
    out = {"id": c.id, "channel": c.channel, "status": c.status, "subject": c.subject,
           "person": {"id": c.person.id, "name": c.person.display_name, "email": c.person.email,
                      "phone": c.person.phone} if c.person else None,
           "unread": c.unread_count, "last_message_at": _v(c.last_message_at),
           "last_inbound_at": _v(c.last_inbound_at)}
    if messages is not None:
        out["messages"] = [conversation_message(m) for m in messages]
    return out


def conversation_message(m):
    body, subject = m.body, m.subject
    if m.sequence_message is not None:
        body = body or m.sequence_message.sent_body
        subject = subject or m.sequence_message.sent_subject
    return {"id": m.id, "direction": m.direction, "channel": m.channel, "subject": subject, "body": body,
            "status": m.status, "error": m.error, "at": _v(m.sent_at or m.created_at),
            "from_campaign": m.sequence_message_id is not None}


def campaign(c, summary=None):
    out = {"id": c.id, "name": c.name, "status": c.status, "channel": c.channel, "description": c.description,
           "start_at": _v(c.start_at), "steps": len(c.steps)}
    if summary:
        out["stats"] = {k: _v(v) for k, v in summary.items()}
    return out


def enrollment(e):
    return {"id": e.id, "campaign_id": e.campaign_id, "person_id": e.person_id, "email": e.email,
            "stage": e.stage, "next_due_at": _v(e.next_due_at), "replied": e.replied,
            "unsubscribed": e.unsubscribed, "bounced": e.bounced, "paused": not e.send_flag,
            "variables": e.custom_fields}
