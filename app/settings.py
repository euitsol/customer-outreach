"""Runtime settings stored in the database (editable from the panel).

Defaults come from environment variables so an existing deployment keeps behaving
exactly as before until the user changes something in the UI.
"""
from datetime import datetime, timedelta, timezone as _tz
from app.clock import utcnow
from zoneinfo import ZoneInfo

from app import config
from app.models import Setting

DEFAULTS = {
    "sending_paused": False,          # global kill switch
    "min_delay": config.MIN_DELAY_SECONDS,
    "max_delay": config.MAX_DELAY_SECONDS,
    "daily_limit": config.MAX_PER_DAY,
    "poll_interval": config.CHECK_INTERVAL_SECONDS,
    "window_enabled": False,
    "window_start": "08:00",
    "window_end": "18:00",
    "weekdays_only": False,
    "timezone": config.TIMEZONE,
    "retry_max": 3,                   # total attempts per email before it needs a manual retry
    "retry_backoff": 30,              # minutes, multiplied by the attempt number
    "sender_name": config.SENDER_NAME,
    "reply_to": "",
    "signature": "",
    "track_opens": True,
    "track_clicks": True,
    "footer_enabled": True,
    "footer_text": "Don't want further emails from us?",
    "footer_address": "",
}

INT_KEYS = {"min_delay", "max_delay", "daily_limit", "poll_interval", "retry_max", "retry_backoff"}
BOOL_KEYS = {"sending_paused", "window_enabled", "weekdays_only", "track_opens",
             "track_clicks", "footer_enabled"}


def _cast(key, raw):
    if raw is None:
        return DEFAULTS.get(key)
    if key in INT_KEYS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULTS[key]
    if key in BOOL_KEYS:
        return str(raw).lower() in ("1", "true", "yes", "on")
    return raw


def load(session):
    rows = {r.key: r.value for r in session.query(Setting).all()}
    out = {k: _cast(k, rows[k]) if k in rows else v for k, v in DEFAULTS.items()}
    out["max_delay"] = max(out["max_delay"], out["min_delay"])
    return out


def get_raw(session, key, default=None):
    row = session.get(Setting, key)
    return row.value if row and row.value is not None else default


def put(session, key, value):
    """Store a value (also used for internal state like heartbeats). Caller commits."""
    if isinstance(value, bool):
        value = "1" if value else "0"
    row = session.get(Setting, key)
    if row:
        row.value = None if value is None else str(value)
    else:
        session.add(Setting(key=key, value=None if value is None else str(value)))


def save_many(session, values):
    for k, v in values.items():
        if k in DEFAULTS:
            put(session, k, v)


# ---- time helpers (DB stores naive UTC) ----

def tz_of(s):
    try:
        return ZoneInfo(s["timezone"])
    except Exception:
        return ZoneInfo("UTC")


def to_local(dt, s):
    if dt is None:
        return None
    return dt.replace(tzinfo=_tz.utc).astimezone(tz_of(s))


def local_midnight_utc(s, now=None):
    """Start of the current local day as naive UTC."""
    now = now or utcnow()
    local = to_local(now, s)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(_tz.utc).replace(tzinfo=None)


def _hhmm(v, fallback):
    try:
        h, m = str(v).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return fallback


def in_send_window(s, now=None):
    """(allowed, next_open_utc_or_None)"""
    if not s["window_enabled"] and not s["weekdays_only"]:
        return True, None
    now = now or utcnow()
    local = to_local(now, s)
    start = _hhmm(s["window_start"], 8 * 60)
    end = _hhmm(s["window_end"], 18 * 60)
    if not s["window_enabled"]:
        start, end = 0, 24 * 60
    minutes = local.hour * 60 + local.minute
    weekday_ok = (not s["weekdays_only"]) or local.weekday() < 5
    if weekday_ok and start <= minutes < end:
        return True, None
    # find the next opening
    probe = local.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60 // 15):
        probe += timedelta(minutes=15)
        m = probe.hour * 60 + probe.minute
        if ((not s["weekdays_only"]) or probe.weekday() < 5) and start <= m < end:
            return False, probe.astimezone(_tz.utc).replace(tzinfo=None)
    return False, None


def local_to_utc(naive_local, s):
    """Interpret a naive datetime as local (settings timezone) and return naive UTC."""
    if naive_local is None:
        return None
    return naive_local.replace(tzinfo=tz_of(s)).astimezone(_tz.utc).replace(tzinfo=None)


def parse_local_input(value, s):
    """Value of an <input type=datetime-local> (or date) -> naive UTC, or None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return local_to_utc(datetime.strptime(value, fmt), s)
        except ValueError:
            continue
    return None


def to_local_input(dt, s):
    local = to_local(dt, s)
    return local.strftime("%Y-%m-%dT%H:%M") if local else ""
