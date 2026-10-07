"""The bot's Slack loop against a fake Slack, with the Claude reading replaced by a fixed answer."""
from . import bot
from .context import Alliant, Customer
from .schema import Action, Category, Change, ParsedMessage


class FakeSlack:
    def __init__(self):
        self.posts = []
        self.history = {
            "C1": [{"ts": "1790636236.000100", "user": "U1", "text": "Kobuk decrease 40 barmops. Total 40"},
                   {"ts": "1790636300.000100", "user": "U2", "text": "You're 100% complete with the linens"},
                   {"ts": "1790636400.000100", "user": "U1", "text": "ok", "thread_ts": "1790636236.000100"}],
            "T1": []}

    def conversations_list(self, **kw):
        return {"channels": [{"name": "route-1", "id": "C1", "is_member": True},
                             {"name": "service-desk-test", "id": "T1", "is_member": True},
                             {"name": "general", "id": "G1", "is_member": True}]}

    def conversations_history(self, channel, **kw):
        return {"messages": list(reversed(self.history[channel]))}

    def auth_test(self):
        return {"user_id": "UBOT"}

    def users_info(self, user):
        return {"user": {"real_name": {"U1": "Route 1", "U2": "Kirk"}[user]}}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack/{channel}/{message_ts}"}

    def chat_postMessage(self, channel, text, **kw):
        self.posts.append((channel, kw.get("thread_ts"), text))
        self.history[channel].append({"ts": "9", "text": text, "bot_id": "B"})


SEEN = []


def fake_parse(client, text, channel, ts, alliant, author="", office=False, previous=""):
    SEEN.append((text, author, office, previous))
    if "complete" in text:
        return ParsedMessage(category=Category.not_a_request, summary="plant update")
    return ParsedMessage(category=Category.item_change, customer_as_written="Kobuk", account_number="K1", summary="",
                         changes=[Change(action=Action.decrease, item="bar mops", quantity=40, stated_total=40)])


def test_silent_run_posts_once_to_test_channel(monkeypatch):
    import service_changes.parser as parser
    monkeypatch.setattr(parser, "parse_message", fake_parse)
    a = Alliant(customers=[Customer("K1", "KOBUK COFFEE", "1", ["Fri"])], items={"K1": {"TOWEL BAR MOP GOLD STRIPE": 80}})
    slack = FakeSlack()
    assert bot.run_once(slack, None, a, since_minutes=10**7, live=False) == 1
    channel, thread, text = slack.posts[0]
    assert channel == "T1" and thread is None
    assert "*#route-1* · Route 1" in text and "KOBUK COFFEE" in text and "80 → *40*" in text
    assert "ref C1/1790636236.000100" in text
    # A second run sees its own ref in the test channel and posts nothing new.
    assert bot.run_once(slack, None, a, since_minutes=10**7, live=False) == 0



def test_bot_passes_author_and_previous_message(monkeypatch):
    import service_changes.parser as parser
    monkeypatch.setattr(parser, "parse_message", fake_parse)
    monkeypatch.setattr(bot, "_office_staff", lambda: {"kirk"})
    SEEN.clear()
    a = Alliant(customers=[Customer("K1", "KOBUK COFFEE", "1", ["Fri"])], items={"K1": {"TOWEL BAR MOP GOLD STRIPE": 80}})
    bot.run_once(FakeSlack(), None, a, since_minutes=10**7, live=False)
    assert SEEN[0] == ("Kobuk decrease 40 barmops. Total 40", "Route 1", False, "")
    assert SEEN[1] == ("You're 100% complete with the linens", "Kirk", True, "Kobuk decrease 40 barmops. Total 40")


def test_history_oldest_never_has_more_than_six_decimals():
    # Slack silently returns no messages when `oldest` has 7 decimals, as str(time.time()) often does.
    from .bot import _messages
    seen = []

    class Slack:
        def conversations_history(self, **kw):
            seen.append(kw["oldest"])
            return {"messages": []}
    list(_messages(Slack(), "C1", 1791351064.4902253))
    assert seen == ["1791351064.490225"]
