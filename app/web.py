import hmac
import secrets
from datetime import datetime

from flask import Flask, abort, g, redirect, render_template, request, session as flask_session, url_for
from markupsafe import Markup, escape

from app import config, settings as app_settings
from app.db import init_db
from app.views.common import db, settings_now
from app.queries import STATUS_LABELS

app = Flask(__name__, static_folder="../static", template_folder="../templates")
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=False,
)
init_db()

PUBLIC_PREFIXES = ("/t/", "/static/", "/healthz", "/login")


# ------------------------------------------------------------------ request lifecycle

@app.teardown_appcontext
def close_db(exc):
    s = g.pop("db", None)
    if s is not None:
        if exc:
            s.rollback()
        s.close()


def csrf_token():
    if "csrf" not in flask_session:
        flask_session["csrf"] = secrets.token_urlsafe(24)
    return flask_session["csrf"]


@app.before_request
def guard():
    path = request.path
    if path.startswith("/t/") or path == "/healthz" or path.startswith("/static/"):
        return None
    if config.ADMIN_PASSWORD and not flask_session.get("auth") and path != "/login":
        if request.method == "GET":
            return redirect(url_for("admin.login", next=request.full_path.rstrip("?")))
        abort(401)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path != "/login":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(sent, flask_session.get("csrf", "x")):
            abort(400, "Your session expired. Reload the page and try again.")
    return None


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.path.startswith(("/t/", "/static/")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ------------------------------------------------------------------ templating helpers

def _local(dt):
    return app_settings.to_local(dt, settings_now()) if dt else None


@app.template_filter("dt")
def f_dt(dt, fmt="%d %b %Y, %H:%M"):
    d = _local(dt)
    return d.strftime(fmt) if d else "-"


@app.template_filter("day")
def f_day(dt):
    d = _local(dt)
    return d.strftime("%d %b %Y") if d else "-"


@app.template_filter("ago")
def f_ago(dt):
    if not dt:
        return "never"
    secs = (datetime.utcnow() - dt).total_seconds()
    if secs < 0:
        secs = -secs
        suffix = ""
        prefix = "in "
    else:
        suffix, prefix = " ago", ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{prefix}{int(secs // size)}{unit}{suffix}"
    return "just now" if not prefix else "in <1m"


@app.template_filter("when")
def f_when(dt):
    """Friendly 'due' text: Now / in 3h / 12 Oct."""
    if not dt:
        return "Now"
    if dt <= datetime.utcnow():
        return "Due"
    return f_ago(dt)


@app.template_filter("delay")
def f_delay(hours):
    hours = hours or 0
    if hours == 0:
        return "immediately"
    d, h = divmod(hours, 24)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "") if p)


@app.template_filter("nl2br")
def f_nl2br(text):
    return Markup("<br>").join(escape(line) for line in (text or "").splitlines())


def nav_counts():
    if "nav" not in g:
        from sqlalchemy import func
        from app.models import Message
        try:
            sess = db()
            g.nav = {
                "failed": sess.query(func.count(Message.id)).filter(Message.status == "failed").scalar() or 0,
                "queued": sess.query(func.count(Message.id)).filter(Message.queued == True).scalar() or 0,  # noqa: E712
                "paused": settings_now()["sending_paused"],
            }
        except Exception:
            sess.rollback()
            g.nav = {"failed": 0, "queued": 0, "paused": False}
    return g.nav


def qs(args, drop=(), **extra):
    """Rebuild a query string from request args, dropping/overriding some keys."""
    from urllib.parse import urlencode
    drop = set(drop) | set(extra)
    pairs = [(k, v) for k in args.keys() if k not in drop for v in args.getlist(k) if v != ""]
    pairs += [(k, str(v)) for k, v in extra.items() if v not in (None, "")]
    return urlencode(pairs)


app.jinja_env.globals.update(csrf_token=csrf_token, STATUS_LABELS=STATUS_LABELS, nav_counts=nav_counts, qs=qs)


@app.context_processor
def inject():
    return {
        "auth_enabled": bool(config.ADMIN_PASSWORD),
        "year": datetime.utcnow().year,
    }


def register():
    from app.views.admin import bp as admin_bp
    from app.views.campaigns import bp as campaigns_bp
    from app.views.contacts import bp as contacts_bp
    from app.views.dashboard import bp as dashboard_bp
    from app.views.imports import bp as imports_bp
    from app.views.outbox import bp as outbox_bp
    from app.views.tracking import bp as tracking_bp
    for b in (admin_bp, campaigns_bp, contacts_bp, dashboard_bp, imports_bp, outbox_bp, tracking_bp):
        app.register_blueprint(b)


register()


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, title="Page not found",
                           message="That page doesn't exist or was deleted."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, title="Request problem",
                           message=getattr(e, "description", "Bad request")), 400


@app.errorhandler(401)
def unauthorized(e):
    return render_template("error.html", code=401, title="Sign in required", message="Please sign in again."), 401


@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", code=413, title="File too large",
                           message="Uploads are limited to 25 MB."), 413


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, title="Something went wrong",
                           message="The error was logged. Try again, or check the container logs."), 500
