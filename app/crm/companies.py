"""Companies (accounts)."""
from sqlalchemy import func

from app.crm import audit, normalize
from app.models import Company

FIELDS = ["name", "domain", "website", "industry", "size", "phone", "country", "city", "address", "description",
          "owner_id"]
SIZES = ["1-10", "11-50", "51-200", "201-500", "501-1000", "1000+"]


def find(session, name=None, domain=None):
    if domain:
        c = session.query(Company).filter(Company.domain == domain).first()
        if c:
            return c
    if name and name.strip():
        return (session.query(Company).filter(func.lower(Company.name) == name.strip().lower())
                .order_by(Company.id).first())
    return None


def get_or_create(session, name=None, website=None, email=None, actor=None):
    """Match on domain (from website, else a non-freemail address), then on name; create if new."""
    domain = normalize.domain_from_url(website) or normalize.company_domain_from_email(email)
    name = (name or "").strip() or None
    company = find(session, name, domain)
    if company:
        if website and not company.website:
            company.website = website[:500]
        if domain and not company.domain and not find(session, domain=domain):
            company.domain = domain
        return company, False
    if not name and not (website and domain):
        return None, False      # an email domain alone is not enough to invent a company
    company = Company(name=(name or domain)[:300], domain=domain, website=(website or None) and website[:500],
                      attributes={})
    session.add(company)
    session.flush()
    audit.record(session, actor, "create", "company", company.id, {"name": company.name, "domain": domain})
    return company, True


def update(session, company, data, actor=None):
    before = audit.diff(company, FIELDS + ["attributes"])
    for f in FIELDS:
        if f in data:
            v = data[f]
            if isinstance(v, str):
                v = v.strip() or None
            if f == "domain" and v:
                v = normalize.domain_from_url(v)
                clash = session.query(Company).filter(Company.domain == v, Company.id != company.id).first()
                if clash:
                    raise ValueError(f"Domain {v} already belongs to '{clash.name}'.")
            if f == "name" and not v:
                raise ValueError("Company name is required.")
            setattr(company, f, v)
    if "website" in data and data["website"] and not company.domain:
        d = normalize.domain_from_url(data["website"])
        if d and not session.query(Company).filter(Company.domain == d, Company.id != company.id).first():
            company.domain = d
    if "attributes" in data and data["attributes"] is not None:
        company.attributes = {**(company.attributes or {}), **data["attributes"]}
    session.flush()
    delta = audit.changes(before, audit.diff(company, FIELDS + ["attributes"]))
    if delta:
        audit.record(session, actor, "update", "company", company.id, delta)
    return delta


def delete(session, company, actor=None):
    audit.record(session, actor, "delete", "company", company.id, {"name": company.name})
    for p in list(company.people):
        p.company_id = None
    session.delete(company)
