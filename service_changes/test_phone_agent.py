import io
import json
from types import SimpleNamespace

from . import phone, phone_agent
from .schema import Action, Category, Change, ParsedMessage
from .test_phone import ALLIANT, Claude, Slack, desk  # noqa: F401  (the phone test fixture)


def test_customer_tools(desk, monkeypatch):
    make, leads = desk
    p = make()
    tools = phone_agent.PhoneTools(p)
    known = {"conversation_id": "conv1", "caller": "+19075551234", "called": "+18668278398"}
    info = tools.handle("caller_info", known)
    assert info["verified_customer"] and "TOWEL SHOP RED" in info["account_facts"] and "COSTCO" not in info["account_facts"]
    stranger = {"conversation_id": "conv2", "caller": "+19075550000", "called": "+18668278398"}
    assert tools.handle("caller_info", stranger)["verified_customer"] is False
    assert "call back" in tools.handle("customer_request", {**stranger, "summary": "200 more bar mops at Costco"})["result"]
    assert p.slack.posts[-1]["meta"]["kind"] == "phone_request"
    tools.handle("new_lead", {**stranger, "summary": "aprons", "business": "Moose Cafe", "town": "Wasilla"})
    assert leads[-1][1]["business"] == "Moose Cafe" and leads[-1][1]["phone"] == "+19075550000"


def test_driver_change_by_agent(desk, monkeypatch, tmp_path):
    make, leads = desk
    import service_changes.parser as parser
    import service_changes.run as run
    (tmp_path / "employee_phones.csv").write_text("phone,slack_id,name\n(907) 555-7777,UMIKE,Mike Driver\n")
    monkeypatch.setattr(parser, "parse_message", lambda *a, **k: ParsedMessage(
        category=Category.item_change, account_number="1205-1-00004", summary="towels",
        changes=[Change(action=Action.set, item="shop towels", quantity=400)]))
    monkeypatch.setattr(run, "fill_item_matches", lambda c, p, a, **kw: None)
    p = make(channels=("service-desk", "employee-calls", "route-12"))
    p.slack.chat_getPermalink = lambda channel, message_ts: {"permalink": "https://slack/x"}
    post = p.slack.chat_postMessage
    p.slack.chat_postMessage = lambda **kw: post(**kw) or {"ts": "9.9"}
    tools = phone_agent.PhoneTools(p)
    mike = {"conversation_id": "c9", "caller": "+19075557777", "called": "+18885197604"}
    assert tools.handle("caller_info", mike)["name"] == "Mike Driver"
    r = tools.handle("driver_change", {**mike, "said": "midas shop towels to 400"})
    assert r["status"] == "confirm" and "300 to 400" in r["say"] and not p.slack.posts
    assert tools.handle("driver_change", {**mike, "said": "yes"})["status"] == "sent"
    note, t = p.slack.posts
    assert note["channel"] == "Croute-12" and t["meta"]["driver_id"] == "UMIKE"
    stranger = {"conversation_id": "c10", "caller": "+19075550000", "called": "+18885197604"}
    p.slack.users_list = lambda **kw: {"members": []}
    assert tools.handle("driver_change", {**stranger, "said": "midas 50 more"})["status"] == "not_allowed"
    tools.handle("employee_message", {**stranger, "summary": "Joe sick today", "caller_name": "Joe"})
    assert p.slack.posts[-1]["channel"] == "Cemployee-calls"


def test_webhook_needs_the_key(monkeypatch):
    from .webchat import make_handler
    monkeypatch.setenv("PHONE_AGENT_TOKEN", "p" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    chat = SimpleNamespace(phone=SimpleNamespace())
    chat._tools = SimpleNamespace(handle=lambda name, body: {"ok": name, "who": body.get("caller")})
    H = make_handler(chat, "https://x")
    sent = []

    def post(key, in_url=False):
        body = json.dumps({"caller": "+19075551234"}).encode()
        h = H.__new__(H)
        h.path, h.rfile = "/api/phone/tool/caller_info" + (f"?key={key}" if in_url else ""), io.BytesIO(body)
        h.headers = {"Content-Length": str(len(body))} | ({} if in_url else {"X-Donna-Key": key})
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_POST()
        return sent[-1]
    assert post("nope")[0] == 401
    assert json.loads(post("p" * 32)[1]) == {"ok": "caller_info", "who": "+19075551234"}
    assert json.loads(post("p" * 32, in_url=True)[1])["ok"] == "caller_info" and post("nope", in_url=True)[0] == 401


def test_tools_copy_the_donna_agents_webhook_shape():
    hook = {"type": "webhook", "name": "ask_donna", "description": "x", "id": "t1",
            "api_schema": {"url": "https://x/api/donna/tool", "method": "POST", "request_headers": {"Authorization": "Bearer k"},
                           "request_body_schema": {"type": "object", "properties": {
                               "question": {"type": "string", "description": "what he asked"},
                               "donna_session": {"type": "string", "description": "", "dynamic_variable": "donna_session"}}}}}
    t = phone_agent._tool(hook, "driver_change", "https://chat.example.com", "secret")
    api = t["api_schema"]
    assert t["name"] == "driver_change" and "id" not in t
    assert api["url"] == "https://chat.example.com/api/phone/tool/driver_change?key=secret" and api["request_headers"] == {}
    props = api["request_body_schema"]["properties"]
    assert props["caller"]["dynamic_variable"] == "system__caller_id" and "dynamic_variable" not in props["said"]
    assert props["conversation_id"]["dynamic_variable"] == "system__conversation_id"
    assert hook["api_schema"]["url"] == "https://x/api/donna/tool"  # the Donna agent's own tool untouched
    assert "caller_info" in phone_agent.prompt("employees") and "NEVER ask why" in phone_agent.prompt("employees")
