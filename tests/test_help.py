"""In-app documentation: every module documented in English and Bengali, linked from its pages."""
import re

import pytest

from app import help as help_docs
from app.web import app

# Pages that are not part of any module (sign-in, health check, recipient-facing and machine endpoints).
UNDOCUMENTED_BLUEPRINTS = {"api", "webhooks", "tracking", "help", "static"}
UNDOCUMENTED_ENDPOINTS = {"admin.login", "admin.logout", "admin.healthz"}


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    c = app.test_client()
    assert c.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return c


@pytest.mark.parametrize("lang", sorted(help_docs.LANGS))
@pytest.mark.parametrize("slug", sorted(help_docs.MODULES))
def test_every_module_has_a_page_in_each_language(slug, lang):
    assert help_docs.has(slug, lang), f"app/help/{slug}.{lang}.md is missing"
    doc = help_docs.load(slug, lang)
    assert doc["lang"] == lang
    assert doc["summary"] and len(doc["html"]) > 400


def test_no_orphan_help_files():
    names = {p.name for p in help_docs.DIR.glob("*.md")}
    expected = {f"{slug}.{lang}.md" for slug in help_docs.MODULES for lang in help_docs.LANGS}
    assert names == expected


def test_every_page_of_the_app_has_help():
    missing = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in UNDOCUMENTED_ENDPOINTS:
            continue
        if rule.endpoint.split(".")[0] in UNDOCUMENTED_BLUEPRINTS or "." not in rule.endpoint:
            continue
        if help_docs.slug_for(rule.endpoint) is None:
            missing.append(rule.endpoint)
    assert not missing, f"no help page documents: {missing}"


def test_module_endpoints_exist():
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    for slug, (_, main, covered) in help_docs.MODULES.items():
        assert main in endpoints, slug
        assert all(e in endpoints or e in {r.split(".")[0] for r in endpoints} for e in covered), slug


@pytest.mark.parametrize("lang", sorted(help_docs.LANGS))
def test_links_between_help_pages_resolve(lang):
    for slug in help_docs.MODULES:
        text = help_docs._path(slug, lang).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(/help/([\w-]+)\)", text):
            assert target in help_docs.MODULES, f"{slug}.{lang}.md links to unknown help page {target}"


def test_headings_get_anchors_in_bengali():
    doc = help_docs.load("people", "bn")
    assert doc["sections"] and all(anchor for anchor, _ in doc["sections"])
    assert all(f'id="{anchor}"' in doc["html"] for anchor, _ in doc["sections"])


def test_index_lists_every_module(client):
    for lang in help_docs.LANGS:
        html = client.get(f"/help?lang={lang}").get_data(as_text=True)
        for slug in help_docs.MODULES:
            assert f'href="/help/{slug}"' in html
    assert "সাহায্য ও নির্দেশিকা" in client.get("/help?lang=bn").get_data(as_text=True)


def test_page_in_both_languages_and_language_is_remembered(client):
    en = client.get("/help/people?lang=en").get_data(as_text=True)
    assert "Finding people" in en
    bn = client.get("/help/people?lang=bn")
    assert "মানুষ খুঁজে বের করা" in bn.get_data(as_text=True)
    again = client.get("/help/companies").get_data(as_text=True)     # no ?lang: remembered from last visit
    assert "কোম্পানি" in again


def test_untranslated_page_falls_back_to_english(client, monkeypatch):
    real = help_docs.has
    monkeypatch.setattr(help_docs, "has", lambda slug, lang: False if (slug, lang) == ("tags", "bn") else real(slug, lang))
    html = client.get("/help/tags?lang=bn").get_data(as_text=True)
    assert "has not been translated" in html and "Renaming a tag" in html


def test_unknown_help_page_and_language(client):
    assert client.get("/help/nope").status_code == 404
    assert client.get("/help/people?lang=xx").status_code == 200       # unknown language is ignored


def test_help_requires_sign_in():
    app.config["TESTING"] = True
    assert app.test_client().get("/help/people").status_code == 302


@pytest.mark.parametrize("url, slug", [
    ("/", "dashboard"), ("/inbox", "inbox"), ("/tasks", "tasks"), ("/people", "people"),
    ("/companies", "companies"), ("/deals", "deals"), ("/campaigns", "campaigns"), ("/enrollments", "enrollments"),
    ("/outbox", "outbox"), ("/activity", "activity"), ("/import", "import"), ("/templates", "templates"),
    ("/tags", "tags"), ("/suppression", "suppression"), ("/settings", "settings"),
    ("/settings/integrations", "integrations"), ("/settings/fields", "fields"), ("/settings/pipelines", "fields"),
    ("/settings/users", "users"), ("/settings/api-keys", "api-keys"), ("/settings/audit", "audit"),
])
def test_each_page_links_to_its_help_in_both_languages(client, url, slug):
    resp = client.get(url, follow_redirects=True)
    assert resp.status_code == 200, url
    html = resp.get_data(as_text=True)
    assert f'href="/help/{slug}?lang=en"' in html and f'href="/help/{slug}?lang=bn"' in html, url


def test_help_pages_have_no_floating_button(client):
    assert "help-fab" not in client.get("/help/people").get_data(as_text=True)
