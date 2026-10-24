"""Custom field definitions and value coercion. Values are stored in the record's `attributes` JSON
under the field key, typed (numbers as numbers, dates as ISO strings, multiselect as lists)."""
import re
from datetime import datetime

from app.crm import audit
from app.models import CustomField

ENTITIES = [("person", "People"), ("company", "Companies"), ("deal", "Deals")]
FIELD_TYPES = [("text", "Text"), ("textarea", "Long text"), ("number", "Number"), ("date", "Date"),
               ("select", "Dropdown"), ("multiselect", "Multiple choice"), ("checkbox", "Yes / no"),
               ("url", "Link")]
TYPE_KEYS = {k for k, _ in FIELD_TYPES}
RESERVED = {"id", "email", "phone", "first_name", "last_name", "full_name", "company", "name", "title", "domain",
            "website", "owner", "owner_id", "status", "stage", "amount", "tags", "created_at", "updated_at"}


def key_from_label(label):
    return re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")[:64]


def definitions(session, entity):
    return (session.query(CustomField).filter(CustomField.entity == entity)
            .order_by(CustomField.position, CustomField.id).all())


def create(session, entity, label, field_type="text", options=None, is_required=False, actor=None):
    if entity not in {k for k, _ in ENTITIES}:
        raise ValueError("Unknown record type.")
    if field_type not in TYPE_KEYS:
        raise ValueError("Unknown field type.")
    key = key_from_label(label)
    if not key:
        raise ValueError("Give the field a name.")
    if key in RESERVED:
        raise ValueError(f"'{key}' is a built-in field name; choose another.")
    if session.query(CustomField).filter_by(entity=entity, key=key).first():
        raise ValueError(f"A field called '{key}' already exists.")
    opts = [o.strip() for o in (options or []) if o and o.strip()] if field_type in ("select", "multiselect") else None
    if field_type in ("select", "multiselect") and not opts:
        raise ValueError("Dropdown fields need at least one option.")
    pos = len(definitions(session, entity))
    f = CustomField(entity=entity, key=key, label=label.strip()[:200], field_type=field_type, options=opts,
                    is_required=bool(is_required), position=pos)
    session.add(f)
    session.flush()
    audit.record(session, actor, "create", "custom_field", f.id, {"entity": entity, "key": key, "type": field_type})
    return f


def coerce(field, raw):
    """Form / API value -> stored JSON value (None = clear). Raises ValueError on bad input."""
    t = field.field_type
    if t == "multiselect":
        values = raw if isinstance(raw, list) else [v for v in re.split(r"[;,]", raw or "")]
        values = [str(v).strip() for v in values if str(v).strip()]
        bad = [v for v in values if field.options and v not in field.options]
        if bad:
            raise ValueError(f"{field.label}: '{bad[0]}' is not one of the options.")
        return values or None
    if t == "checkbox":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("1", "on", "true", "yes") if raw not in (None, "") else None
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "":
        return None
    if t == "number":
        try:
            n = float(raw.replace(",", ""))
        except ValueError:
            raise ValueError(f"{field.label}: '{raw}' is not a number.")
        return int(n) if n.is_integer() else n
    if t == "date":
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError(f"{field.label}: '{raw}' is not a date (use YYYY-MM-DD).")
    if t == "select":
        if field.options and raw not in field.options:
            raise ValueError(f"{field.label}: '{raw}' is not one of the options.")
        return raw
    if t == "url" and "://" not in raw:
        return "https://" + raw
    return raw


def values_from_form(session, entity, form):
    """Read `cf_<key>` inputs into an attributes dict. Raises ValueError listing the first problem."""
    out = {}
    for f in definitions(session, entity):
        name = f"cf_{f.key}"
        if f.field_type == "multiselect":
            raw = form.getlist(name) if hasattr(form, "getlist") else form.get(name)
        elif f.field_type == "checkbox":
            raw = form.get(name) if name in form else False
        else:
            if name not in form:
                continue
            raw = form.get(name)
        value = coerce(f, raw)
        if f.is_required and value in (None, "", []):
            raise ValueError(f"{f.label} is required.")
        out[f.key] = value
    return out


def values_from_api(session, entity, data):
    """Like values_from_form for a JSON dict; unknown keys are kept as free-form text."""
    defs = {f.key: f for f in definitions(session, entity)}
    out = {}
    for k, v in (data or {}).items():
        f = defs.get(k)
        out[k] = coerce(f, v) if f else v
    return out
