"""Notes, calls, meetings (activities) and follow-up tasks."""
from sqlalchemy import or_

from app.clock import utcnow
from app.crm import audit, people as people_svc
from app.models import Activity, Deal, Person, Task

ACTIVITY_KINDS = [("note", "Note"), ("call", "Call"), ("meeting", "Meeting"), ("email", "Email (logged)"),
                  ("whatsapp", "WhatsApp (logged)")]
TASK_PRIORITIES = ["low", "normal", "high"]


def _owner_ids(person_id=None, company_id=None, deal_id=None):
    if not (person_id or company_id or deal_id):
        raise ValueError("Attach it to a person, company or deal.")
    return {"person_id": person_id or None, "company_id": company_id or None, "deal_id": deal_id or None}


def log(session, *, kind, body=None, subject=None, person_id=None, company_id=None, deal_id=None, user_id=None,
        occurred_at=None, duration_minutes=None, outcome=None, meta=None, actor=None):
    if kind not in {k for k, _ in ACTIVITY_KINDS} | {"system"}:
        raise ValueError(f"Unknown activity kind '{kind}'.")
    if not (body or subject or "").strip():
        raise ValueError("Write something first.")
    ids = _owner_ids(person_id, company_id, deal_id)
    if ids["deal_id"] and not ids["company_id"]:
        deal = session.get(Deal, ids["deal_id"])
        ids["company_id"] = deal.company_id if deal else None
    if ids["person_id"] and not ids["company_id"]:
        person = session.get(Person, ids["person_id"])
        ids["company_id"] = person.company_id if person else None
    a = Activity(kind=kind, subject=(subject or "").strip()[:300] or None, body=(body or "").strip() or None,
                 user_id=user_id, occurred_at=occurred_at or utcnow(), duration_minutes=duration_minutes,
                 outcome=(outcome or None) and outcome[:50], meta=meta, **ids)
    session.add(a)
    session.flush()
    if a.person_id:
        p = session.get(Person, a.person_id)
        people_svc.touch(p, a.occurred_at, contacted=kind in ("call", "meeting", "email", "whatsapp"))
    audit.record(session, actor, "create", "activity", a.id, {"kind": kind, **ids})
    return a


def delete_activity(session, activity, actor=None):
    audit.record(session, actor, "delete", "activity", activity.id, {"kind": activity.kind})
    session.delete(activity)


def create_task(session, *, title, due_at=None, priority="normal", notes=None, assignee_id=None, person_id=None,
                company_id=None, deal_id=None, created_by_id=None, actor=None):
    if not (title or "").strip():
        raise ValueError("A task needs a title.")
    if priority not in TASK_PRIORITIES:
        priority = "normal"
    t = Task(title=title.strip()[:300], due_at=due_at, priority=priority, notes=(notes or "").strip() or None,
             assignee_id=assignee_id or None, person_id=person_id or None, company_id=company_id or None,
             deal_id=deal_id or None, created_by_id=created_by_id, status="open")
    session.add(t)
    session.flush()
    audit.record(session, actor, "create", "task", t.id, {"title": t.title, "due_at": due_at})
    return t


def set_task_status(session, task, status, actor=None):
    if status not in ("open", "done", "cancelled"):
        raise ValueError("Unknown task status.")
    task.status = status
    task.completed_at = utcnow() if status == "done" else None
    if status == "done" and task.person_id:
        people_svc.touch(session.get(Person, task.person_id))
    audit.record(session, actor, status if status != "open" else "reopen", "task", task.id, {"title": task.title})
    return task


def update_task(session, task, data, actor=None):
    before = audit.diff(task, ["title", "notes", "due_at", "priority", "assignee_id"])
    if "title" in data:
        if not (data["title"] or "").strip():
            raise ValueError("A task needs a title.")
        task.title = data["title"].strip()[:300]
    if "notes" in data:
        task.notes = (data["notes"] or "").strip() or None
    if "due_at" in data:
        task.due_at = data["due_at"]
    if "priority" in data and data["priority"] in TASK_PRIORITIES:
        task.priority = data["priority"]
    if "assignee_id" in data:
        task.assignee_id = data["assignee_id"] or None
    delta = audit.changes(before, audit.diff(task, ["title", "notes", "due_at", "priority", "assignee_id"]))
    if delta:
        audit.record(session, actor, "update", "task", task.id, delta)
    return delta


def task_query(session, view="open", assignee_id=None, person_id=None, company_id=None, deal_id=None, now=None):
    now = now or utcnow()
    q = session.query(Task)
    if view == "open":
        q = q.filter(Task.status == "open")
    elif view == "overdue":
        q = q.filter(Task.status == "open", Task.due_at < now)
    elif view == "today":
        end = now.replace(hour=23, minute=59, second=59)
        q = q.filter(Task.status == "open", Task.due_at <= end)
    elif view == "done":
        q = q.filter(Task.status == "done")
    if assignee_id:
        q = q.filter(Task.assignee_id == assignee_id)
    if person_id:
        q = q.filter(Task.person_id == person_id)
    if company_id:
        q = q.filter(or_(Task.company_id == company_id,
                         Task.person_id.in_(session.query(Person.id).filter(Person.company_id == company_id))))
    if deal_id:
        q = q.filter(Task.deal_id == deal_id)
    if view == "done":
        return q.order_by(Task.completed_at.desc())
    return q.order_by(Task.due_at.is_(None), Task.due_at.asc(), Task.id)


def company_activities(session, company, limit=80):
    people_ids = session.query(Person.id).filter(Person.company_id == company.id)
    return (session.query(Activity)
            .filter(or_(Activity.company_id == company.id, Activity.person_id.in_(people_ids)))
            .order_by(Activity.occurred_at.desc()).limit(limit).all())


def deal_activities(session, deal, limit=80):
    return (session.query(Activity).filter(Activity.deal_id == deal.id)
            .order_by(Activity.occurred_at.desc()).limit(limit).all())

