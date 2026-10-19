import csv
import io
import re
from datetime import datetime

from app.db import SessionLocal, init_db
from app.models import Campaign, Contact
from app.services import add_tags

REQUIRED_COLS = ["email"]
VALID_SEND = {"send", "do not send", "verify email first", ""}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None

def normalize_send(value):
    v = (value or "").strip().lower()
    if v not in VALID_SEND:
        return None  # invalid
    return v or "send"

def get_or_create_campaign(session, campaign_id=None, campaign_name=None):
    if campaign_id:
        camp = session.query(Campaign).get(campaign_id)
        if not camp:
            raise ValueError(f"Campaign id {campaign_id} not found")
        return camp
    if campaign_name:
        camp = session.query(Campaign).filter(Campaign.name == campaign_name).first()
        if not camp:
            camp = Campaign(name=campaign_name)
            session.add(camp)
            session.flush()
        return camp
    raise ValueError("Either campaign_id or campaign_name is required")

def parse_csv_text(text):
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
    for col in REQUIRED_COLS:
        if col not in reader.fieldnames:
            raise ValueError(f"CSV missing required column: {col}")
    return list(reader)

def validate_rows(rows, session, campaign_id):
    """Returns (valid_rows, errors) without touching the DB."""
    errors = []
    valid = []
    existing_emails = set()
    if campaign_id:
        existing_emails = {
            c.email for c in session.query(Contact).filter(Contact.campaign_id == campaign_id).all()
        }

    for i, row in enumerate(rows, start=2):  # row 1 = header
        email = (row.get("email") or "").strip().lower()
        if not email:
            errors.append({"row": i, "error": "missing email"})
            continue
        if not EMAIL_RE.match(email):
            errors.append({"row": i, "email": email, "error": "invalid email format"})
            continue
        send_val = normalize_send(row.get("send"))
        if send_val is None:
            errors.append({"row": i, "email": email, "error": f"invalid 'send' value: {row.get('send')}"})
            continue
        fu1_date = parse_date(row.get("followup1_date"))
        if row.get("followup1_date") and not fu1_date:
            errors.append({"row": i, "email": email, "error": "followup1_date must be YYYY-MM-DD"})
            continue
        fu2_date = parse_date(row.get("followup2_date"))
        if row.get("followup2_date") and not fu2_date:
            errors.append({"row": i, "email": email, "error": "followup2_date must be YYYY-MM-DD"})
            continue

        valid.append({
            "row": i,
            "email": email,
            "company": row.get("company"),
            "first_name": row.get("first_name"),
            "last_name": row.get("last_name"),
            "priority": row.get("priority"),
            "send": send_val,
            "subject": row.get("subject"),
            "email1_body": row.get("email1_body"),
            "followup1_body": row.get("followup1_body"),
            "followup1_date": fu1_date,
            "followup2_body": row.get("followup2_body"),
            "followup2_date": fu2_date,
            "will_update": email in existing_emails,
            "phone": row.get("phone"),
            "notes": row.get("notes"),
            "tags": row.get("tags"),
        })
    return valid, errors

def apply_rows(session, campaign, valid_rows):
    created, updated = 0, 0
    for r in valid_rows:
        do_not_send = r["send"] == "do not send"
        existing = session.query(Contact).filter(
            Contact.campaign_id == campaign.id, Contact.email == r["email"]
        ).first()

        fields = dict(
            company=r["company"], first_name=r["first_name"], last_name=r["last_name"],
            phone=r["phone"], notes=r["notes"],
            priority=r["priority"], subject=r["subject"], body1=r["email1_body"],
            body2=r["followup1_body"], body2_date=r["followup1_date"],
            body3=r["followup2_body"], body3_date=r["followup2_date"],
            needs_verification=(r["send"] == "verify email first"),
        )
        if existing:
            if do_not_send:
                existing.send_flag = False
            else:
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.send_flag = True
            if r["tags"]:
                add_tags(session, existing, r["tags"])
            updated += 1
        else:
            if do_not_send:
                continue
            c = Contact(campaign_id=campaign.id, email=r["email"], send_flag=True, stage=0, **fields)
            session.add(c)
            session.flush()
            if r["tags"]:
                add_tags(session, c, r["tags"])
            created += 1
    session.commit()
    return created, updated

def export_csv(campaign_id=None):
    init_db()
    session = SessionLocal()
    try:
        q = session.query(Contact)
        if campaign_id:
            q = q.filter(Contact.campaign_id == campaign_id)
        contacts = q.all()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "company", "first_name", "last_name", "email", "phone", "priority", "send",
            "subject", "email1_body", "followup1_body", "followup1_date",
            "followup2_body", "followup2_date", "tags", "notes",
            "stage", "replied", "unsubscribed",
        ])
        for c in contacts:
            send_val = "Verify email first" if c.needs_verification else ("Send" if c.send_flag else "Do not send")
            writer.writerow([
                c.company, c.first_name, c.last_name, c.email, c.phone, c.priority, send_val,
                c.subject, c.body1, c.body2, c.body2_date or "", c.body3, c.body3_date or "",
                ",".join(sorted(t.name for t in c.tags)), c.notes,
                c.stage, c.replied, c.unsubscribed,
            ])
        return buf.getvalue()
    finally:
        session.close()

def run_import(csv_text, dry_run, campaign_id=None, campaign_name=None):
    init_db()
    session = SessionLocal()
    try:
        rows = parse_csv_text(csv_text)
        campaign = get_or_create_campaign(session, campaign_id, campaign_name)
        valid, errors = validate_rows(rows, session, campaign.id)

        if dry_run:
            session.rollback()
            return {
                "dry_run": True,
                "campaign_id": campaign.id,
                "campaign_name": campaign.name,
                "to_create": sum(1 for r in valid if not r["will_update"]),
                "to_update": sum(1 for r in valid if r["will_update"]),
                "errors": errors,
            }

        created, updated = apply_rows(session, campaign, valid)
        return {
            "dry_run": False,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "created": created,
            "updated": updated,
            "errors": errors,
        }
    finally:
        session.close()