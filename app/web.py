import io
from datetime import datetime, timedelta

from flask import Flask, Response, redirect, abort, request, jsonify, render_template, send_file
from sqlalchemy import text

from app.db import SessionLocal, init_db
from app.models import Event, ClickLink, Contact, Campaign
from app.importer import run_import, export_csv
from app.mailer import send_stage_email
import smtplib, ssl, imaplib
from app.config import SMTP_HOST, SMTP_PORT, IMAP_HOST, IMAP_PORT

app = Flask(__name__, static_folder="../static", template_folder="../templates")
init_db()

PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)

# ---------- tracking ----------

@app.route("/t/o/<token>.gif")
def track_open(token):
    session = SessionLocal()
    ev = session.query(Event).filter(Event.type == "open_token", Event.detail == token).first()
    if ev:
        session.add(Event(contact_id=ev.contact_id, stage=ev.stage, type="opened"))
        session.commit()
    session.close()
    return Response(PIXEL_GIF, mimetype="image/gif")

@app.route("/t/c/<token>")
def track_click(token):
    session = SessionLocal()
    link = session.query(ClickLink).filter(ClickLink.token == token).first()
    if not link:
        session.close()
        abort(404)
    session.add(Event(contact_id=link.contact_id, stage=link.stage, type="clicked"))
    session.commit()
    target = link.target_url
    session.close()
    return redirect(target, code=302)

@app.route("/t/u/<token>")
def unsubscribe(token):
    session = SessionLocal()
    ev = session.query(Event).filter(Event.type == "unsub_token", Event.detail == token).first()
    if not ev:
        session.close()
        abort(404)
    contact = session.query(Contact).get(ev.contact_id)
    contact.unsubscribed = True
    session.commit()
    session.close()
    return "You've been unsubscribed and won't receive further emails from this sequence."

# ---------- dashboard page ----------

@app.route("/")
def index():
    return render_template("index.html")

# ---------- health ----------

@app.route("/api/health")
def health():
    result = {}
    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
        result["database"] = "ok"
    except Exception as e:
        result["database"] = f"error: {e}"
    finally:
        session.close()

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=5) as s:
            s.starttls(context=ctx)
        result["smtp"] = "ok"
    except Exception as e:
        result["smtp"] = f"error: {e}"

    try:
        if IMAP_HOST:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=5)
            imap.logout()
            result["imap"] = "ok"
        else:
            result["imap"] = "not configured"
    except Exception as e:
        result["imap"] = f"error: {e}"

    session = SessionLocal()
    last_event = session.query(Event).order_by(Event.created_at.desc()).first()
    session.close()
    if last_event:
        age = datetime.utcnow() - last_event.created_at.replace(tzinfo=None)
        result["worker_last_activity_minutes_ago"] = round(age.total_seconds() / 60, 1)
    else:
        result["worker_last_activity_minutes_ago"] = None

    return jsonify(result)

# ---------- stats ----------

@app.route("/api/stats")
def stats():
    session = SessionLocal()
    campaign_id = request.args.get("campaign_id", type=int)
    q = session.query(Contact)
    if campaign_id:
        q = q.filter(Contact.campaign_id == campaign_id)
    contacts = q.all()
    ids = [c.id for c in contacts]

    def count(event_type):
        if not ids:
            return 0
        return session.query(Event).filter(Event.type == event_type, Event.contact_id.in_(ids)).count()

    opened_ids = {e.contact_id for e in session.query(Event).filter(Event.type == "opened").all()}
    clicked_ids = {e.contact_id for e in session.query(Event).filter(Event.type == "clicked").all()}

    result = {
        "total": len(contacts),
        "sent": sum(1 for c in contacts if c.stage >= 1),
        "opened": sum(1 for c in contacts if c.id in opened_ids),
        "clicked": sum(1 for c in contacts if c.id in clicked_ids),
        "replied": sum(1 for c in contacts if c.replied),
        "unsubscribed": sum(1 for c in contacts if c.unsubscribed),
    }
    session.close()
    return jsonify(result)

# ---------- campaigns ----------

@app.route("/api/campaigns", methods=["GET"])
def list_campaigns():
    session = SessionLocal()
    camps = session.query(Campaign).all()
    out = [{"id": c.id, "name": c.name, "contact_count": len(c.contacts)} for c in camps]
    session.close()
    return jsonify(out)

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    contact_ids = data.get("contact_ids", [])
    if not name:
        return jsonify({"error": "name is required"}), 400

    session = SessionLocal()
    if session.query(Campaign).filter(Campaign.name == name).first():
        session.close()
        return jsonify({"error": "campaign name already exists"}), 400

    camp = Campaign(name=name)
    session.add(camp)
    session.flush()

    moved = 0
    if contact_ids:
        contacts = session.query(Contact).filter(Contact.id.in_(contact_ids)).all()
        for c in contacts:
            c.campaign_id = camp.id
            moved += 1
    session.commit()
    out = {"id": camp.id, "name": camp.name, "contacts_assigned": moved}
    session.close()
    return jsonify(out)

# ---------- contacts ----------

@app.route("/api/contacts")
def list_contacts():
    session = SessionLocal()
    campaign_id = request.args.get("campaign_id", type=int)
    q = session.query(Contact)
    if campaign_id:
        q = q.filter(Contact.campaign_id == campaign_id)
    contacts = q.all()

    opened_ids = {e.contact_id for e in session.query(Event).filter(Event.type == "opened").all()}
    clicked_ids = {e.contact_id for e in session.query(Event).filter(Event.type == "clicked").all()}

    out = [{
        "id": c.id, "campaign_id": c.campaign_id, "company": c.company,
        "first_name": c.first_name, "last_name": c.last_name, "email": c.email,
        "priority": c.priority, "send_flag": c.send_flag, "needs_verification": c.needs_verification,
        "stage": c.stage, "opened": c.id in opened_ids, "clicked": c.id in clicked_ids,
        "replied": c.replied, "unsubscribed": c.unsubscribed,
    } for c in contacts]
    session.close()
    return jsonify(out)

@app.route("/api/contacts/import", methods=["POST"])
def import_contacts():
    dry_run = request.form.get("dry_run", "true").lower() == "true"
    campaign_id = request.form.get("campaign_id", type=int)
    campaign_name = request.form.get("campaign_name")
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "file is required"}), 400

    try:
        result = run_import(file.read().decode("utf-8"), dry_run, campaign_id, campaign_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/contacts/export")
def export_contacts():
    campaign_id = request.args.get("campaign_id", type=int)
    csv_text = export_csv(campaign_id)
    return send_file(
        io.BytesIO(csv_text.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="contacts_export.csv",
    )

@app.route("/api/contacts/demo")
def demo_csv():
    return send_file("../static/demo_contacts.csv", as_attachment=True, download_name="demo_contacts.csv")

@app.route("/api/contacts/<int:contact_id>/send", methods=["POST"])
def send_one(contact_id):
    """Manual send for testing — bypasses the scheduler. dry_run just previews the composed email."""
    dry_run = request.args.get("dry_run", "true").lower() == "true"
    session = SessionLocal()
    contact = session.query(Contact).get(contact_id)
    if not contact:
        session.close()
        return jsonify({"error": "not found"}), 404

    stage = contact.stage + 1
    subject = contact.subject
    body = {1: contact.body1, 2: contact.body2, 3: contact.body3}.get(stage)
    if not body:
        session.close()
        return jsonify({"error": f"no body configured for stage {stage}"}), 400

    if dry_run:
        session.close()
        return jsonify({"dry_run": True, "to": contact.email, "subject": subject, "body_preview": body[:500]})

    ok, msgid = send_stage_email(session, contact, stage, subject, body)
    if ok:
        contact.stage = stage
        if stage == 1:
            contact.message_id = msgid
    session.commit()
    session.close()
    return jsonify({"dry_run": False, "sent": ok})