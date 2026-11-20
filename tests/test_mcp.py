"""MCP server (Claude): tools, permissions, audit attribution, HTTP bearer auth."""
import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from app import mcp_server
from app.crm import apikeys
from app.db import SessionLocal, init_db
from app.models import AuditLog, Deal, Person


@pytest.fixture(scope="module")
def keys():
    init_db()
    s = SessionLocal()
    rw, _ = apikeys.create(s, "claude rw", ["read", "write"])
    ro, _ = apikeys.create(s, "claude ro", ["read"])
    s.commit()
    s.close()
    return {"rw": rw, "ro": ro}


def call(name, args, key, monkeypatch):
    monkeypatch.setenv("OUTREACH_API_KEY", key)

    async def go():
        r = await mcp_server.server.call_tool(name, args)
        return json.loads(r.content[0].text)
    return anyio.run(go)


def test_tools_are_registered_with_annotations():
    async def go():
        return await mcp_server.server.list_tools()
    tools = {t.name: t for t in anyio.run(go)}
    for name in ("search", "get_person", "create_person", "update_person", "create_deal", "update_deal",
                 "log_activity", "create_task", "enroll_in_campaign", "send_message", "get_conversation"):
        assert name in tools, name
    assert tools["search"].annotations.read_only_hint is True
    assert tools["send_message"].annotations.open_world_hint is True
    assert tools["merge_people"].annotations.destructive_hint is True


def test_claude_workflow(keys, monkeypatch):
    p = call("create_person", {"first_name": "Nora", "email": "nora@harbour.ie", "company": "Harbour Hotel",
                               "company_website": "harbourhotel.ie", "tags": ["hotel"]}, keys["rw"], monkeypatch)
    assert p["company"]["name"] == "Harbour Hotel" and p["tags"] == ["hotel"]
    with pytest.raises(ToolError, match="already belongs"):
        call("create_person", {"email": "NORA@harbour.ie"}, keys["rw"], monkeypatch)
    found = call("search", {"query": "harbour"}, keys["ro"], monkeypatch)
    assert found["people"][0]["id"] == p["id"] and found["companies"][0]["name"] == "Harbour Hotel"

    d = call("create_deal", {"title": "Harbour - website", "amount": 4800, "person_id": p["id"], "stage": "Qualified"},
             keys["rw"], monkeypatch)
    assert d["stage"] == "Qualified" and d["company"]["name"] == "Harbour Hotel"
    call("log_activity", {"body": "Owner wants direct bookings up 20%", "person_id": p["id"]}, keys["rw"], monkeypatch)
    call("create_task", {"title": "Send proposal", "person_id": p["id"], "due_at": "2030-01-10T10:00"},
         keys["rw"], monkeypatch)
    upd = call("update_person", {"person_id": p["id"], "lifecycle_stage": "opportunity", "add_tags": ["warm"]},
               keys["rw"], monkeypatch)
    assert upd["person"]["lifecycle_stage"] == "opportunity" and "lifecycle_stage" in upd["changed"]

    full = call("get_person", {"email": "nora@harbour.ie"}, keys["ro"], monkeypatch)
    assert full["deals"][0]["title"] == "Harbour - website" and full["open_tasks"][0]["title"] == "Send proposal"
    assert any(i["kind"] == "note" for i in full["timeline"])

    # read-only keys can't write; nobody without 'send' can message people
    with pytest.raises(ToolError, match="'write' permission"):
        call("log_activity", {"body": "x", "person_id": p["id"]}, keys["ro"], monkeypatch)
    with pytest.raises(ToolError, match="'send' permission"):
        call("send_message", {"person_id": p["id"], "channel": "email", "body": "hi"}, keys["rw"], monkeypatch)
    with pytest.raises(ToolError, match="not found"):
        call("get_person", {"person_id": 987654}, keys["ro"], monkeypatch)
    with pytest.raises(ToolError, match="invalid"):
        call("search", {"query": "x"}, "oc_bad_key", monkeypatch)

    won = call("update_deal", {"deal_id": d["id"], "stage": "Won"}, keys["rw"], monkeypatch)
    assert won["deal"]["status"] == "won"

    s = SessionLocal()
    assert s.get(Deal, d["id"]).status == "won"
    labels = {a.actor_label for a in s.query(AuditLog).filter(AuditLog.entity_id == str(p["id"]),
                                                               AuditLog.entity_type == "person")}
    assert labels == {"mcp: claude rw"}
    assert s.get(Person, p["id"]).lifecycle_stage == "opportunity"
    s.close()


def test_http_transport_requires_a_key(keys):
    app = mcp_server.http_app()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "harbour"}}}
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.post("/mcp", json=body, headers=headers).status_code == 401
        assert c.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer oc_x_y"}).status_code == 401
        r = c.post("/mcp", json=body, headers={**headers, "Authorization": f"Bearer {keys['ro']}"})
        assert r.status_code == 200
        payload = next(line[5:] for line in r.text.splitlines() if line.startswith("data:"))
        result = json.loads(json.loads(payload)["result"]["content"][0]["text"])
        assert result["companies"][0]["name"] == "Harbour Hotel"


# ---------------------------------------------------------------------- WhatsApp templates (Twilio Content API)

class FakeContent:
    """Stands in for content.twilio.com: one approved and one unsubmitted template."""
    def __init__(self):
        self.calls = []
        self.items = [
            {"sid": "HXaaa", "friendly_name": "approved_one", "language": "en", "date_created": "2026-11-17",
             "variables": {}, "types": {"twilio/text": {"body": "Hello there"}},
             "approval_requests": {"status": "approved", "category": "MARKETING", "name": "approved_one"}},
            {"sid": "HXbbb", "friendly_name": "draft_one", "language": "en", "date_created": "2026-11-18",
             "variables": {"1": "Anna"}, "types": {"twilio/text": {"body": "Hi {{1}}, thanks"}}},
        ]

    def request(self, method, url, json=None, auth=None):
        import httpx
        self.calls.append((method, url, json))
        if method == "GET":
            return httpx.Response(200, json={"contents": self.items, "meta": {"next_page_url": None}})
        if url.endswith("/Content"):
            return httpx.Response(201, json={"sid": "HXnew"})
        return httpx.Response(201, json={"status": "received", "category": json["category"], "name": json["name"]})


@pytest.fixture
def twilio(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "tok")
    fake = FakeContent()
    monkeypatch.setattr("app.channels.whatsapp.httpx.Client", lambda **kw: _Ctx(fake))
    return fake


class _Ctx:
    def __init__(self, fake): self.fake = fake
    def __enter__(self): return self.fake
    def __exit__(self, *a): return False


def test_whatsapp_template_tools(keys, twilio, monkeypatch):
    sess = SessionLocal()
    sk, _ = apikeys.create(sess, "claude send", ["read", "write", "send"])
    sess.commit()
    sess.close()
    allt = call("list_whatsapp_templates", {}, keys["ro"], monkeypatch)
    assert [t["name"] for t in allt["templates"]] == ["draft_one", "approved_one"]      # newest first
    assert allt["templates"][0]["status"] == "unsubmitted"
    ok = call("list_whatsapp_templates", {"status": "approved"}, keys["ro"], monkeypatch)
    assert [t["sid"] for t in ok["templates"]] == ["HXaaa"] and ok["templates"][0]["body"] == "Hello there"

    new = call("create_whatsapp_template", {"name": "quote_chaser", "body": "Hi {{1}}, did you get our quote?",
                                            "variable_examples": ["Anna"]}, keys["rw"], monkeypatch)
    assert new["sid"] == "HXnew" and new["status"] == "unsubmitted"
    method, url, payload = [c for c in twilio.calls if c[0] == "POST"][0]
    assert payload["types"] == {"twilio/text": {"body": "Hi {{1}}, did you get our quote?"}}
    assert payload["variables"] == {"1": "Anna"}
    s = SessionLocal()
    assert s.query(AuditLog).filter_by(entity_type="whatsapp_template", entity_id="HXnew").count() == 1
    s.close()

    for bad, msg in [({"name": "Bad Name", "body": "x"}, "lowercase"),
                     ({"name": "ok_name", "body": "Hi {{1}}", "variable_examples": ["A"]}, "starts or ends"),
                     ({"name": "ok_name", "body": "a {{2}} b", "variable_examples": ["A"]}, "no gaps"),
                     ({"name": "ok_name", "body": "a {{1}} b"}, "variable_examples")]:
        with pytest.raises(ToolError, match=msg):
            call("create_whatsapp_template", bad, keys["rw"], monkeypatch)
    with pytest.raises(ToolError, match="'write' permission"):
        call("create_whatsapp_template", {"name": "ok_name", "body": "a b"}, keys["ro"], monkeypatch)

    with pytest.raises(ToolError, match="'send' permission"):
        call("submit_whatsapp_template", {"sid": "HXbbb", "category": "UTILITY"}, keys["rw"], monkeypatch)
    with pytest.raises(ToolError, match="already submitted"):
        call("submit_whatsapp_template", {"sid": "HXaaa", "category": "UTILITY"}, sk, monkeypatch)
    with pytest.raises(ToolError, match="MARKETING or UTILITY"):
        call("submit_whatsapp_template", {"sid": "HXbbb", "category": "AUTH"}, sk, monkeypatch)
    sub = call("submit_whatsapp_template", {"sid": "HXbbb", "category": "utility"}, sk, monkeypatch)
    assert sub["status"] == "received" and sub["name"] == "draft_one" and sub["category"] == "UTILITY"
    assert twilio.calls[-1][1].endswith("/Content/HXbbb/ApprovalRequests/whatsapp")


def test_campaign_accepts_template_name(keys, twilio, monkeypatch):
    c = call("create_campaign", {"name": "WA by name", "channel": "whatsapp",
                                 "steps": [{"name": "Hello", "wa_template": "Approved_One", "body": "{{first_name|there}}"}]},
             keys["rw"], monkeypatch)
    assert "HXaaa" in json.dumps(c)
    with pytest.raises(ToolError, match="No single Twilio template"):
        call("create_campaign", {"name": "WA missing", "channel": "whatsapp",
                                 "steps": [{"wa_template": "nope"}]}, keys["rw"], monkeypatch)


# ---------------------------------------------------------------------- saved templates (Templates page)

def test_template_tools(keys, twilio, monkeypatch):
    e = call("create_template", {"name": "Quote chaser", "subject": "Your quote", "body": "Hi {{first_name|there}},\n\nQuote attached."},
             keys["rw"], monkeypatch)
    assert e["channel"] == "email" and e["subject"] == "Your quote" and e["wa_template"] is None
    with pytest.raises(ToolError, match="already exists"):
        call("create_template", {"name": "quote CHASER", "body": "x"}, keys["rw"], monkeypatch)
    with pytest.raises(ToolError, match="needs a body"):
        call("create_template", {"name": "Empty"}, keys["rw"], monkeypatch)

    w = call("create_template", {"name": "WA by name", "channel": "whatsapp", "wa_template": "approved_one",
                                 "body": "{{first_name|there}}"}, keys["rw"], monkeypatch)
    assert w["wa_template"] == "HXaaa" and w["language"] == "en_US"        # name resolved to the Content SID
    with pytest.raises(ToolError, match="needs wa_template"):
        call("create_template", {"name": "WA none", "channel": "whatsapp"}, keys["rw"], monkeypatch)
    with pytest.raises(ToolError, match="No single Twilio template"):
        call("create_template", {"name": "WA bad", "channel": "whatsapp", "wa_template": "nope"}, keys["rw"], monkeypatch)

    u = call("update_template", {"template_id": e["id"], "subject": "Your quote (reminder)"}, keys["rw"], monkeypatch)
    assert u["subject"] == "Your quote (reminder)" and u["body"] == e["body"] and u["name"] == "Quote chaser"
    u = call("update_template", {"template_id": w["id"], "wa_template": "HXbbb"}, keys["rw"], monkeypatch)
    assert u["wa_template"] == "HXbbb" and u["body"] == "{{first_name|there}}"
    with pytest.raises(ToolError, match="not found"):
        call("update_template", {"template_id": 999999, "name": "x"}, keys["rw"], monkeypatch)

    found = call("list_templates", {"channel": "whatsapp", "q": "wa by"}, keys["ro"], monkeypatch)
    assert [t["id"] for t in found["templates"]] == [w["id"]]
    with pytest.raises(ToolError, match="'write' permission"):
        call("create_template", {"name": "No", "body": "x"}, keys["ro"], monkeypatch)
    s = SessionLocal()
    assert s.query(AuditLog).filter_by(entity_type="template", entity_id=str(e["id"])).count() == 2
    s.close()
