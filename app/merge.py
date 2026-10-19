"""Merge fields: {{first_name}}, {{company|your business}}, any CSV column name."""
import re

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_ .\-]+?)\s*(?:\|([^}]*))?\}\}")

BUILTIN_VARS = ["first_name", "last_name", "full_name", "company", "email", "phone", "sender_name"]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def context_for(contact, sender_name=""):
    ctx = {slug(k): ("" if v is None else str(v)) for k, v in contact.custom_fields.items()}
    ctx.update({
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "full_name": contact.full_name,
        "company": contact.company or "",
        "email": contact.email or "",
        "phone": contact.phone or "",
        "sender_name": sender_name or "",
    })
    return ctx


def render(text, ctx):
    if not text:
        return text or ""

    def repl(m):
        key = slug(m.group(1))
        fallback = (m.group(2) or "").strip()
        val = ctx.get(key, "")
        return val.strip() if val and val.strip() else fallback
    return VAR_RE.sub(repl, text)


def find_vars(text):
    return {slug(m.group(1)) for m in VAR_RE.finditer(text or "")}


def unknown_vars(text, known):
    return sorted(v for v in find_vars(text) if v not in known)
