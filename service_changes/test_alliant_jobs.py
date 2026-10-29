from types import SimpleNamespace

from .alliant_jobs import ACTION, Jobs
from .todo import attachment


class Slack:
    def __init__(self):
        self.msgs, self.thread = {}, []

    def conversations_history(self, channel, latest, **kw):
        return {"messages": [self.msgs[latest]] if latest in self.msgs else []}

    def chat_update(self, channel, ts, text, attachments=None, metadata=None):
        self.msgs[ts].update(attachments=attachments, metadata=metadata)

    def chat_postEphemeral(self, channel, user, text):
        self.thread.append(("ephemeral", text))

    def chat_postMessage(self, channel, text, thread_ts=None, **kw):
        self.thread.append((thread_ts, text))


def ticket(ts="100.1"):
    return {"ts": ts, "text": "", "attachments": attachment("*1205-1-00004* MIDAS #4\n➕ 50 TOWEL BAR MOP (100 → 150)", button=False),
            "metadata": {"event_type": "service_desk", "event_payload": {"kind": "ticket", "account": "1205-1-00004",
                                                                        "expect": [{"item": "TOWEL BAR MOP", "qty": 150}]}}}


def setup(tmp_path):
    slack = Slack()
    readbacks = []
    desk = SimpleNamespace(slack=slack, desk_id="CDESK", data_dir=str(tmp_path), me="UBOT",
                           is_office=lambda u: u == "UANA", is_owner=lambda u: u == "U4KFSQCDQ", name=lambda u: {"UANA": "Ana Faamita"}.get(u, u),
                           send_readback=lambda t, user, notes: readbacks.append((t["ts"], user, notes)))
    t = ticket()
    slack.msgs[t["ts"]] = t
    return Jobs(desk), slack, t, readbacks


def tap(user):
    return {"user": {"id": user}, "actions": [{"action_id": ACTION, "value": "100.1"}]}


def blocks(m):
    return [b.get("block_id") for b in m["attachments"][0]["blocks"]]


def test_button_then_entered(tmp_path):
    jobs, slack, t, readbacks = setup(tmp_path)
    assert jobs.offer([t], set()) == 1 and "donna" in blocks(t)
    assert jobs.offer([t], set()) == 0  # once
    assert jobs.queue(tap("UDRIVER")).startswith("ignored") and jobs.next() is None  # office staff only
    assert slack.thread.pop() == ("ephemeral", "Only the office can send tickets to Donna for Alliant.")
    assert jobs.queue(tap("UANA")) == "queued 100.1"
    assert "donna" not in blocks(t) and "Queued for Donna · asked by Ana" in str(t["attachments"])
    job = jobs.next()
    assert job["account"] == "1205-1-00004" and "50 TOWEL BAR MOP" in job["ticket"] and job["expect"][0]["qty"] == 150
    assert jobs.next() is None  # one at a time
    assert jobs.result("100.1", "entered", "Bar mops 100 → 150, saved.") == "entered"
    assert readbacks == [("100.1", "UBOT", [])]  # the readback goes out as Donna; her note never to the customer
    assert slack.thread == [("100.1", "🤖 Donna: Bar mops 100 → 150, saved.")]
    assert jobs.result("100.1", "entered", "again") == "unknown job"


def test_needs_a_person_and_pause(tmp_path):
    jobs, slack, t, readbacks = setup(tmp_path)
    jobs.queue(tap("UANA"))
    assert "Donna is paused" in jobs.pause(True) and jobs.next() is None
    jobs.pause(False)
    jobs.next()
    assert jobs.result("100.1", "needs_person", "Account name on screen doesn't match the ticket.") == "back to the office"
    assert not readbacks and "Donna needs a person" in str(t["attachments"])
    assert t["metadata"]["event_payload"]["donna"] == "needs_person"


def test_a_lost_job_goes_back_in_the_queue(tmp_path, monkeypatch):
    import time
    jobs, slack, t, readbacks = setup(tmp_path)
    jobs.queue(tap("UANA"))
    jobs.next()
    later = time.time() + 31 * 60
    monkeypatch.setattr(time, "time", lambda: later)
    assert jobs.next()["id"] == "100.1"


def test_workstation_key(monkeypatch):
    import io
    from .webchat import make_handler
    monkeypatch.setenv("DONNA_WORKER_TOKEN", "w" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: io.BytesIO(b'{"job": null}'))
    H = make_handler(None, "https://x")
    sent = []

    def get(auth):
        h = H.__new__(H)
        h.path, h.headers = "/api/donna/jobs/next", {"Authorization": auth}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_GET()
        return sent[-1]
    assert get("Bearer nope")[0] == 401
    assert get("Bearer " + "w" * 32) == (200, b'{"job": null}')
    h = H.__new__(H)
    h.path, h.headers = "/api/donna/jobs/rules", {"Authorization": "Bearer " + "w" * 32}
    h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
    h.do_GET()
    assert sent[-1][0] == 200 and b"the only field you may change is **Auto Count**" in sent[-1][1]


def test_owner_can_always_send_a_ticket(tmp_path):
    jobs, slack, t, readbacks = setup(tmp_path)
    assert jobs.queue(tap("U4KFSQCDQ")) == "queued 100.1"


def test_workstation_reads_the_alliant_notes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from .webchat import make_handler
    monkeypatch.setenv("DONNA_WORKER_TOKEN", "w" * 32)
    monkeypatch.setattr("time.sleep", lambda s: None)
    notes = tmp_path / "alliant_university"
    (notes / "Courses").mkdir(parents=True)
    (notes / "02 Customer Items.md").write_text("New Item: pick the SKU")
    (notes / "Courses" / "c1.md").write_text("course")
    (tmp_path / "secret.md").write_text("not a note")
    H = make_handler(SimpleNamespace(data_dir=str(tmp_path)), "https://x")
    sent = []

    def get(path, key="w" * 32):
        h = H.__new__(H)
        h.path, h.headers = path, {"Authorization": "Bearer " + key}
        h._send = lambda code, b, ctype, cache=0: sent.append((code, b))
        h.do_GET()
        return sent[-1]
    assert get("/api/donna/jobs/notes") == (200, b"02 Customer Items.md\nCourses/c1.md")
    assert get("/api/donna/jobs/notes/02%20Customer%20Items.md") == (200, b"New Item: pick the SKU")
    assert get("/api/donna/jobs/notes/../secret.md")[0] == 404
    assert get("/api/donna/jobs/notes", key="nope")[0] == 401


def test_her_readback_is_what_she_actually_did(tmp_path):
    jobs, slack, t, readbacks = setup(tmp_path)
    jobs.desk.alliant = SimpleNamespace(customers=[SimpleNamespace(account="1205-1-00004", name="MIDAS #4")])
    jobs.desk.send_readback = lambda t, user, notes: readbacks.append(t["metadata"]["event_payload"]["readback"])
    jobs.queue(tap("UANA"))
    jobs.next()
    jobs.result("100.1", "entered", "screen 120, set 150", readback="bar mops changed from 120 to 150 a week")
    assert readbacks == ["✅ 1205-1-00004 MIDAS #4 – bar mops changed from 120 to 150 a week"]


def test_unclear_she_asks_whoever_asked(tmp_path):
    jobs, slack, t, readbacks = setup(tmp_path)
    asked = []
    jobs.desk.ask_requester = lambda t, q: asked.append(q) or "customer"
    jobs.queue(tap("UANA"))
    jobs.next()
    assert jobs.result("100.1", "ask", "screen shows 50", question="60 in total, or 60 more?") == "asked the customer"
    assert asked == ["60 in total, or 60 more?"] and not readbacks
    assert "Donna asked the customer: 60 in total, or 60 more?" in str(t["attachments"])
    assert t["metadata"]["event_payload"]["kind"] == "asked"  # no longer an open ticket; the answer makes a new one
    (tmp_path / "b").mkdir()
    jobs2, slack2, t2, _ = setup(tmp_path / "b")
    jobs2.desk.ask_requester = lambda t, q: None  # nobody to ask: the office
    jobs2.queue(tap("UANA"))
    jobs2.next()
    assert jobs2.result("100.1", "ask", "?", question="which mat?") == "back to the office"


def test_signed_out_is_announced_once(tmp_path):
    jobs, slack, t, readbacks = setup(tmp_path)
    posted = []
    slack.chat_postMessage = lambda channel, text, thread_ts=None, **kw: posted.append((channel, thread_ts, text, kw))
    jobs.queue(tap("UANA"))
    jobs.next()
    jobs.result("100.1", "needs_person", "Alliant needs signing in")
    alerts = [p for p in posted if p[1] is None]
    assert [p[0] for p in alerts] == ["CDESK", "U4KFSQCDQ"] and "Alliant signed Donna out" in alerts[0][2]
    assert all(p[3]["blocks"][-1]["elements"][0]["value"] == "100.1" for p in alerts)  # the re-send button rides along
    jobs.signed_out()
    assert len([p for p in posted if p[1] is None]) == 2  # at most once an hour
    assert "donna" in blocks(t) and "Waiting for Alliant sign-in" in str(t["attachments"])  # the button is back
    updated = []
    slack.chat_update = lambda **kw: updated.append(kw)
    alert_tap = {**tap("UANA"), "container": {"channel_id": "U4KFSQCDQ", "message_ts": "200.1"}}
    assert jobs.queue(alert_tap) == "queued 100.1" and jobs.next()["id"] == "100.1"  # one tap once she's signed in
    assert any(u["ts"] == "200.1" and u["blocks"] == [] and "Re-sent to Donna" in u["text"] for u in updated)


def test_only_a_real_sign_in_screen_raises_the_alarm():
    from .alliant_jobs import SIGNED_OUT
    assert SIGNED_OUT.search("Alliant needs signing in") and SIGNED_OUT.search("Got a Pick an account screen")
    assert not SIGNED_OUT.search("Alliant session is open and signed in, but no usual SKU for the mat")
    assert not SIGNED_OUT.search("Logged in fine; the account is on hold")
