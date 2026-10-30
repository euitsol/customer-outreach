"""In-app documentation (English / Bengali), one page per module."""
from flask import Blueprint, abort, render_template, request, session as flask_session, url_for

from app import help as help_docs

bp = Blueprint("help", __name__)


def current_lang():
    """?lang= wins and is remembered for the session; otherwise the remembered one, else English."""
    lang = request.args.get("lang")
    if lang in help_docs.LANGS:
        flask_session["help_lang"] = lang
        return lang
    lang = flask_session.get("help_lang")
    return lang if lang in help_docs.LANGS else help_docs.DEFAULT_LANG


@bp.route("/help")
def index():
    lang = current_lang()
    groups = [(labels, [(slug, help_docs.load(slug, lang)) for slug in slugs])
              for _, labels, slugs in help_docs.grouped()]
    return render_template("help/index.html", lang=lang, langs=help_docs.LANGS, groups=groups)


@bp.route("/help/<slug>")
def page(slug):
    if slug not in help_docs.MODULES:
        abort(404)
    lang = current_lang()
    doc = help_docs.load(slug, lang)
    order = list(help_docs.MODULES)
    i = order.index(slug)
    neighbours = [(s, help_docs.load(s, lang)["title"]) if s else None
                  for s in (order[i - 1] if i else None, order[i + 1] if i + 1 < len(order) else None)]
    return render_template("help/page.html", slug=slug, doc=doc, lang=lang, langs=help_docs.LANGS,
                           translated=doc["lang"] == lang, prev=neighbours[0], next=neighbours[1],
                           open_url=url_for(help_docs.app_endpoint(slug)))
