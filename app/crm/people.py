"""People (CRM contacts): identities, tags, consent, timeline, de-duplication."""
import re

from sqlalchemy import func, or_

from app.clock import utcnow
from app.crm import audit, companies, normalize
from app.models import (
    Activity, Consent, ContactPoint, Conversation, ConversationMessage, Deal, Enrollment, Event, Person, Tag,
    Task, deal_people,
)

FIELDS = ["first_name", "last_name", "job_title", "email", "phone", "lifecycle_stage", "lead_status", "source",
          "owner_id", "company_id", "do_not_contact"]

LIFECYCLE_STAGES = [("subscriber", "Subscriber"), ("lead", "Lead"), ("mql", "Marketing qualified"),
                    ("sql", "Sales qualified"), ("opportunity", "Opportunity"), ("customer", "Customer"),
                    ("evangelist", "Evangelist"), ("other", "Other")]
LEAD_STATUSES = [("new", "New"), ("open", "Open"), ("in_progress", "In progress"), ("contacted", "Contacted"),
                 ("qualified", "Qualified"), ("unqualified", "Unqualified"), ("bad_timing", "Bad timing")]
LIFECYCLE_KEYS = {k for k, _ in LIFECYCLE_STAGES}
LEAD_STATUS_KEYS = {k for k, _ in LEAD_STATUSES}


class IdentityTaken(ValueError):
    def __init__(self, kind, value, owner):
        super().__init__(f"{kind} {value} already belongs to {owner.display_name} (#{owner.id}).")
        self.owner = owner


# ---------------------------------------------------------------------- lookup

def find_by_email(session, value):
    e = normalize.email(value)
    if not e:
        return None
    p = session.query(Person).filter(Person.email == e).first()
    if p:
        return p
    cp = session.query(ContactPoint).filter(ContactPoint.kind == "email", ContactPoint.value == e).first()
    return cp.person if cp else None


def find_by_phone(session, value):
    ph = normalize.phone(value)
    if not ph:
        return None
    cp = (session.query(ContactPoint)
          .filter(ContactPoint.kind.in_(("phone", "whatsapp")), ContactPoint.value == ph).first())
    if cp:
        return cp.person
    return session.query(Person).filter(Person.phone == ph).first()


def search_query(session, term):
    q = session.query(Person)
    term = (term or "").strip().lower()
    if term:
        like = f"%{term}%"
        from app.models import Company
        q = q.outerjoin(Company, Company.id == Person.company_id).filter(or_(
            func.lower(func.coalesce(Person.first_name, "") + " " + func.coalesce(Person.last_name, "")).like(like),
            func.lower(func.coalesce(Person.email, "")).like(like),
            func.coalesce(Person.phone, "").like(like),
            func.lower(func.coalesce(Person.job_title, "")).like(like),
            func.lower(func.coalesce(Company.name, "")).like(like),
        ))
    return q


# ---------------------------------------------------------------------- identities

def _point(session, kind, value):
    return session.query(ContactPoint).filter(ContactPoint.kind == kind, ContactPoint.value == value).first()


def add_contact_point(session, person, kind, value, label=None, primary=False, status="active"):
    if kind == "email":
        value = normalize.email(value)
        if not value:
            raise ValueError("Not a valid email address.")
    elif kind in ("phone", "whatsapp"):
        value = normalize.phone(value)
        if not value:
            raise ValueError("Not a valid phone number.")
    else:
        value = (value or "").strip()[:320]
        if not value:
            raise ValueError("Value is required.")
    cp = _point(session, kind, value)
    if cp and cp.person_id != person.id:
        raise IdentityTaken(kind, value, cp.person)
    if not cp:
        cp = ContactPoint(person_id=person.id, kind=kind, value=value, label=label, status=status)
        session.add(cp)
        person.contact_points.append(cp)
    if primary:
        for other in person.contact_points:
            if other.kind == kind and other is not cp:
                other.is_primary = False
        cp.is_primary = True
        if kind == "email":
            person.email = value
        elif kind == "phone":
            person.phone = value
    session.flush()
    return cp


def set_primary_email(session, person, value):
    if not (value or "").strip():
        person.email = None
        for cp in person.contact_points:
            if cp.kind == "email":
                cp.is_primary = False
        return None
    return add_contact_point(session, person, "email", value, primary=True)


def set_primary_phone(session, person, value):
    if not (value or "").strip():
        person.phone = None
        for cp in person.contact_points:
            if cp.kind == "phone":
                cp.is_primary = False
        return None
    return add_contact_point(session, person, "phone", value, primary=True)


def remove_contact_point(session, person, cp):
    if cp.person_id != person.id:
        raise ValueError("That contact point belongs to someone else.")
    if cp.is_primary and cp.kind == "email":
        person.email = None
    if cp.is_primary and cp.kind == "phone":
        person.phone = None
    person.contact_points.remove(cp)
    session.delete(cp)
    session.flush()
    nxt = next((p for p in person.contact_points if p.kind == cp.kind), None)
    if nxt and cp.is_primary:
        add_contact_point(session, person, nxt.kind, nxt.value, primary=True)


def whatsapp_number(person):
    """The number to message on WhatsApp: an explicit whatsapp point, else the primary phone."""
    for cp in person.contact_points:
        if cp.kind == "whatsapp" and cp.status == "active":
            return cp.value
    return person.phone


# ---------------------------------------------------------------------- create / update

def upsert(session, *, email=None, phone=None, first_name=None, last_name=None, job_title=None,
           company_name=None, company_website=None, source=None, lifecycle_stage=None, owner_id=None,
           attributes=None, overwrite=False, actor=None):
    """Find a person by email (then phone) or create one. Empty fields are filled in; existing
    values are only replaced when overwrite=True. Returns (person, created)."""
    person = find_by_email(session, email) if email else None
    if person is None and phone and not normalize.email(email):
        # phone only identifies someone when there is no email (office numbers are shared)
        person = find_by_phone(session, phone)
    created = person is None
    if created:
        person = Person(lifecycle_stage=lifecycle_stage if lifecycle_stage in LIFECYCLE_KEYS else "lead",
                        source=source, owner_id=owner_id, attributes={}, do_not_contact=False)
        session.add(person)
        session.flush()

    def put(field, value):
        value = (value or "").strip() if isinstance(value, str) else value
        if value and (overwrite or not getattr(person, field)):
            setattr(person, field, value)

    put("first_name", first_name)
    put("last_name", last_name)
    put("job_title", job_title)
    if email and normalize.email(email) and (overwrite or not person.email):
        set_primary_email(session, person, email)
    elif email and normalize.email(email):
        add_contact_point(session, person, "email", email)
    if phone and (overwrite or not person.phone):
        try:
            set_primary_phone(session, person, phone)
        except IdentityTaken:
            pass                    # shared office number: keep it on the first person only
    if company_name or company_website:
        if overwrite or not person.company_id:
            company, _ = companies.get_or_create(session, company_name, company_website, email, actor)
            if company:
                person.company_id = company.id
                person.company = company
    if attributes:
        merged = dict(person.attributes or {})
        for k, v in attributes.items():
            if v not in (None, "") and (overwrite or k not in merged):
                merged[k] = v
        person.attributes = merged
    if not created and lifecycle_stage in LIFECYCLE_KEYS and overwrite:
        person.lifecycle_stage = lifecycle_stage
    session.flush()
    if created:
        audit.record(session, actor, "create", "person", person.id,
                     {"email": person.email, "name": person.full_name, "source": source})
    return person, created


def update(session, person, data, actor=None):
    """Apply a dict of field values (only keys present are touched). Returns the changes made."""
    before = audit.diff(person, FIELDS + ["attributes"])
    for f in ("first_name", "last_name", "job_title", "lead_status", "source"):
        if f in data:
            v = data[f]
            setattr(person, f, (v.strip() if isinstance(v, str) else v) or None)
    if "lifecycle_stage" in data and data["lifecycle_stage"]:
        if data["lifecycle_stage"] not in LIFECYCLE_KEYS:
            raise ValueError(f"Unknown lifecycle stage '{data['lifecycle_stage']}'.")
        person.lifecycle_stage = data["lifecycle_stage"]
    if "lead_status" in data and data["lead_status"] and data["lead_status"] not in LEAD_STATUS_KEYS:
        raise ValueError(f"Unknown lead status '{data['lead_status']}'.")
    if "owner_id" in data:
        person.owner_id = data["owner_id"] or None
    if "do_not_contact" in data:
        person.do_not_contact = bool(data["do_not_contact"])
    if "email" in data:
        set_primary_email(session, person, data["email"])
    if "phone" in data:
        set_primary_phone(session, person, data["phone"])
    if "company_id" in data:
        person.company_id = data["company_id"] or None
    elif "company" in data or "company_name" in data:
        name = (data.get("company") or data.get("company_name") or "").strip()
        if name:
            company, _ = companies.get_or_create(session, name, data.get("company_website"), person.email, actor)
            person.company_id = company.id if company else None
        else:
            person.company_id = None
    if "attributes" in data and data["attributes"] is not None:
        merged = {**(person.attributes or {}), **data["attributes"]}
        person.attributes = {k: v for k, v in merged.items() if v not in (None, "", [])}
    session.flush()
    delta = audit.changes(before, audit.diff(person, FIELDS + ["attributes"]))
    if delta:
        audit.record(session, actor, "update", "person", person.id, delta)
    return delta


def delete(session, person, actor=None):
    from app.services import delete_enrollments
    audit.record(session, actor, "delete", "person", person.id, {"email": person.email, "name": person.full_name})
    delete_enrollments(session, [e.id for e in person.enrollments])
    for model in (Activity, Task, Conversation, Consent):
        session.query(model).filter(model.person_id == person.id).delete(synchronize_session=False)
    session.query(Deal).filter(Deal.person_id == person.id).update({Deal.person_id: None}, synchronize_session=False)
    session.execute(deal_people.delete().where(deal_people.c.person_id == person.id))
    person.tags = []
    session.flush()
    session.delete(person)


# ---------------------------------------------------------------------- tags

def parse_tag_names(value):
    parts = re.split(r"[;,]", value) if isinstance(value, str) else list(value or [])
    out = []
    for p in parts:
        name = str(p).strip().lower()[:100]
        if name and name not in out:
            out.append(name)
    return out


def get_or_create_tag(session, name):
    tag = session.query(Tag).filter(Tag.name == name).first()
    if not tag:
        tag = Tag(name=name)
        session.add(tag)
        session.flush()
    return tag


def add_tags(session, record, value):
    for name in parse_tag_names(value):
        tag = get_or_create_tag(session, name)
        if tag not in record.tags:
            record.tags.append(tag)


def remove_tags(session, record, value):
    names = set(parse_tag_names(value))
    record.tags = [t for t in record.tags if t.name not in names]


def set_tags(session, record, value):
    record.tags = [get_or_create_tag(session, n) for n in parse_tag_names(value)]


# ---------------------------------------------------------------------- consent

def consent_status(session, person, channel):
    c = session.query(Consent).filter_by(person_id=person.id, channel=channel).first()
    return c.status if c else None


def set_consent(session, person, channel, status, source=None, actor=None):
    c = session.query(Consent).filter_by(person_id=person.id, channel=channel).first()
    if c is None:
        c = Consent(person_id=person.id, channel=channel)
        session.add(c)
    c.status, c.source, c.recorded_at = status, (source or "")[:200] or None, utcnow()
    audit.record(session, actor, "consent", "person", person.id, {"channel": channel, "status": status,
                                                                   "source": source})
    return c


def can_contact(session, person, channel):
    if person.do_not_contact:
        return False
    return consent_status(session, person, channel) != "revoked"


def touch(person, when=None, contacted=False):
    when = when or utcnow()
    if not person.last_activity_at or person.last_activity_at < when:
        person.last_activity_at = when
    if contacted and (not person.last_contacted_at or person.last_contacted_at < when):
        person.last_contacted_at = when


# ---------------------------------------------------------------------- timeline

def timeline(session, person, limit=100):
    """Everything that happened with this person, newest first, as plain dicts."""
    items = []
    for a in (session.query(Activity).filter(Activity.person_id == person.id)
              .order_by(Activity.occurred_at.desc()).limit(limit)):
        items.append({"when": a.occurred_at, "kind": a.kind, "title": a.subject or a.kind.capitalize(),
                      "body": a.body, "user": a.user.label if a.user else None, "activity": a})
    enrollment_ids = [e.id for e in person.enrollments]
    if enrollment_ids:
        camp = {e.id: e.campaign for e in person.enrollments}
        for ev in (session.query(Event).filter(Event.enrollment_id.in_(enrollment_ids),
                                               Event.type.in_(("sent", "opened", "clicked", "replied", "bounced",
                                                               "unsubscribed", "failed")))
                   .order_by(Event.id.desc()).limit(limit)):
            c = camp.get(ev.enrollment_id)
            items.append({"when": ev.created_at, "kind": f"campaign_{ev.type}",
                          "title": f"Campaign email {ev.type}" + (f" (stage {ev.stage})" if ev.stage else ""),
                          "body": ev.detail if ev.type in ("sent", "failed", "bounced", "clicked") else None,
                          "campaign": c, "enrollment_id": ev.enrollment_id})
    for m in (session.query(ConversationMessage).join(Conversation)
              .filter(Conversation.person_id == person.id, ConversationMessage.sequence_message_id.is_(None))
              .order_by(ConversationMessage.id.desc()).limit(limit)):
        items.append({"when": m.sent_at or m.created_at, "kind": f"{m.channel}_{m.direction}",
                      "title": f"{m.channel.capitalize()} {'received' if m.direction == 'in' else 'sent'}"
                               + (f": {m.subject}" if m.subject else ""),
                      "body": m.body, "conversation_id": m.conversation_id, "status": m.status})
    for t in session.query(Task).filter(Task.person_id == person.id).order_by(Task.id.desc()).limit(limit):
        items.append({"when": t.completed_at or t.created_at, "kind": "task",
                      "title": ("Task done: " if t.status == "done" else "Task: ") + t.title, "body": t.notes,
                      "task": t})
    items = [i for i in items if i["when"]]
    items.sort(key=lambda i: i["when"], reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------- de-duplication

def merge(session, keep, other, actor=None):
    """Fold `other` into `keep`: identities, enrollments, history, deals, tags. `other` is deleted."""
    if keep.id == other.id:
        raise ValueError("Pick two different people.")
    for f in ("first_name", "last_name", "job_title", "company_id", "lead_status", "source", "owner_id",
              "last_contacted_at", "last_activity_at"):
        if not getattr(keep, f) and getattr(other, f):
            setattr(keep, f, getattr(other, f))
    keep.attributes = {**(other.attributes or {}), **(keep.attributes or {})}
    keep.do_not_contact = keep.do_not_contact or other.do_not_contact
    for cp in list(other.contact_points):
        clash = _point(session, cp.kind, cp.value)
        other.contact_points.remove(cp)
        if clash is not None and clash.id != cp.id:
            session.delete(cp)
            continue
        cp.person_id = keep.id
        cp.is_primary = False
        keep.contact_points.append(cp)
    session.flush()
    if not keep.email:
        e = next((cp.value for cp in keep.contact_points if cp.kind == "email"), None)
        if e:
            set_primary_email(session, keep, e)
    if not keep.phone:
        p = next((cp.value for cp in keep.contact_points if cp.kind == "phone"), None)
        if p:
            set_primary_phone(session, keep, p)
    kept_campaigns = {e.campaign_id: e for e in keep.enrollments}
    from app.services import delete_enrollments
    drop = []
    for e in list(other.enrollments):
        mine = kept_campaigns.get(e.campaign_id)
        if mine is None:
            e.person_id = keep.id
        elif (e.stage or 0) > (mine.stage or 0):     # keep whichever got further
            drop.append(mine.id)
            e.person_id = keep.id
        else:
            drop.append(e.id)
    session.flush()
    delete_enrollments(session, drop)
    for model in (Activity, Task, Conversation):
        session.query(model).filter(model.person_id == other.id).update({model.person_id: keep.id},
                                                                        synchronize_session=False)
    session.query(Deal).filter(Deal.person_id == other.id).update({Deal.person_id: keep.id},
                                                                  synchronize_session=False)
    for (deal_id,) in session.query(deal_people.c.deal_id).filter(deal_people.c.person_id == other.id).all():
        if not session.query(deal_people).filter_by(deal_id=deal_id, person_id=keep.id).first():
            session.execute(deal_people.insert().values(deal_id=deal_id, person_id=keep.id))
    session.execute(deal_people.delete().where(deal_people.c.person_id == other.id))
    for c in session.query(Consent).filter(Consent.person_id == other.id).all():
        mine = session.query(Consent).filter_by(person_id=keep.id, channel=c.channel).first()
        if mine is None:
            c.person_id = keep.id
        else:
            if c.status == "revoked":              # the stricter decision wins
                mine.status, mine.source = "revoked", c.source
            session.delete(c)
    for t in other.tags:
        if t not in keep.tags:
            keep.tags.append(t)
    other.tags = []
    audit.record(session, actor, "merge", "person", keep.id, {"merged_person_id": other.id,
                                                               "merged_email": other.email})
    session.flush()
    session.expire(other)
    session.delete(other)
    session.flush()
    return keep
