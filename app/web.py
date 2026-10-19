import io
import re
from datetime import datetime, date

from flask import Flask, Response, redirect, abort, request, jsonify, render_template, send_file
from sqlalchemy import text, func
from sqlalchemy.orm import joinedload

from app.db import SessionLocal, init_db
from app.models import Event, ClickLink, Contact, Campaign, Tag, contact_tags
from app.importer import run_import, export_csv, EMAIL_RE
from app.mailer import send_stage_email
from app.services import (
    add_tags, remove_tags, set_tags, delete_contacts, next_step, parse_tag_names
)
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
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=5, context=ctx) as s:
                pass
        else:
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

# ---------- helpers ----------

TEXT_FIELDS = ["company", "first_name", "last_name", "email", "phone", "priority",
               "notes", "subject", "body1", "body2", "body3"]
BOOL_FIELDS = ["send_flag", "needs_verification", "unsubscribed", "replied"]
LONG_FIELDS = {"body1", "body2", "body3", "notes"}


def iso(dt):
    return dt.isoformat() + "Z" if dt else None  # DB timestamps are UTC

def parse_date(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD")

def metrics(session):
    """(sent, opened, clicked) -> {contact_id: (count, first, last)}"""
    def grouped(event_type):
        rows = (session.query(Event.contact_id, func.count(Event.id),
                              func.min(Event.created_at), func.max(Event.created_at))
                .filter(Event.type == event_type).group_by(Event.contact_id).all())
        return {cid: (n, first, last) for cid, n, first, last in rows}
    return grouped("sent"), grouped("opened"), grouped("clicked")

def serialize_contact(c, m, camp_names):
    sent, opened, clicked = m
    s, o, k = sent.get(c.id), opened.get(c.id), clicked.get(c.id)
    return {
        "id": c.id, "campaign_id": c.campaign_id, "campaign": camp_names.get(c.campaign_id, ""),
        "company": c.company, "first_name": c.first_name, "last_name": c.last_name,
        "email": c.email, "phone": c.phone, "priority": c.priority, "notes": c.notes,
        "send_flag": bool(c.send_flag), "needs_verification": bool(c.needs_verification),
        "queued": bool(c.queued), "stage": c.stage or 0,
        "replied": bool(c.replied), "unsubscribed": bool(c.unsubscribed),
        "tags": sorted(t.name for t in c.tags),
        "sent_count": s[0] if s else 0, "last_sent": iso(s[2]) if s else None,
        "open_count": o[0] if o else 0,
        "first_opened": iso(o[1]) if o else None, "last_opened": iso(o[2]) if o else None,
        "click_count": k[0] if k else 0,
    }

def apply_fields(c, data):
    for f in TEXT_FIELDS:
        if f in data:
            v = data[f]
            if isinstance(v, str) and f not in LONG_FIELDS:
                v = v.strip()
            setattr(c, f, v)
    for f in ("body2_date", "body3_date"):
        if f in data:
            setattr(c, f, parse_date(data[f]))
    for f in BOOL_FIELDS:
        if f in data:
            setattr(c, f, bool(data[f]))
    if data.get("campaign_id"):
        c.campaign_id = int(data["campaign_id"])
    if "stage" in data:
        c.stage = max(0, min(3, int(data["stage"])))


# ---------- stats ----------

@app.route("/api/stats")
def stats():
    session = SessionLocal()
    try:
        campaign_id = request.args.get("campaign_id", type=int)
        q = session.query(Contact)
        if campaign_id:
            q = q.filter(Contact.campaign_id == campaign_id)
        contacts = q.all()
        ids = {c.id for c in contacts}
        sent, opened, clicked = metrics(session)
        failed = sum(1 for (cid,) in session.query(Event.contact_id).filter(Event.type == "failed") if cid in ids)

        contacted = [c for c in contacts if (c.stage or 0) >= 1]
        opened_n = sum(1 for c in contacted if c.id in opened)
        result = {
            "total": len(contacts),
            "ready_to_send": sum(1 for c in contacts if (c.stage or 0) == 0 and c.send_flag
                                 and not c.unsubscribed and not c.replied and not c.needs_verification),
            "needs_verification": sum(1 for c in contacts if c.needs_verification),
            "contacted": len(contacted),
            "emails_sent": sum(sent[c.id][0] for c in contacts if c.id in sent),
            "failed": failed,
            "opened": opened_n,
            "not_opened": len(contacted) - opened_n,
            "total_opens": sum(opened[c.id][0] for c in contacts if c.id in opened),
            "open_rate": f"{round(100 * opened_n / len(contacted), 1)}%" if contacted else "0%",
            "clicked": sum(1 for c in contacts if c.id in clicked),
            "replied": sum(1 for c in contacts if c.replied),
            "unsubscribed": sum(1 for c in contacts if c.unsubscribed),
        }
        return jsonify(result)
    finally:
        session.close()


# ---------- campaigns ----------

def _blank():
    return dict(contact_count=0, contacted=0, emails_sent=0, opened=0, not_opened=0,
                total_opens=0, clicked=0, replied=0, unsubscribed=0, ready=0, needs_verification=0)

@app.route("/api/campaigns", methods=["GET"])
def list_campaigns():
    session = SessionLocal()
    try:
        sent, opened, clicked = metrics(session)
        agg = {}
        for c in session.query(Contact).all():
            a = agg.setdefault(c.campaign_id, _blank())
            a["contact_count"] += 1
            if (c.stage or 0) >= 1:
                a["contacted"] += 1
                if c.id in opened:
                    a["opened"] += 1
                else:
                    a["not_opened"] += 1
            a["emails_sent"] += sent[c.id][0] if c.id in sent else 0
            a["total_opens"] += opened[c.id][0] if c.id in opened else 0
            a["clicked"] += 1 if c.id in clicked else 0
            a["replied"] += 1 if c.replied else 0
            a["unsubscribed"] += 1 if c.unsubscribed else 0
            a["needs_verification"] += 1 if c.needs_verification else 0
            if (c.stage or 0) == 0 and c.send_flag and not c.unsubscribed and not c.replied \
                    and not c.needs_verification and c.body1:
                a["ready"] += 1
        out = []
        for camp in session.query(Campaign).order_by(Campaign.id).all():
            a = agg.get(camp.id, _blank())
            a["open_rate"] = round(100 * a["opened"] / a["contacted"], 1) if a["contacted"] else 0
            out.append({"id": camp.id, "name": camp.name, "is_active": bool(camp.is_active), **a})
        return jsonify(out)
    finally:
        session.close()

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    contact_ids = data.get("contact_ids", [])
    if not name:
        return jsonify({"error": "name is required"}), 400
    session = SessionLocal()
    try:
        if session.query(Campaign).filter(Campaign.name == name).first():
            return jsonify({"error": "campaign name already exists"}), 400
        camp = Campaign(name=name, is_active=False)
        session.add(camp)
        session.flush()
        moved = 0
        if contact_ids:
            for c in session.query(Contact).filter(Contact.id.in_(contact_ids)).all():
                c.campaign_id = camp.id
                moved += 1
        session.commit()
        return jsonify({"id": camp.id, "name": camp.name, "contacts_assigned": moved})
    finally:
        session.close()

@app.route("/api/campaigns/<int:cid>", methods=["PUT"])
def update_campaign(cid):
    data = request.get_json(force=True)
    session = SessionLocal()
    try:
        camp = session.get(Campaign, cid)
        if not camp:
            return jsonify({"error": "not found"}), 404
        if "name" in data:
            name = (data["name"] or "").strip()
            if not name:
                return jsonify({"error": "name is required"}), 400
            clash = session.query(Campaign).filter(Campaign.name == name, Campaign.id != cid).first()
            if clash:
                return jsonify({"error": "campaign name already exists"}), 400
            camp.name = name
        if "is_active" in data:
            camp.is_active = bool(data["is_active"])
        session.commit()
        return jsonify({"id": camp.id, "name": camp.name, "is_active": camp.is_active})
    finally:
        session.close()

@app.route("/api/campaigns/<int:cid>", methods=["DELETE"])
def delete_campaign(cid):
    session = SessionLocal()
    try:
        camp = session.get(Campaign, cid)
        if not camp:
            return jsonify({"error": "not found"}), 404
        ids = [i for (i,) in session.query(Contact.id).filter(Contact.campaign_id == cid)]
        n = delete_contacts(session, ids)
        session.flush()
        session.expire(camp)
        session.delete(camp)
        session.commit()
        return jsonify({"deleted_contacts": n})
    finally:
        session.close()

@app.route("/api/campaigns/<int:cid>/send_now", methods=["POST"])
def campaign_send_now(cid):
    """Queue the FIRST email for every ready contact in this campaign, regardless of paused/active."""
    session = SessionLocal()
    try:
        n = 0
        for c in session.query(Contact).filter(Contact.campaign_id == cid).all():
            if (c.stage or 0) == 0 and c.body1 and c.send_flag and not c.unsubscribed \
                    and not c.replied and not c.needs_verification:
                c.queued = True
                n += 1
        session.commit()
        return jsonify({"queued": n})
    finally:
        session.close()


# ---------- tags ----------

@app.route("/api/tags")
def list_tags():
    session = SessionLocal()
    try:
        return jsonify([{"id": t.id, "name": t.name, "count": len(t.contacts)}
                        for t in session.query(Tag).order_by(Tag.name).all()])
    finally:
        session.close()

@app.route("/api/tags/<int:tag_id>", methods=["DELETE"])
def delete_tag(tag_id):
    session = SessionLocal()
    try:
        t = session.get(Tag, tag_id)
        if not t:
            return jsonify({"error": "not found"}), 404
        session.delete(t)
        session.commit()
        return jsonify({"ok": True})
    finally:
        session.close()


# ---------- contacts ----------

@app.route("/api/contacts", methods=["GET"])
def list_contacts():
    session = SessionLocal()
    try:
        q = session.query(Contact).options(joinedload(Contact.tags))
        campaign_id = request.args.get("campaign_id", type=int)
        if campaign_id:
            q = q.filter(Contact.campaign_id == campaign_id)
        contacts = q.order_by(Contact.id.desc()).all()
        m = metrics(session)
        names = {c.id: c.name for c in session.query(Campaign).all()}
        return jsonify([serialize_contact(c, m, names) for c in contacts])
    finally:
        session.close()

@app.route("/api/contacts", methods=["POST"])
def create_contact():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        return jsonify({"error": "valid email is required"}), 400
    session = SessionLocal()
    try:
        camp = session.get(Campaign, int(data.get("campaign_id") or 0))
        if not camp:
            return jsonify({"error": "pick a campaign"}), 400
        if session.query(Contact).filter(Contact.campaign_id == camp.id, Contact.email == email).first():
            return jsonify({"error": "this email already exists in that campaign"}), 400
        c = Contact(campaign_id=camp.id, email=email, stage=0, send_flag=True,
                    needs_verification=False, queued=False)
        data["email"] = email
        apply_fields(c, data)
        session.add(c)
        session.flush()
        set_tags(session, c, data.get("tags", ""))
        session.commit()
        return jsonify({"id": c.id})
    except ValueError as e:
        session.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()

@app.route("/api/contacts/<int:contact_id>", methods=["GET"])
def get_contact(contact_id):
    session = SessionLocal()
    try:
        c = session.get(Contact, contact_id)
        if not c:
            return jsonify({"error": "not found"}), 404
        names = {x.id: x.name for x in session.query(Campaign).all()}
        d = serialize_contact(c, metrics(session), names)
        d.update({
            "subject": c.subject, "body1": c.body1, "body2": c.body2, "body3": c.body3,
            "body2_date": c.body2_date.isoformat() if c.body2_date else "",
            "body3_date": c.body3_date.isoformat() if c.body3_date else "",
        })
        return jsonify(d)
    finally:
        session.close()

@app.route("/api/contacts/<int:contact_id>", methods=["PUT"])
def update_contact(contact_id):
    data = request.get_json(force=True)
    session = SessionLocal()
    try:
        c = session.get(Contact, contact_id)
        if not c:
            return jsonify({"error": "not found"}), 404
        if data.get("campaign_id") and not session.get(Campaign, int(data["campaign_id"])):
            return jsonify({"error": "campaign not found"}), 400
        if "email" in data:
            email = (data["email"] or "").strip().lower()
            if not EMAIL_RE.match(email):
                return jsonify({"error": "invalid email"}), 400
            clash = session.query(Contact).filter(
                Contact.campaign_id == int(data.get("campaign_id") or c.campaign_id),
                Contact.email == email, Contact.id != c.id).first()
            if clash:
                return jsonify({"error": "this email already exists in that campaign"}), 400
            data["email"] = email
        apply_fields(c, data)
        if "tags" in data:
            set_tags(session, c, data["tags"])
        session.commit()
        return jsonify({"id": contact_id})
    except ValueError as e:
        session.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        session.close()

@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
def delete_contact(contact_id):
    session = SessionLocal()
    try:
        n = delete_contacts(session, [contact_id])
        session.commit()
        return jsonify({"deleted": n})
    finally:
        session.close()

@app.route("/api/contacts/<int:contact_id>/events")
def contact_events(contact_id):
    session = SessionLocal()
    try:
        evs = (session.query(Event)
               .filter(Event.contact_id == contact_id,
                       Event.type.in_(["sent", "failed", "opened", "clicked", "replied"]))
               .order_by(Event.created_at.desc()).limit(200).all())
        return jsonify([{"type": e.type, "stage": e.stage, "detail": e.detail if e.type == "failed" else None,
                         "at": iso(e.created_at)} for e in evs])
    finally:
        session.close()

@app.route("/api/contacts/bulk", methods=["POST"])
def bulk_contacts():
    data = request.get_json(force=True)
    ids = [int(i) for i in data.get("contact_ids", [])]
    action = data.get("action")
    if not ids:
        return jsonify({"error": "no contacts selected"}), 400
    session = SessionLocal()
    try:
        contacts = session.query(Contact).filter(Contact.id.in_(ids)).all()
        n = skipped = 0
        if action == "delete":
            n = delete_contacts(session, ids)
        elif action == "move":
            camp = session.get(Campaign, int(data.get("campaign_id") or 0))
            if not camp:
                return jsonify({"error": "campaign not found"}), 400
            for c in contacts:
                c.campaign_id = camp.id
                n += 1
        elif action in ("add_tags", "remove_tags"):
            if not parse_tag_names(data.get("tags", "")):
                return jsonify({"error": "no tags given"}), 400
            for c in contacts:
                (add_tags if action == "add_tags" else remove_tags)(session, c, data["tags"])
                n += 1
        elif action == "queue":
            today = date.today()
            for c in contacts:
                if c.send_flag and not c.unsubscribed and not c.replied \
                        and not c.needs_verification and next_step(c, today, force=True):
                    c.queued = True
                    n += 1
                else:
                    skipped += 1
        elif action == "unqueue":
            for c in contacts:
                c.queued = False
                n += 1
        elif action in ("enable", "disable"):
            for c in contacts:
                c.send_flag = (action == "enable")
                n += 1
        elif action in ("verify", "unverify"):
            for c in contacts:
                c.needs_verification = (action == "unverify")
                n += 1
        elif action == "reset":
            for c in contacts:
                c.stage, c.message_id, c.replied, c.queued = 0, None, False, False
                n += 1
        elif action in ("unsubscribe", "resubscribe"):
            for c in contacts:
                c.unsubscribed = (action == "unsubscribe")
                n += 1
        else:
            return jsonify({"error": f"unknown action: {action}"}), 400
        session.commit()
        return jsonify({"action": action, "affected": n, "skipped": skipped})
    finally:
        session.close()

@app.route("/api/contacts/import", methods=["POST"])
def import_contacts():
    dry_run = request.form.get("dry_run", "true").lower() == "true"
    campaign_id = request.form.get("campaign_id", type=int)
    campaign_name = request.form.get("campaign_name")
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "file is required"}), 400
    try:
        result = run_import(file.read().decode("utf-8-sig"), dry_run, campaign_id, campaign_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/contacts/export")
def export_contacts():
    campaign_id = request.args.get("campaign_id", type=int)
    csv_text = export_csv(campaign_id)
    return send_file(io.BytesIO(csv_text.encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name="contacts_export.csv")

@app.route("/api/contacts/demo")
def demo_csv():
    return send_file("../static/demo_contacts.csv", as_attachment=True, download_name="demo_contacts.csv")

@app.route("/api/contacts/<int:contact_id>/send", methods=["POST"])
def send_one(contact_id):
    """Immediate send of the NEXT email for one contact (ignores follow-up dates).
    dry_run=true only previews."""
    dry_run = request.args.get("dry_run", "true").lower() == "true"
    session = SessionLocal()
    try:
        c = session.get(Contact, contact_id)
        if not c:
            return jsonify({"error": "not found"}), 404
        if c.unsubscribed:
            return jsonify({"error": "contact is unsubscribed"}), 400
        step = next_step(c, date.today(), force=True)
        if not step:
            return jsonify({"error": "nothing left to send (sequence finished or no body written)"}), 400
        stage, subject, body = step
        if dry_run:
            return jsonify({"dry_run": True, "to": c.email, "stage": stage,
                            "subject": subject, "body_preview": body[:800]})
        ok, msgid = send_stage_email(session, c, stage, subject, body)
        if ok:
            c.stage = stage
            if stage == 1:
                c.message_id = msgid
        c.queued = False
        session.commit()
        return jsonify({"dry_run": False, "sent": ok, "stage": stage})
    finally:
        session.close()


# ---------- send log ----------

@app.route("/api/log")
def send_log():
    session = SessionLocal()
    try:
        campaign_id = request.args.get("campaign_id", type=int)
        limit = request.args.get("limit", 5000, type=int)

        q = (session.query(Event, Contact, Campaign)
             .join(Contact, Event.contact_id == Contact.id)
             .join(Campaign, Contact.campaign_id == Campaign.id)
             .filter(Event.type.in_(["sent", "failed"])))
        if campaign_id:
            q = q.filter(Contact.campaign_id == campaign_id)
        rows = q.order_by(Event.created_at.desc()).limit(limit).all()

        def per_stage(event_type):
            g = (session.query(Event.contact_id, Event.stage, func.count(Event.id),
                               func.min(Event.created_at), func.max(Event.created_at))
                 .filter(Event.type == event_type)
                 .group_by(Event.contact_id, Event.stage).all())
            return {(cid, st): (n, first, last) for cid, st, n, first, last in g}

        opens, clicks = per_stage("opened"), per_stage("clicked")
        tagmap = {}
        for cid, name in session.query(contact_tags.c.contact_id, Tag.name).filter(Tag.id == contact_tags.c.tag_id):
            tagmap.setdefault(cid, []).append(name)

        out = []
        for ev, c, camp in rows:
            o, k = opens.get((c.id, ev.stage)), clicks.get((c.id, ev.stage))
            is_sent = ev.type == "sent"
            out.append({
                "id": ev.id, "sent_at": iso(ev.created_at), "type": ev.type,
                "contact_id": c.id, "email": c.email, "company": c.company,
                "name": f"{c.first_name or ''} {c.last_name or ''}".strip(),
                "campaign_id": camp.id, "campaign": camp.name,
                "tags": sorted(tagmap.get(c.id, [])), "stage": ev.stage,
                "subject": ev.detail if is_sent else "",
                "error": "" if is_sent else (ev.detail or ""),
                "opened": bool(o) and is_sent,
                "open_count": o[0] if (o and is_sent) else 0,
                "first_opened": iso(o[1]) if (o and is_sent) else None,
                "last_opened": iso(o[2]) if (o and is_sent) else None,
                "click_count": k[0] if (k and is_sent) else 0,
                "replied": bool(c.replied),
            })
        return jsonify(out)
    finally:
        session.close()