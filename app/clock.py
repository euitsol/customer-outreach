from datetime import datetime, timezone


def utcnow():
    """Naive UTC now (the DB stores naive UTC); replaces the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
