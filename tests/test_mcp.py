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
