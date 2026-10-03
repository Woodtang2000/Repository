"""Service Desk bot: read new messages in the #route-N channels and post an office ticket for each change.

  python -m service_changes.bot --data service_changes/data --since-minutes 70          # silent trial
  python -m service_changes.bot --data service_changes/data --since-minutes 70 --live   # reply in threads

Silent mode (the default) posts everything to #service-desk-test, where only the people in that channel see
it; drivers and the office see nothing. Live mode replies in the thread under each driver's message instead.
One run is one pass, so a scheduled job (every hour, say) can run it; posts are tagged with the source
message so overlapping runs never post twice.

Needs SLACK_BOT_TOKEN and ANTHROPIC_API_KEY (or SERVICE_DESK_API_KEY).
"""
import argparse
import os
import re
import sys
import time

from .checks import check, ticket
from .context import Alliant, route_from_channel, service_day
from .schema import Category

TEST_CHANNEL = "service-desk-test"
ACTIONABLE = {Category.item_change, Category.wearer_change, Category.hold_or_closure, Category.special_order}
REF = re.compile(r"ref ([A-Z0-9]+)/(\d+\.\d+)")


def ref_tag(channel_id: str, ts: str) -> str:
    return f"ref {channel_id}/{ts}"


def format_post(channel_name: str, author: str, text: str, link: str, parsed, result, alliant, live: bool) -> str | None:
    """The Slack text for one driver message, or None when there is nothing worth posting."""
    if parsed.category == Category.not_a_request:
        return None
    if parsed.category not in ACTIONABLE:
        body = f"_Not an account change ({parsed.category.value.replace('_', ' ')}): {parsed.summary}_"
        if live:
            return None  # the office handles route moves and problems as they do today
    else:
        body = ticket(result, alliant)
    if live:
        return body
    quoted = "\n".join("> " + ln for ln in text.splitlines()) or ">"
    return f"*#{channel_name}* · {author} · <{link}|open>\n{quoted}\n{body}"


def _office_staff() -> set[str]:
    """Names (as Slack shows them) of office staff, from office_staff.txt, one per line, lower-cased."""
    path = os.path.join(os.path.dirname(__file__), "office_staff.txt")
    if not os.path.exists(path):
        return set()
    return {ln.strip().lower() for ln in open(path) if ln.strip() and not ln.startswith("#")}


def _name(slack, cache: dict, user: str | None) -> str:
    if user not in cache:
        try:
            u = slack.users_info(user=user)["user"]
            cache[user] = u.get("real_name") or u.get("name") or user
        except Exception:
            cache[user] = user or "?"
    return cache[user]


def _messages(client, channel_id: str, oldest: float, **kw):
    cursor = None
    while True:
        r = client.conversations_history(channel=channel_id, oldest=str(oldest), limit=200, cursor=cursor, **kw)
        yield from r["messages"]
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return


def run_once(slack, claude, alliant: Alliant, since_minutes: int, live: bool, dry_run: bool = False) -> int:
    from .parser import parse_message
    from .run import fill_item_matches, fix_department

    channels = {}
    cursor = None
    while True:
        r = slack.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=500, cursor=cursor)
        channels.update({c["name"]: c["id"] for c in r["channels"] if c.get("is_member")})
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    routes = {name: cid for name, cid in channels.items() if re.fullmatch(r"route-\d+", name)}
    test_id = channels.get(TEST_CHANNEL)
    if not routes:
        sys.exit("The bot isn't in any #route-N channel yet: /invite @Service Desk in each one.")
    if not live and not test_id:
        sys.exit(f"Create #{TEST_CHANNEL} and /invite @Service Desk, or run with --live.")

    oldest = time.time() - since_minutes * 60
    done = set()  # source messages already handled by an earlier run
    if test_id:
        for m in _messages(slack, test_id, oldest - 86400):
            done.update(REF.findall(m.get("text", "")))
    me = slack.auth_test()["user_id"]
    names: dict[str, str] = {}
    office = _office_staff()
    posted = 0
    for name, cid in sorted(routes.items()):
        previous = ""
        for m in reversed(list(_messages(slack, cid, oldest - 3600))):  # an hour of lead-in for context
            if m.get("subtype") or m.get("bot_id") or (m.get("thread_ts") and m["thread_ts"] != m["ts"]):
                continue
            prior, previous = previous, m.get("text", "")
            if float(m["ts"]) < oldest:
                continue
            if (cid, m["ts"]) in done:
                continue
            if live and m.get("reply_count"):  # already answered in the thread?
                replies = slack.conversations_replies(channel=cid, ts=m["ts"])["messages"]
                if any(r.get("user") == me or REF.search(r.get("text", "")) for r in replies[1:]):
                    continue
            text = m.get("text", "")
            author = _name(slack, names, m.get("user"))
            parsed = parse_message(claude, text, name, m["ts"], alliant, author=author,
                                   office=author.lower() in office, previous=prior)
            fix_department(parsed, alliant, route_from_channel(name), service_day(m["ts"]))
            fill_item_matches(claude, parsed, alliant)
            result = check(parsed, alliant)
            link = slack.chat_getPermalink(channel=cid, message_ts=m["ts"])["permalink"]
            post = format_post(name, author, text, link, parsed, result, alliant, live)
            if post is None:
                continue
            post += f"\n_{ref_tag(cid, m['ts'])}_"
            if dry_run:
                print(post, "\n")
            elif live:
                slack.chat_postMessage(channel=cid, thread_ts=m["ts"], text=post)
            else:
                slack.chat_postMessage(channel=test_id, text=post, unfurl_links=False)
            posted += 1
    return posted


def main():
    import anthropic
    from slack_sdk import WebClient

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="service_changes/data")
    ap.add_argument("--since-minutes", type=int, default=70)
    ap.add_argument("--live", action="store_true", help="reply in the drivers' threads instead of #service-desk-test")
    ap.add_argument("--dry-run", action="store_true", help="print the posts instead of sending them")
    args = ap.parse_args()

    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SERVICE_DESK_API_KEY"))
    alliant = Alliant.from_dir(args.data)
    if args.live and os.environ.get("SERVICE_DESK_LIVE") != "1":
        # Safety lock: replying in the drivers' channels takes a setting in the environment, not just a flag.
        sys.exit("--live needs SERVICE_DESK_LIVE=1 in the environment. Until then the bot only posts to #service-desk-test.")
    n = run_once(slack, claude, alliant, args.since_minutes, args.live, args.dry_run)
    print(f"{n} post(s)")


if __name__ == "__main__":
    main()
