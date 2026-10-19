"""Command-line maintenance: wipe data, seed a CSV, show counts.

    python -m app.manage status
    python -m app.manage reset            # shows what WOULD be deleted
    python -m app.manage reset --yes      # deletes campaigns, contacts, history, tags
    python -m app.manage reset --yes --everything   # ...plus do-not-contact list, templates, settings
    python -m app.manage seed seeds/ireland_hospitality_resend.csv --campaign "My campaign" [--replace] [--start]

Inside Docker:  docker compose exec web python -m app.manage <command>
"""
import argparse
import sys

from sqlalchemy import func

from app import importer, settings as app_settings
from app.db import SessionLocal, init_db
from app.models import (
    Campaign, CampaignStep, ClickLink, Contact, Event, Message, Setting, Suppression, Tag, Template, contact_tags,
)
from app.services import create_default_steps, delete_campaign, recompute_campaign, set_campaign_status

DATA_TABLES = [("events", Event), ("click links", ClickLink), ("messages", Message), ("contacts", Contact),
               ("campaign stages", CampaignStep), ("campaigns", Campaign), ("tags", Tag)]
EXTRA_TABLES = [("do-not-contact entries", Suppression), ("email templates", Template), ("settings", Setting)]


def counts(session, tables):
    return [(label, session.query(func.count()).select_from(model).scalar() or 0) for label, model in tables]


def cmd_status(session, args):
    for label, n in counts(session, DATA_TABLES + EXTRA_TABLES):
        print(f"{label:24} {n}")
    for c in session.query(Campaign).order_by(Campaign.id):
        n = session.query(func.count(Contact.id)).filter(Contact.campaign_id == c.id).scalar()
        print(f"  campaign #{c.id} [{c.status}] {c.name}: {n} contacts")


def cmd_reset(session, args):
    tables = DATA_TABLES + (EXTRA_TABLES if args.everything else [])
    for label, n in counts(session, tables):
        print(f"{'deleting' if args.yes else 'would delete':14} {n:6} {label}")
    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to do it. (Take a pg_dump backup first.)")
        return 0
    session.execute(contact_tags.delete())
    for _, model in tables:
        session.query(model).delete()
    session.commit()
    print("\nDone. The sending worker keeps running; with no contacts it simply idles.")
    return 0


def cmd_seed(session, args):
    with open(args.path, "rb") as fh:
        raw = fh.read()
    headers, rows = importer.read_table(args.path, raw)
    mapping = importer.guess_mapping(headers)
    parsed, errors = importer.parse_rows(rows, headers, mapping)
    name = args.campaign
    camp = session.query(Campaign).filter(func.lower(Campaign.name) == name.lower()).first()
    if camp and args.replace:
        n = delete_campaign(session, camp)
        session.flush()
        print(f"replaced existing campaign '{name}' ({n} contacts removed)")
        camp = None
    created_campaign = camp is None
    if camp is None:
        camp = Campaign(name=name, status="draft", is_active=False, description=args.description)
        session.add(camp)
        session.flush()
        create_default_steps(session, camp)
    plan, stats = importer.analyse(session, parsed, camp, args.mode)
    created, updated, locked = importer.apply_plan(session, camp, plan, app_settings.load(session))
    if args.start:
        set_campaign_status(camp, "active")
    session.flush()
    recompute_campaign(session, camp)
    session.commit()
    print(f"campaign #{camp.id} '{camp.name}' ({'created' if created_campaign else 'existing'}, {camp.status})")
    print(f"  contacts created {created}, updated {updated}; skipped 'do not send' rows {stats['skip_dnr']}; "
          f"with personalised emails {stats['customised']}")
    for e in errors:
        print(f"  problem on row {e['row']} {e.get('email', '')}: {e['error']}")
    if camp.status != "active":
        print("  Campaign is NOT started. Review it in the panel, send a test, then press Start.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.manage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show row counts")
    r = sub.add_parser("reset", help="delete campaigns, contacts and history")
    r.add_argument("--yes", action="store_true", help="really delete (otherwise only show counts)")
    r.add_argument("--everything", action="store_true", help="also wipe do-not-contact list, templates and settings")
    s = sub.add_parser("seed", help="import a CSV/XLSX into a campaign")
    s.add_argument("path")
    s.add_argument("--campaign", required=True, help="campaign name (created if missing)")
    s.add_argument("--replace", action="store_true", help="delete the campaign first if it already exists")
    s.add_argument("--start", action="store_true", help="start the campaign immediately (default: leave as draft)")
    s.add_argument("--mode", choices=("upsert", "create_only", "update_only"), default="upsert")
    s.add_argument("--description", default=None)
    args = p.parse_args(argv)

    init_db()
    session = SessionLocal()
    try:
        return {"status": cmd_status, "reset": cmd_reset, "seed": cmd_seed}[args.cmd](session, args) or 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
