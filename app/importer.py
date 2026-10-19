import tempfile
import requests
from app.models import Campaign, Contact

def fetch_sheet_to_tempfile(sheet_id):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.write(resp.content)
    tmp.close()
    return tmp.name

def get_or_create_campaign(session, name):
    campaign = session.query(Campaign).filter(Campaign.name == name).first()
    if not campaign:
        campaign = Campaign(name=name)
        session.add(campaign)
        session.flush()
    return campaign

def import_file(path, campaign_name):
    header_row = find_header_row(path)
    df = pd.read_excel(path, header=header_row)
    df = df.dropna(how="all")

    for col in REQUIRED_COLS:
        if col not in df.columns:
            raise ValueError(f"Missing expected column: {col}")

    init_db()
    session = SessionLocal()
    campaign = get_or_create_campaign(session, campaign_name)

    this_year = date.today().year
    created, updated = 0, 0

    for _, row in df.iterrows():
        email = str(row.get("Email", "")).strip().lower()
        if not email or "@" not in email:
            continue

        send_status = str(row.get("Send?", "")).strip().lower()
        do_not_send = send_status.startswith("do not")
        needs_verification = send_status.startswith("verify")
        send_flag = (send_status == "send") or needs_verification

        existing = session.query(Contact).filter(
            Contact.campaign_id == campaign.id, Contact.email == email
        ).first()

        fields = dict(
            company=row.get("Company"),
            first_name=row.get("First name"),
            last_name=row.get("Last name"),
            priority=row.get("Priority"),
            subject=row.get("Subject line"),
            body1=row.get("Email 1"),
            body2=row.get("Follow-up 1"),
            body2_date=parse_sheet_date(row.get("Follow-up 1 date"), this_year),
            body3=row.get("Follow-up 2"),
            body3_date=parse_sheet_date(row.get("Follow-up 2 date"), this_year),
            needs_verification=needs_verification,
        )

        if existing:
            if do_not_send:
                existing.send_flag = False  # stop future stages, never delete history
            else:
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.send_flag = send_flag
            updated += 1
        else:
            if do_not_send:
                continue
            c = Contact(campaign_id=campaign.id, email=email, send_flag=send_flag, stage=0, **fields)
            session.add(c)
            created += 1

    session.commit()
    print(f"Campaign '{campaign_name}': {created} new, {updated} updated.")
    session.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m app.importer <path-to-xlsx> <campaign-name>")
        sys.exit(1)
    import_file(sys.argv[1], sys.argv[2])