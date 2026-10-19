"""Helpers shared by all view modules."""
from urllib.parse import urlparse

from flask import abort, flash, g, redirect, request, url_for

from app import settings as app_settings
from app.db import SessionLocal


def db():
    if "db" not in g:
        g.db = SessionLocal()
    return g.db


def settings_now():
    if "settings" not in g:
        g.settings = app_settings.load(db())
    return g.settings


def int_arg(name, default=None, source=None):
    source = source if source is not None else request.args
    try:
        return int(source.get(name))
    except (TypeError, ValueError):
        return default


def get_or_404(model, pk):
    obj = db().get(model, pk)
    if obj is None:
        abort(404)
    return obj


def safe_next(default="/"):
    target = request.values.get("next") or ""
    p = urlparse(target)
    if target.startswith("/") and not target.startswith("//") and not p.netloc:
        return target
    return default


def back(default_endpoint, **values):
    """Redirect to ?next= if it is a local path, else to an endpoint."""
    nxt = request.values.get("next")
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for(default_endpoint, **values))


def ok(msg):
    flash(msg, "ok")


def warn(msg):
    flash(msg, "warn")


def err(msg):
    flash(msg, "err")


class Page:
    def __init__(self, query, page, per_page):
        self.per_page = per_page
        self.total = query.order_by(None).count() if hasattr(query, "order_by") else 0
        self.pages = max(1, -(-self.total // per_page))
        self.page = min(max(1, page), self.pages)
        self.items = query.limit(per_page).offset((self.page - 1) * per_page).all()

    @property
    def first(self):
        return 0 if not self.total else (self.page - 1) * self.per_page + 1

    @property
    def last(self):
        return min(self.total, self.page * self.per_page)

    def window(self):
        lo, hi = max(1, self.page - 2), min(self.pages, self.page + 2)
        return list(range(lo, hi + 1))


def paginate(query, default_per_page=50):
    per = int_arg("per_page", default_per_page)
    per = per if per in (25, 50, 100, 250) else default_per_page
    return Page(query, int_arg("page", 1), per)


def form_ids(name="ids"):
    out = []
    for v in request.form.getlist(name):
        try:
            out.append(int(v))
        except ValueError:
            pass
    return out


def form_bool(name):
    return request.form.get(name) in ("1", "on", "true", "yes")
