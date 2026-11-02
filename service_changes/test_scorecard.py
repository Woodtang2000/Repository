from datetime import datetime

from .context import ALASKA
from . import scorecard


def msg(ts, payload, text):
    return {"ts": str(ts), "text": text, "metadata": {"event_type": "service_desk", "event_payload": payload}}


def test_the_daily_scorecard_counts_channels_and_who_finished():
    day = datetime(2026, 11, 1, tzinfo=ALASKA)
    t = lambda h: day.replace(hour=h).timestamp()
    msgs = [
        msg(t(9), {"kind": "done", "src_channel": "CR5", "driver": "Jordan Smith", "entered_by": "Donna", "done_at": t(10),
                   "readback": "✅ 5045-1-00000 HOMER GOLF COURSE – 20 aprons added"}, "Homer ... #route-5"),
        msg(t(11), {"kind": "done", "src_channel": "text", "sms_to": "+19075551234", "driver": "Jane", "entered_by": "Sonja Burke",
                    "done_at": t(12)}, "Customer text from Jane"),
        msg(t(13), {"kind": "ticket", "src_channel": "DS", "driver": "Scott Woodland", "donna": "needs_person"}, "Scott, by voice"),
        msg(t(14), {"kind": "ticket", "src_channel": "CR5", "driver": "Kirk"}, "📞 Kirk called it in"),
        msg(t(15), {"kind": "web_request"}, "Website request"),
        msg(t(16), {"kind": "answer"}, "just an answer"),
    ]
    text = scorecard.build(msgs, lambda m: m["metadata"]["event_payload"], lambda m: m["text"], {"CR5"}, "CDESK",
                           day, day.replace(hour=23, minute=59), "Sunday, November 1")
    assert "*Requests in:* 5" in text
    assert "Driver in Slack 1" in text and "Customer text 1" in text and "Donna app (voice) 1" in text
    assert "Employee line (driver call) 1" in text and "Website chat 1" in text
    assert "*Finished (✅):* 2 — Donna 1 · Sonja 1" in text
    assert "*Still open from that day:* 3 (1 that Donna handed to a person)" in text
    assert "• 5045-1-00000 HOMER GOLF COURSE – 20 aprons added" in text
    assert "Asked by:" in text and "Jordan 1" in text
