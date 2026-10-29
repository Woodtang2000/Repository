"""Donna field guide for Snow White drivers: the content, and the web page made from it.

Add a feature: a row in GUIDE (and the cheat sheet if it belongs there) and a line at the top of WHATS_NEW; deploy
and the web page has it. Rebuild the printable PDF with `python3 flyer/donna_field_guide.py`.
"""
# The guide itself, once: the web page below and the printable PDF (donna_field_guide.py) both come from it.
# ("lead"|"p"|"note", html) · ("bullets", [html]) · ("table", [head, head], [(a, b)]) · ("section", title)
# Inline marks: <b>, <i>, {CHECK}. Add new features here and under WHATS_NEW, then deploy (and rebuild the PDF).
GUIDE = [
    ("section", "How it works"),
    ("bullets", ["<b>You post</b> a change or a question in your route channel (for example #route-4).",
                 "<b>She reacts with the eyes emoji</b> so you know she has it.",
                 "<b>If something's unclear,</b> she asks you right there, and your phone buzzes.",
                 "<b>She sends the office a ticket</b> with the account, the item and the new count.",
                 "<b>When it's entered,</b> she posts a {CHECK} readback to you in the channel.",
                 "<b>Stuck on the handheld?</b> Ask her how to do it, any time. See <i>Help with your handheld</i>."]),
    ("section", "Report a change"),
    ("p", "Say <b>who</b>, <b>what</b> and <b>how many</b>."),
    ("table", ["What happened", "Type or say something like"], [
        ("Customer wants more", "Midas Fairbanks wants 50 more bar mops"),
        ("A new total", "Humpy's bar mops to 150 a week"),
        ("Fewer", "Safeway 1821 bakery, take 2 mats off"),
        ("Stop an item", "Costco deli stop the aprons"),
        ("Something they've never had", "Wendy's 4412 wants 20 shop towels, weekly"),
        ("Bring something back", "Restart the floor mats at Midas"),
        ("Closed or seasonal", "Homer Golf Course on hold for the winter"),
        ("Skip a week", "Lynden skip next Tuesday, they're closed"),
        ("One-time order", "Chili's needs 30 extra napkins Friday only"),
        ("Today only, already done on the handheld", "0 bar mops today only at Humpy's"),
        ("A problem", "Midas says they were short 10 towels last week")]),
    ("bullets", ["<b>\"More\" or \"total\"?</b> Say which. \"50 more\" adds 50; \"to 50\" makes it 50. If you don't "
                 "say, Donna asks.",
                 "<b>Several customers in one message is fine.</b> Each account gets its own ticket.",
                 "<b>Store numbers and departments help:</b> say \"deli\" or \"bakery\" at Costco 1342.",
                 "<b>Today-only changes</b> you made on the handheld just go to the office as an FYI."]),
    ("section", "Gloves on: talk instead of type"),
    ("p", "<b>Voice message.</b> In Slack, <b>press and hold the microphone</b> next to the message box, say the change, "
          "and let go. Donna posts <i>\"Heard: \u2026\"</i> so you can check she got it right, then it works exactly like "
          "a typed message. Answer her questions the same way."),
    ("p", "<b>Call Donna: (888) 519-7604.</b> Save it in your phone as <b>Donna</b> and say <i>\"Hey Siri, call "
          "Donna.\"</i> She knows you by the phone number in your Slack profile."),
    ("bullets", ["Tell her the change: <i>\"Midas wants 50 more bar mops.\"</i>",
                 "She asks if anything's unclear, then reads it back: <i>\"Midas #4, bar mops from 100 to 150. Want me "
                 "to send that in?\"</i>",
                 "Say <b>yes</b>. The office gets the ticket, and your {CHECK} readback shows up in the route channel "
                 "when it's entered.",
                 "Ask her how to do something on the handheld: <i>\"How do I skip a stop?\"</i>",
                 "Same number for calling in sick or late, or for a problem on route."]),
    ("note", "Phone hands-free only while driving. If Donna doesn't greet you by name, add your cell to your Slack "
             "profile (Profile \u2192 Edit \u2192 Phone) or tell the office."),
    ("section", "Quick checks, readbacks and fixes"),
    ("bullets", ["<b>When Donna asks you something,</b> answer in the channel: \"6\", \"total\", \"the red ones\". "
                 "She asks at most twice; after that the office calls you.",
                 "<b>When the office enters it,</b> you get a {CHECK} readback with who entered it: <i>\"Midas #4: bar "
                 "mops from 100 to 150, entered by Sonja.\"</i> That's what's in Alliant now.",
                 "<b>Readback wrong?</b> Reply \"no, I meant 3\". It goes back as a correction.",
                 "<b>Changed your mind before it's entered?</b> Post the new version. The old ticket is marked "
                 "replaced, so nobody enters it twice.",
                 "<b>An office note</b> sometimes comes with the readback (\"only had 1 in stock\"). Pass it on to "
                 "the customer."]),
    ("section", "Uniforms and wearers"),
    ("p", "Give the wearer's <b>full name</b>, the <b>customer</b>, and for new garments the <b>sizes and how many</b>."),
    ("table", ["What you need", "Type or say something like"], [
        ("New wearer", "New wearer at Tire Town: Karissa Lee, 11 shirts size M, 11 pants 32x30"),
        ("Size change", "Raven at Safeway 1817 meat, shirts from L to XL"),
        ("Wearer left", "Stop Joey Smith at Midas, he quit"),
        ("Swap a garment", "Dave at Tire Town wants coats instead of shirts"),
        ("Garments need repair", "Pull Karissa's shirts at Tire Town for repair, buttons missing"),
        ("Check or audit garments", "Inspect all the pants at Midas, they look worn"),
        ("Count what's there", "Inventory count at Safeway 1821 bakery"),
        ("Where are their garments?", "Are Dave's new shirts at Tire Town on the way?")]),
    ("section", "Ask Donna"),
    ("p", "Ask in your route channel and she answers from Alliant in seconds."),
    ("table", ["You want to know", "Ask"], [
        ("What a customer gets", "How many bar mops does Humpy's get?"),
        ("Everything on an account", "What's on the Midas Fairbanks account?"),
        ("Delivery days", "What day does Chili's get delivered?"),
        ("Several departments", "How many mats does Costco 1342 get, all of them combined?"),
        ("A wearer's garments", "What does Karissa at Tire Town have?"),
        ("Today's stops", "What are my stops today?"),
        ("Stop order", "Who's after Midas today?")]),
    ("bullets", ["Answers come from <b>Alliant data refreshed every hour</b> (6 AM to 10 PM); a change shows within about an hour.",
                 "She answers about <b>customers on your route</b>; anything else goes to the office.",
                 "If Alliant doesn't say, she asks the office and they answer you in the channel.",
                 "The office can see her answers. After 15 questions in a day she hands the rest to the office."]),
    ("section", "Help with your handheld"),
    ("p", "Stuck on the handheld or Alliant mobile? Ask Donna how. She took the Alliant University training and walks "
          "you through it step by step, any way you like:"),
    ("bullets", ["<b>In your route channel:</b> type it or hold the mic. She answers in the message's thread.",
                 "<b>In private:</b> in Slack, search for <b>Donna</b> and message her. Nobody else sees it.",
                 "<b>By phone:</b> call <b>(888) 519-7604</b> and ask. Handy with gloves on."]),
    ("table", ["You're stuck on", "Ask"], [
        ("Starting the day", "How do I start my route on the handheld?"),
        ("A closed stop", "How do I mark a stop closed?"),
        ("Skipping a stop", "How do I skip a stop?"),
        ("A today-only change", "How do I change a quantity just for today?"),
        ("Garments", "How do I scan in a new wearer's garments?"),
        ("Signatures", "How do I get the customer's signature?")]),
    ("p", "If the training doesn't cover it, she says so and asks the office to follow up with you."),
    ("section", "What Donna won't do"),
    ("bullets", ["<b>No prices or balances.</b> Pricing questions go to the office.",
                 "<b>Nothing is changed until the {CHECK} readback.</b> The eyes emoji means \"got it\", not \"done\". "
                 "Tell the customer it's done once you see the readback.",
                 "<b>Only your route.</b> She won't look up customers on other routes.",
                 "<b>Not for emergencies.</b> Breakdown, accident, angry customer on the spot: call the office at "
                 "<b>(907) 222-7738</b>.",
                 "<b>She's an AI assistant</b> and will say so if you ask. She gets the office a clean ticket faster; "
                 "she doesn't replace them."]),
]
INTRO = ("Post a customer change in your route channel, by typing or by voice, and Donna turns it into a clean ticket "
         "for the office, then tells you when it's in Alliant. Stuck on your handheld? Ask her how.",
         "You talk the way you'd tell the office. Customer names, nicknames and store numbers all work. No forms, no "
         "account numbers, no threads needed.")
CHEAT = [("Change", "customer + item + \"more\" or \"total\" + how many. <i>Humpy's bar mops to 150.</i>"),
         ("New item", "customer + item + how many + how often. <i>Wendy's 4412, 20 shop towels weekly.</i>"),
         ("Stop", "customer + item. <i>Costco deli stop the aprons.</i>"),
         ("Wearer", "customer + full name + garments + sizes. <i>Tire Town, Karissa Lee, 11 shirts M.</i>"),
         ("Repair or count", "customer + whose garments + why. <i>Pull Karissa's shirts for repair.</i>"),
         ("Question", "just ask. <i>How many mats does Midas get?</i>"),
         ("Stops", "<i>What are my stops today?</i>"),
         ("Handheld help", "ask Donna: in your channel, in private, or by phone. <i>How do I skip a stop?</i>"),
         ("Gloves on", "hold the mic in Slack, or call Donna at <b>(888) 519-7604</b>."),
         ("Readback wrong", "<i>\"No, I meant 3.\"</i>")]
CHEAT_FOOT = "Eyes emoji = she has it. {CHECK} = it's in Alliant. Prices, balances and emergencies: <b>(907) 222-7738</b>."
WHATS_NEW = [("Oct 29, 2026", "Handheld help by phone and voice message, too"),
             ("Oct 29, 2026", "Call Donna at (888) 519-7604 to report a change, hands-free"),
             ("Oct 29, 2026", "Voice messages: hold the mic in Slack instead of typing"),
             ("Oct 29, 2026", "This field guide"),
             ("Oct 2026", "Donna's new name and picture (she was the Service Desk)"),
             ("Oct 2026", "Handheld how-to help, in your channel or in a private message"),
             ("Oct 2026", "Today's stops and stop order for your route"),
             ("Oct 2026", "Garment questions: what a wearer has, and whether new garments are on the way")]


def _inline(t: str) -> str:
    return t.replace("{CHECK}", '<span class="ok">\u2714</span>')


def html(base: str = "") -> str:
    """The guide as a phone-friendly page in Snow White's colors (webchat serves it at /drivers/<DRIVER_GUIDE_KEY>)."""
    out, open_ul = [], False
    for block in GUIDE:
        kind = block[0]
        if kind == "section":
            out.append(f"<h2>{block[1]}</h2>")
        elif kind in ("p", "lead", "note"):
            out.append(f'<p class="{kind}">{_inline(block[1])}</p>')
        elif kind == "bullets":
            out.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in block[1]) + "</ul>")
        elif kind == "table":
            out.append('<div class="rows">' + "".join(
                f'<div class="row"><div class="what">{_inline(a)}</div><div class="say">{_inline(b)}</div></div>'
                for a, b in block[2]) + "</div>")
    cheat = "".join(f"<li><b>{w}:</b> {_inline(h)}</li>" for w, h in CHEAT)
    new = "".join(f"<tr><td>{d}</td><td>{w}</td></tr>" for d, w in WHATS_NEW)
    return PAGE.format(base=base, intro0=INTRO[0], intro1=INTRO[1], body="\n".join(out), cheat=cheat,
                       cheat_foot=_inline(CHEAT_FOOT), new=new, updated=WHATS_NEW[0][0])


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Donna Field Guide</title>
<style>
  :root{{--navy:#003746;--gold:#f9af10;--ink:#1c2226;--dim:#5a646b;--line:#d8dee2;--band:#f3f6f7;--card:#fff9ea}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:#fff;color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
  header{{background:var(--navy);color:#fff;padding:18px 16px 16px;border-bottom:4px solid var(--gold)}}
  header .top{{display:flex;align-items:center;gap:14px;max-width:760px;margin:0 auto}}
  header img.logo{{height:44px;background:#fff;border-radius:8px;padding:4px 8px}}
  header h1{{margin:0;font-size:24px;line-height:1.15}}
  header p{{margin:2px 0 0;color:#cfe0e5;font-size:13px;letter-spacing:.06em;text-transform:uppercase}}
  main{{max-width:760px;margin:0 auto;padding:16px 16px 40px}}
  .hello{{display:flex;gap:14px;align-items:center;margin:6px 0 4px}}
  .hello img{{width:84px;height:84px;border-radius:50%;flex:none;border:3px solid var(--gold)}}
  .hello .lead{{font-size:18px;color:var(--navy);margin:0 0 6px}}
  .hello p{{margin:0}}
  h2{{color:var(--navy);font-size:15px;letter-spacing:.06em;text-transform:uppercase;margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line)}}
  ul{{padding-left:20px;margin:8px 0}} li{{margin:5px 0}}
  p.note{{color:var(--dim);font-size:14px}}
  .ok{{color:#2e9e5b;font-weight:700}}
  .rows{{border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:8px 0}}
  .row{{display:grid;grid-template-columns:38% 1fr;gap:10px;padding:9px 12px;border-top:1px solid var(--line)}}
  .row:first-child{{border-top:0}} .row:nth-child(even){{background:var(--band)}}
  .what{{font-weight:600}} .say{{font-style:italic;color:#24343b}}
  .cheat{{background:var(--card);border:3px solid var(--gold);border-radius:12px;padding:12px 16px;margin-top:28px}}
  .cheat h2{{border:0;margin:4px 0 6px}} .cheat ol{{padding-left:22px;margin:6px 0}}
  table.new{{width:100%;border-collapse:collapse;font-size:15px}}
  table.new td{{padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}} table.new td:first-child{{white-space:nowrap;color:var(--dim);width:1%}}
  .foot{{color:var(--dim);font-size:13px;margin-top:24px}} a{{color:var(--navy)}}
  @media (max-width:520px){{.row{{grid-template-columns:1fr;gap:2px}} header h1{{font-size:21px}} header img.logo{{height:36px}}}}
  @media print{{header{{background:#fff;color:var(--navy)}} header p{{color:var(--dim)}} .noprint{{display:none}}}}
</style></head><body>
<header><div class="top"><img class="logo" src="{base}/logo/sw.png" alt="Snow White Linen Supply">
<div><h1>Donna Field Guide</h1><p>For Snow White drivers</p></div></div></header>
<main>
<div class="hello"><img src="donna.png" alt="Donna"><div><p class="lead">{intro0}</p><p>{intro1}</p></div></div>
{body}
<div class="cheat"><h2>Cheat sheet</h2><ol>{cheat}</ol><p>{cheat_foot}</p></div>
<h2>What's new</h2>
<table class="new">{new}</table>
<p class="foot noprint">Updated {updated}. Printable copy: <a href="guide.pdf">Donna Field Guide (PDF)</a>. Questions: the office, (907) 222-7738.</p>
</main></body></html>
"""
