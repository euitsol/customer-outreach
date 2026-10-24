"""Merge fields: {{first_name}}, {{company|your business}}, custom fields, any imported column."""
import re

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_ .\-]+?)\s*(?:\|([^}]*))?\}\}")

BUILTIN_VARS = ["first_name", "last_name", "full_name", "company", "job_title", "email", "phone",
                "company_website", "company_domain", "sender_name"]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _flat(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def context_for(enrollment, sender_name=""):
    """Values for one recipient. Precedence (low -> high): company custom fields, person custom
    fields, the campaign's per-enrollment variables, built-ins."""
    person = getattr(enrollment, "person", None)
    company = person.company if person is not None else None
    ctx = {}
    if company is not None:
        ctx.update({slug(k): _flat(v) for k, v in (company.attributes or {}).items()})
        if company.website:
            ctx.setdefault("website", company.website)
    if person is not None:
        ctx.update({slug(k): _flat(v) for k, v in (person.attributes or {}).items()})
    ctx.update({slug(k): _flat(v) for k, v in enrollment.custom_fields.items()})
    ctx.update({
        "first_name": (person.first_name if person else None) or "",
        "last_name": (person.last_name if person else None) or "",
        "full_name": person.full_name if person else "",
        "company": company.name if company else "",
        "job_title": (person.job_title if person else None) or "",
        "email": enrollment.email or (person.email if person else "") or "",
        "phone": (person.phone if person else None) or "",
        "company_website": (company.website if company else None) or "",
        "company_domain": (company.domain if company else None) or "",
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
