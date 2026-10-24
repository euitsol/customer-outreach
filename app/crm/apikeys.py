"""API keys for the REST API and the MCP server. Format: `oc_<prefix>_<secret>`; only a SHA-256 of
the whole key is stored, so a leaked database does not leak usable keys."""
import hashlib
import secrets
from datetime import timedelta

from app.clock import utcnow
from app.crm import audit
from app.models import ApiKey

SCOPES = ["read", "write", "send"]   # send = may email / WhatsApp people (outward-facing)


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create(session, name, scopes=None, user_id=None, expires_days=None, actor=None):
    scopes = [s for s in (scopes or ["read", "write"]) if s in SCOPES] or ["read"]
    prefix = secrets.token_hex(4)
    raw = f"oc_{prefix}_{secrets.token_urlsafe(32)}"
    key = ApiKey(name=(name or "API key").strip()[:200], prefix=prefix, key_hash=_hash(raw), scopes=scopes,
                 created_by_id=user_id,
                 expires_at=utcnow() + timedelta(days=int(expires_days)) if expires_days else None)
    session.add(key)
    session.flush()
    audit.record(session, actor, "create", "api_key", key.id, {"name": key.name, "scopes": scopes})
    return raw, key


def verify(session, raw):
    """The active ApiKey for a presented token, or None."""
    raw = (raw or "").strip()
    if not raw.startswith("oc_"):
        return None
    key = session.query(ApiKey).filter(ApiKey.key_hash == _hash(raw)).first()
    if key is None or not key.active:
        return None
    now = utcnow()
    if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
        key.last_used_at = now
        session.commit()
    return key


def revoke(session, key, actor=None):
    key.revoked_at = utcnow()
    audit.record(session, actor, "revoke", "api_key", key.id, {"name": key.name})


def actor_for(key, via="api"):
    return audit.Actor(via, str(key.id), f"{via}: {key.name}", user_id=key.created_by_id)
