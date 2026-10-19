"""
One-time seed from the original messy Google-Sheet-shaped CSV export.
Usage: docker compose run --rm web python -m app.seed /app/seed.csv "Ireland Hospitality Oct 2026"
"""
import sys
import csv
import io
from datetime import timedelta
from dateutil import parser as dateparser

from app.db import SessionLocal, init_db
from app.models import Campaign, Contact

def find_header_row(lines):
    for i, line in enumerate(lines):
        if "Company" in line and "Email" in line:
            return i
    raise ValueError("Could not find header row")

def seed(path, campaign_name):
    with open(path, encoding="utf-8") as f:
        raw_lines = f.readlines()
    header_idx = find_header_row(raw_lines)
    reader = csv.DictReader(io.StringIO("".join(raw_lines[header_idx:])))

    init_db()
    session = SessionLocal()
    campaign = session.query(Campaign).filter(Campaign.name == campaign_name).first()
    if not campaign:
        campaign = Campaign(name=campaign_name)
        session.add(campaign)
        session.flush()

    created = 0
    for row in reader:
        email = (row.get("Email") or "").strip().lower()
        if not email or "@" not in email:
            continue

        send_status = (row.get("Send?") or "").strip().lower()
        if send_status.startswith("do not"):
            continue
        needs_verification = send_status.startswith("verify")

        # the "send date" column has an unstable header name (it shows a sample
        # date like "Fri 16 Oct"), so find it positionally: it's the 4th column.
        fieldnames = reader.fieldnames
        send_date_col = fieldnames[3] if len(fieldnames) > 3 else None
        send_date_raw = row.get(send_date_col, "")
        send_date = None
        try:
            send_date = dateparser.parse(send_date_raw, fuzzy=True).date() if send_date_raw else None
        except Exception:
            pass

        fu2_date = None
        try:
            fu2_raw = row.get("Follow-up 2 date", "")
            fu2_date = dateparser.parse(fu2_raw, fuzzy=True).date() if fu2_raw else None
        except Exception:
            pass

        # Follow-up 1 date isn't present in this export — default to 2 days after send date.
        fu1_date = (send_date + timedelta(days=2)) if send_date else None

        existing = session.query(Contact).filter(
            Contact.campaign_id == campaign.id, Contact.email == email
        ).first()
        if existing:
            continue  # seed never overwrites — re-run importer for updates

        session.add(Contact(
            campaign_id=campaign.id,
            email=email,
            company=row.get("Company"),
            first_name=row.get("First name"),
            last_name=row.get("Last name"),
            priority=row.get("Priority"),
            send_flag=not needs_verification,
            needs_verification=needs_verification,
            subject=row.get("Subject line"),
            body1=row.get("Email 1"),
            body2=row.get("Follow-up 1"),
            body2_date=fu1_date,
            body3=row.get("Follow-up 2"),
            body3_date=fu2_date,
            stage=0,
        ))
        created += 1

    session.commit()
    print(f"Seeded {created} contacts into campaign '{campaign_name}' (id={campaign.id})")
    session.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m app.seed <path-to-old-csv> <campaign-name>")
        sys.exit(1)
    seed(sys.argv[1], sys.argv[2])