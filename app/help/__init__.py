"""In-app documentation: one Markdown page per module and language, `app/help/<slug>.<lang>.md`.

The first `# Heading` of a file is the page title; the first paragraph after it is the summary shown on
the help index. Links to other help pages are written `/help/<slug>`.
"""
import re
from functools import lru_cache
from pathlib import Path

import markdown
from markupsafe import Markup

DIR = Path(__file__).parent
DEFAULT_LANG = "en"
LANGS = {"en": "English", "bn": "বাংলা"}

GROUPS = [
    ("start", {"en": "Getting started", "bn": "শুরু করুন"}),
    ("work", {"en": "Daily work", "bn": "দৈনন্দিন কাজ"}),
    ("crm", {"en": "CRM", "bn": "সিআরএম"}),
    ("outreach", {"en": "Outreach", "bn": "আউটরিচ"}),
    ("library", {"en": "Library", "bn": "লাইব্রেরি"}),
    ("workspace", {"en": "Workspace", "bn": "ওয়ার্কস্পেস"}),
]

# slug -> (group, endpoint of the module's main page, endpoints / blueprints whose pages this documents)
MODULES = {
    "overview": ("start", "dashboard.index", ()),
    "dashboard": ("work", "dashboard.index", ("dashboard",)),
    "inbox": ("work", "inbox.index", ("inbox",)),
    "tasks": ("work", "tasks.index", ("tasks",)),
    "people": ("crm", "people.index", ("people",)),
    "companies": ("crm", "companies.index", ("companies",)),
    "deals": ("crm", "deals.index", ("deals",)),
    "campaigns": ("outreach", "campaigns.index", ("campaigns",)),
    "enrollments": ("outreach", "enrollments.index", ("enrollments",)),
    "outbox": ("outreach", "outbox.index", ("outbox",)),
    "activity": ("outreach", "outbox.activity", ("outbox.activity", "outbox.activity_export")),
    "import": ("outreach", "imports.upload", ("imports",)),
    "templates": ("library", "admin.templates", ("admin.templates", "admin.template_json")),
    "tags": ("library", "admin.tags", ("admin.tags",)),
    "suppression": ("library", "admin.suppression", ("admin.suppression",)),
    "settings": ("workspace", "admin.settings", ("admin.settings", "admin.test_smtp", "admin.test_imap")),
    "integrations": ("workspace", "workspace.integrations", ("workspace.integrations",)),
    "fields": ("workspace", "workspace.fields", ("workspace.fields", "workspace.pipelines")),
    "users": ("workspace", "workspace.users", ("workspace.users", "workspace.me")),
    "api-keys": ("workspace", "workspace.api_keys", ("workspace.api_keys",)),
    "audit": ("workspace", "workspace.audit_log", ("workspace.audit_log",)),
}



def _slug(value, separator):
    """Heading anchor that keeps Bengali vowel signs (the stock slugify drops them)."""
    value = re.sub(r"[^\w\u0980-\u09ff\s-]", "", value).strip().lower()
    return re.sub(rf"[{separator}\s]+", separator, value)


_md_extensions = ["tables", "fenced_code", "sane_lists", "toc"]
_md_config = {"toc": {"slugify": _slug, "toc_depth": "2-3"}}


def slug_for(endpoint):
    """Help page that documents the page served by this Flask endpoint (None: no help for it)."""
    if not endpoint:
        return None
    for slug, (_, _, endpoints) in MODULES.items():
        if endpoint in endpoints:
            return slug
    blueprint = endpoint.split(".")[0]
    for slug, (_, _, endpoints) in MODULES.items():
        if blueprint in endpoints:
            return slug
    return None


def app_endpoint(slug):
    return MODULES[slug][1]


def grouped():
    """[(group key, {lang: label}, [slug, ...])] in menu order."""
    return [(key, labels, [s for s, m in MODULES.items() if m[0] == key]) for key, labels in GROUPS]


def _path(slug, lang):
    return DIR / f"{slug}.{lang}.md"


def has(slug, lang):
    return _path(slug, lang).is_file()


@lru_cache(maxsize=256)
def _render(path, mtime_ns):
    text = Path(path).read_text(encoding="utf-8")
    md = markdown.Markdown(extensions=_md_extensions, extension_configs=_md_config)
    html = md.convert(text)
    title = re.search(r"^#\s+(.+)$", text, re.M)
    summary = re.search(r"^#\s+.+\n+(?!#)(.+)$", text, re.M)
    sections = [(t["id"], t["name"]) for t in md.toc_tokens]       # the "## " headings
    return {"title": title.group(1).strip() if title else Path(path).stem,
            "summary": summary.group(1).strip() if summary else "",
            "html": Markup(html), "sections": sections}


def load(slug, lang):
    """Rendered page for a module: {title, summary, html, sections, lang}; falls back to English when
    the page isn't translated yet (`lang` then says which language you actually got)."""
    if slug not in MODULES:
        raise KeyError(slug)
    if lang not in LANGS or not has(slug, lang):
        lang = DEFAULT_LANG
    path = _path(slug, lang)
    return dict(_render(str(path), path.stat().st_mtime_ns), lang=lang)
