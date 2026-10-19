import re
from app.models import Tag, Contact, Event, ClickLink


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

def add_tags(session, contact, value):
    for name in parse_tag_names(value):
        tag = get_or_create_tag(session, name)
        if tag not in contact.tags:
            contact.tags.append(tag)

def remove_tags(session, contact, value):
    names = set(parse_tag_names(value))
    contact.tags = [t for t in contact.tags if t.name not in names]

def set_tags(session, contact, value):
    contact.tags = [get_or_create_tag(session, n) for n in parse_tag_names(value)]

def delete_contacts(session, ids):
    ids = list(ids)
    if not ids:
        return 0
    session.query(Event).filter(Event.contact_id.in_(ids)).delete(synchronize_session=False)
    session.query(ClickLink).filter(ClickLink.contact_id.in_(ids)).delete(synchronize_session=False)
    contacts = session.query(Contact).filter(Contact.id.in_(ids)).all()
    for c in contacts:
        session.delete(c)
    return len(contacts)

def _re(subject):
    s = subject or ""
    return s if s.lower().startswith("re:") else f"Re: {s}"

def next_step(c, today, force=False):
    """Returns (stage, subject, body) for the next email, or None.
    force=True ignores the follow-up dates (manual / queued sends)."""
    if (c.stage or 0) == 0 and c.body1:
        return 1, c.subject, c.body1
    if c.stage == 1 and c.body2 and (force or (c.body2_date and c.body2_date <= today)):
        return 2, _re(c.subject), c.body2
    if c.stage == 2 and c.body3 and (force or (c.body3_date and c.body3_date <= today)):
        return 3, _re(c.subject), c.body3
    return None