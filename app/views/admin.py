import hmac
import imaplib
import time
from zoneinfo import ZoneInfo, available_timezones

from flask import (
    Blueprint, jsonify, redirect, render_template, request, session as flask_session, url_for,
)
from sqlalchemy import func, text
from werkzeug.security import check_password_hash

from app import config, mailer, settings as app_settings
from app.clock import utcnow
from app.mailer import SendError
from app.models import Suppression, Tag, Template, User, company_tags, person_tags
from app.services import EMAIL_RE, add_suppression
from app.views.common import db, err, form_bool, get_or_404, int_arg, ok, paginate, safe_next, settings_now

bp = Blueprint("admin", __name__)

_attempts = {}  # ip -> [timestamps]; per gunicorn worker, good enough to slow brute force


# ------------------------------------------------------------------ auth + health

def login_required():
    """True when the panel needs a sign-in: an env admin password or at least one active user."""
    if config.ADMIN_PASSWORD:
        return True
    try:
        return db().query(User.id).filter(User.is_active == True).first() is not None  # noqa: E712
    except Exception:
        db().rollback()
        return False


def check_user(username, password):
    u = db().query(User).filter(func.lower(User.email) == (username or "").strip().lower(),
                                User.is_active == True).first()  # noqa: E712
    if u and u.password_hash and check_password_hash(u.password_hash, password or ""):
        return u
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not login_required():
        return redirect(url_for("dashboard.index"))
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    recent = [t for t in _attempts.get(ip, []) if time.time() - t < 600]
    _attempts[ip] = recent
    if request.method == "POST":
        if len(recent) >= 10:
            err("Too many attempts. Wait a few minutes.")
        else:
            username, password = request.form.get("username", ""), request.form.get("password", "")
            user = check_user(username, password)
            env_ok = bool(config.ADMIN_PASSWORD) and hmac.compare_digest(username, config.ADMIN_USER) \
                and hmac.compare_digest(password, config.ADMIN_PASSWORD)
            if user or env_ok:
                flask_session.clear()
                flask_session["auth"] = True
                flask_session["login"] = user.email if user else config.ADMIN_USER
                if user:
                    flask_session["uid"] = user.id
                    user.last_login_at = utcnow()
                    db().commit()
                flask_session.permanent = True
                _attempts.pop(ip, None)
                return redirect(safe_next(url_for("dashboard.index")))
            _attempts.setdefault(ip, []).append(time.time())
            time.sleep(1)
            err("Wrong username or password.")
    return render_template("login.html")


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    flask_session.clear()
    return redirect(url_for("admin.login"))


@bp.route("/healthz")
def healthz():
    try:
        db().execute(text("SELECT 1"))
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="error", detail=str(e)), 503


# ------------------------------------------------------------------ settings

def _int(name, default, lo=0, hi=10**7):
    try:
        return min(hi, max(lo, int(request.form.get(name, default))))
    except (TypeError, ValueError):
        return default


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    sess = db()
    s = settings_now()
    if request.method == "POST":
        tzname = (request.form.get("timezone") or s["timezone"]).strip()
        try:
            ZoneInfo(tzname)
        except Exception:
            err(f"Unknown timezone '{tzname}'.")
            tzname = s["timezone"]
        values = {
            "sending_paused": form_bool("sending_paused"),
            "min_delay": _int("min_delay", s["min_delay"]),
            "max_delay": _int("max_delay", s["max_delay"]),
            "daily_limit": _int("daily_limit", s["daily_limit"], 0, 100000),
            "poll_interval": _int("poll_interval", s["poll_interval"], 10, 3600),
            "window_enabled": form_bool("window_enabled"),
            "window_start": request.form.get("window_start") or s["window_start"],
            "window_end": request.form.get("window_end") or s["window_end"],
            "weekdays_only": form_bool("weekdays_only"),
            "timezone": tzname,
            "retry_max": _int("retry_max", s["retry_max"], 1, 20),
            "retry_backoff": _int("retry_backoff", s["retry_backoff"], 1, 10080),
            "sender_name": (request.form.get("sender_name") or "").strip(),
            "reply_to": (request.form.get("reply_to") or "").strip(),
            "signature": (request.form.get("signature") or "").rstrip(),
            "track_opens": form_bool("track_opens"),
            "track_clicks": form_bool("track_clicks"),
            "footer_enabled": form_bool("footer_enabled"),
            "footer_text": (request.form.get("footer_text") or "").strip(),
            "footer_address": (request.form.get("footer_address") or "").strip(),
        }
        if values["max_delay"] < values["min_delay"]:
            values["max_delay"] = values["min_delay"]
        app_settings.save_many(sess, values)
        sess.commit()
        ok("Settings saved. The worker picks them up on its next email.")
        return redirect(url_for("admin.settings"))
    from app.views.dashboard import service_health
    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/")))
    return render_template("settings.html", s=s, zones=zones, health=service_health(s), config=config,
                           smtp_ok=mailer.smtp_configured())


@bp.route("/settings/test-smtp", methods=["POST"])
def test_smtp():
    to = (request.form.get("to") or "").strip()
    if not mailer.smtp_configured():
        err("SMTP_HOST / SMTP_USER / SMTP_PASSWORD are not set in the server's .env file.")
        return redirect(url_for("admin.settings"))
    if not EMAIL_RE.match(to):
        err("Enter an email address to send the test message to.")
        return redirect(url_for("admin.settings"))
    s = settings_now()
    msg, _ = mailer.build_message(
        to_email=to, to_name="", subject="Test email from your outreach panel",
        plain="If you can read this, SMTP sending works.", html_doc="<p>If you can read this, SMTP sending works.</p>",
        sender_name=s["sender_name"], reply_to=s["reply_to"] or None)
    try:
        mailer.deliver(msg, to)
        ok(f"Test email sent to {to}.")
    except SendError as e:
        err(f"SMTP test failed: {e}")
    return redirect(url_for("admin.settings"))


@bp.route("/settings/test-imap", methods=["POST"])
def test_imap():
    if not (config.IMAP_HOST and config.IMAP_USER):
        err("IMAP_HOST / IMAP_USER are not set in the server's .env file.")
    else:
        try:
            imap = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=10)
            imap.login(config.IMAP_USER, config.IMAP_PASSWORD)
            imap.logout()
            ok("IMAP login works. Replies and bounces will be detected.")
        except Exception as e:
            err(f"IMAP test failed: {e}")
    return redirect(url_for("admin.settings"))


# ------------------------------------------------------------------ tags

def _retag(sess, table, key, source_id, target_id):
    for (rid,) in sess.query(table.c[key]).filter(table.c.tag_id == source_id).all():
        if not sess.query(table).filter(table.c[key] == rid, table.c.tag_id == target_id).first():
            sess.execute(table.insert().values(**{key: rid, "tag_id": target_id}))
    sess.execute(table.delete().where(table.c.tag_id == source_id))


@bp.route("/tags", methods=["GET", "POST"])
def tags():
    sess = db()
    if request.method == "POST":
        act = request.form.get("action")
        tag = get_or_404(Tag, int_arg("tag_id", 0, request.form))
        if act == "delete":
            sess.execute(person_tags.delete().where(person_tags.c.tag_id == tag.id))
            sess.execute(company_tags.delete().where(company_tags.c.tag_id == tag.id))
            sess.delete(tag)
            ok(f"Tag '{tag.name}' deleted.")
        elif act == "rename":
            new = (request.form.get("name") or "").strip().lower()[:100]
            other = sess.query(Tag).filter(Tag.name == new, Tag.id != tag.id).first()
            if not new:
                err("Name can't be empty.")
            elif other:  # merge into the existing tag
                _retag(sess, person_tags, "person_id", tag.id, other.id)
                _retag(sess, company_tags, "company_id", tag.id, other.id)
                sess.delete(tag)
                ok(f"Merged into '{other.name}'.")
            else:
                tag.name = new
                ok("Tag renamed.")
        elif act == "color":
            tag.color = (request.form.get("color") or "").strip()[:20] or None
            ok("Colour saved.")
        sess.commit()
        return redirect(url_for("admin.tags"))
    people_counts = dict(sess.query(person_tags.c.tag_id, func.count()).group_by(person_tags.c.tag_id).all())
    company_counts = dict(sess.query(company_tags.c.tag_id, func.count()).group_by(company_tags.c.tag_id).all())
    rows = [(t, people_counts.get(t.id, 0), company_counts.get(t.id, 0))
            for t in sess.query(Tag).order_by(Tag.name).all()]
    return render_template("tags.html", rows=rows)


# ------------------------------------------------------------------ do-not-contact list

@bp.route("/suppression", methods=["GET", "POST"])
def suppression():
    sess = db()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "add":
            added = 0
            for token in request.form.get("values", "").replace(",", "\n").replace(";", "\n").splitlines():
                if add_suppression(sess, token, (request.form.get("reason") or "Added manually").strip()):
                    added += 1
            # make existing contacts for those addresses visibly blocked too
            sess.commit()
            ok(f"Added {added} entr{'y' if added == 1 else 'ies'} to the do-not-contact list.")
        elif act == "remove":
            row = sess.get(Suppression, int_arg("id", 0, request.form))
            if row:
                sess.delete(row)
                sess.commit()
                ok("Removed from the list. (Contacts who unsubscribed stay flagged as unsubscribed.)")
        return redirect(url_for("admin.suppression"))
    q = sess.query(Suppression)
    term = (request.args.get("q") or "").strip().lower()
    if term:
        q = q.filter(func.lower(Suppression.value).like(f"%{term}%"))
    page = paginate(q.order_by(Suppression.id.desc()), 50)
    return render_template("suppression.html", page=page, term=term)


# ------------------------------------------------------------------ reusable email templates

@bp.route("/templates", methods=["GET", "POST"])
def templates():
    sess = db()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "delete":
            t = sess.get(Template, int_arg("id", 0, request.form))
            if t:
                sess.delete(t)
                ok("Template deleted.")
        else:
            name = (request.form.get("name") or "").strip()
            if not name:
                err("Template name is required.")
                return redirect(url_for("admin.templates"))
            t = sess.get(Template, int_arg("id", 0, request.form)) if request.form.get("id") else Template()
            if t is None:
                t = Template()
            t.name = name
            t.channel = request.form.get("channel") if request.form.get("channel") in ("email", "whatsapp") else "email"
            meta = {}
            if t.channel == "whatsapp":
                meta = {"wa_template": (request.form.get("wa_template") or "").strip(),
                        "language": (request.form.get("wa_language") or "en_US").strip()}
            t.meta = meta or None
            t.subject = (request.form.get("subject") or "").strip() or None
            t.body = (request.form.get("body") or "").rstrip() or None
            sess.add(t)
            ok("Template saved.")
        sess.commit()
        return redirect(url_for("admin.templates"))
    edit_id = int_arg("edit")
    return render_template("templates_lib.html", rows=sess.query(Template).order_by(Template.name).all(),
                           editing=sess.get(Template, edit_id) if edit_id else None)


@bp.route("/templates/<int:tid>.json")
def template_json(tid):
    t = get_or_404(Template, tid)
    return jsonify(subject=t.subject or "", body=t.body or "", channel=t.channel, meta=t.meta or {})
