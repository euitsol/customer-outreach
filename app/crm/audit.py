"""Audit trail. Every write made through the panel, the REST API or the MCP server goes through
`record`, so it is always possible to answer "who changed this, and when"."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.models import AuditLog


@dataclass(frozen=True)
class Actor:
    type: str = "system"        # user | api | mcp | system
    id: str | None = None
    label: str | None = None
    user_id: int | None = None  # set when the actor is (or acts for) a panel user

    @property
    def description(self):
        return self.label or self.type


SYSTEM = Actor("system", label="system")


def _plain(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


def diff(obj, fields):
    """Snapshot of `fields` on obj; pass the result of two snapshots to `changes`."""
    return {f: _plain(getattr(obj, f, None)) for f in fields}


def changes(before, after):
    return {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}


def record(session, actor, action, entity_type, entity_id=None, data=None):
    actor = actor or SYSTEM
    session.add(AuditLog(
        actor_type=actor.type, actor_id=actor.id, actor_label=(actor.label or "")[:200] or None,
        action=action, entity_type=entity_type, entity_id=None if entity_id is None else str(entity_id),
        changes=_plain(data) if data else None))
