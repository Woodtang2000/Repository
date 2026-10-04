# Service change parser (prototype)

Reads a driver's message from a `#route-N` Slack channel and works out:

- **what kind of message it is**: item change, wearer change, hold or closure, special order, route or schedule, operational issue, or not a request
- **which customer it's for**: the route comes from the channel and the service day from the post date, which narrows the customer list
- **each change**: action, item, quantity, the driver's stated total, wearer, size, frequency, and start date
- **whether the numbers agree with Alliant**: for example, current 16 + 28 = 44 when the driver said 28 total
- **what to ask the driver**, when something is missing or doesn't add up
- **the readback** that staff post once the change is entered

Claude only reads and classifies the message (`parser.py`). The arithmetic and the readback text are plain code (`checks.py`), so the totals are always computed the same way.

## Files

| File | What it does |
|---|---|
| `schema.py` | The structured result for one message |
| `parser.py` | The prompt and the Claude call |
| `context.py` | Route and service day, plus loading the Alliant CSV exports |
| `checks.py` | Quantity checks against Alliant, driver questions, readback text |
| `run.py` | Command line: runs a batch of messages and prints the report |
| `alliant_report.py` | Converts Alliant's Item Usage report and Wearer Alpha List (Excel) into the CSVs below |
| `record_cards.py` | Parses the Customer Record Cards PDF text: autocount, stop sequence, contacts, special instructions |
| `bot.py` | Silent trial: one ticket per driver post in `#service-desk-test` |
| `desk.py` | Office workflow: questions to the driver, tickets to the office, ✅ → readback |
| `slack_manifest.yaml` | Slack app definition for the Service Desk bot |
| `examples/messages.json` | 27 real messages from the route channels (Jul–Oct 2026) |
| `examples/labels.json` | Hand-checked correct reading of each message, used to score the parser |
| `examples/demo_*.csv` | **Demo data only.** Placeholder accounts. Quantities were inferred from the Slack threads, not taken from Alliant |

## Running it

```bash
pip install anthropic pydantic pytest

# Unit tests (no API key needed)
python -m pytest service_changes

# Run the hand labels through the checks and readbacks (no API key needed)
python -m service_changes.run service_changes/examples/messages.json \
  --customers service_changes/examples/demo_customers.csv \
  --items service_changes/examples/demo_current_items.csv \
  --replay service_changes/examples/labels.json

# Let Claude read the messages and score it against the labels (needs ANTHROPIC_API_KEY or SERVICE_DESK_API_KEY)
python -m service_changes.run service_changes/examples/messages.json \
  --customers service_changes/examples/demo_customers.csv \
  --items service_changes/examples/demo_current_items.csv \
  --eval service_changes/examples/labels.json
```

## Alliant data

**Quickest setup (new session):** the Customer Record Cards PDF alone has everything the bot loads.
It lives in Dropbox at `/Scott Woodland/Accounting/Alliant Exports/Alliant Customer Record Cards.pdf`. Fetch its text
with the Dropbox connector (the result is saved as a JSON file), then:

```bash
python -m service_changes.record_cards <saved fetch result or .txt> --bot-data --out service_changes/data
```

That writes customers, current_items (with autocount), garments (wearer quantities are Qty Assigned),
wearers, customer_cards and card_lines. The Item Usage and Wearer Alpha List exports below are optional.

Export three Alliant reports to Excel: **Item Usage** (All SKU Groups, customer/employee order), the
**Wearer Alpha List**, and the **Customer Record Cards** (for each item's autocount). Then:

```bash
python -m service_changes.alliant_report Alliant_Item_Usage_Report.xlsx \
  --wearers Wearer_Alpha_List.xlsx --cards Alliant_Customer_Record_Cards.xlsx --out service_changes/data
```

This writes `customers.csv` (account, name, route, service_days, frequency), `current_items.csv`
(account, item, quantity, sku, days, frequency, unit_price, delivery_unit) and `garments.csv`
(account, employee number, sku, size, item, quantity) and `wearers.csv` (account, employee number,
first, last, department). `service_changes/data/` is kept out of git
because it holds customer pricing.

Run the parser against it with `--data service_changes/data`.

When Alliant already shows the total the driver asked for, the change is reported as "already entered"
rather than raised as a question.

Drivers talk about the **autocount** (what gets delivered). Inventory on some accounts is double the
autocount. The autocount comes from the **Customer Record Cards PDF**. Save the PDF's text (a Dropbox
fetch of the PDF returns it) and run:

```bash
python -m service_changes.record_cards record_cards.txt --out service_changes/data
```

This writes `customer_cards.csv` (route, stop sequence, contact, phone, email, special instructions, sales
rep, install date, contract expiry, stop minimum) and `card_lines.csv` (every item and garment line with
its autocount). `--data` loads them automatically; the autocount is matched to items on account, SKU and
inventory (about 98% of item lines). Where no autocount was found, the checks accept inventory or half of
it, whichever fits the driver's numbers. (The `--cards` option of `alliant_report.py` reads the Excel
conversion of the cards instead, which loses every header after the first; use the PDF when you can.)

## Office ticket

For every change the report (and later the bot's Slack reply) includes a ticket with what the office needs
to key it in: customer name, Alliant account, route and stop, each line's Alliant item name and SKU, the
wearer number for garments, the autocount before and after, the frequency, and any delivery note from the
record card. Drivers don't have to supply any of it. The same fields are what entering changes into
Alliant automatically would need.

## Nicknames

`aliases.csv` lists other names drivers use for a customer (`account,also_called`, e.g. `1003-1-00000,BSI`).
The office can add a line whenever the bot fails to recognize a name. They're shown to the AI next to the
customer and used for exact matches.

## The bot (`bot.py`)

One run reads the last `--since-minutes` of every `#route-N` channel the bot is in, and for each driver
message posts the office ticket. **Silent mode** (default) posts to `#service-desk-test` only, with a link to
the original message, so nobody else sees anything. `--live` replies in the driver's thread instead.
`--dry-run` prints instead of posting. Each post carries a `ref <channel>/<ts>` tag, so overlapping runs
never post twice.

```bash
pip install anthropic pydantic slack_sdk
python -m service_changes.bot --data service_changes/data --since-minutes 70 --dry-run
```

Needs `SLACK_BOT_TOKEN` (from the Slack app made with `slack_manifest.yaml`) and `ANTHROPIC_API_KEY` or
`SERVICE_DESK_API_KEY`. The bot must be invited to `#service-desk-test` and each `#route-N` channel.

For the silent trial it runs as a scheduled Claude Code routine: rebuild the data from the Dropbox PDF, then
one pass. Instant replies later need an always-on host running it every minute or two (or a Socket Mode
listener; the app manifest already enables it).

## The office workflow (`desk.py`)

The go-live design: drivers talk to the bot in their route channel, the office only sees clean tickets, and nobody
has to use threads.

1. A driver posts a change. The bot reacts 👀 (picked up).
2. If something is unclear it asks in the channel, @mentioning the driver (at most twice). The driver answers in
   the channel; Claude checks whether their next message is the answer or a new request.
3. The ticket goes to the office channel (`--desk`): account number and name, then one line per change
   (➕ Add 20  APRON BIB WHITE · 10 → 30 · weekly), the driver's words, and a link to the route channel.
   Route moves and problems go over as FYI tickets so the office can mute the route channels.
4. The office enters it in Alliant and reacts ✅ on the ticket or types "done" in the channel (with the account
   number if several tickets are open). Words after "done" go to the driver as a note.
5. The bot posts the readback in the route channel, @mentioning the driver, and marks the ticket
   "✅ Entered by <name> · readback sent".
6. Drivers can also ask about an account ("how many bar mops does Humpy's get?"). The bot answers in the channel
   from the Alliant export (items with per-delivery autocount and frequency, wearers and sizes, stop, contact, card
   note; never prices or contract terms) and says which export date it used. Every answer is also logged in the office
   channel (🔎 who asked, which account, what they got). If the data doesn't say, the office gets a ❓ ticket. Questions
   about a customer on another route aren't answered; they go to the office. After 15 questions from one person in a day the bot stops answering and flags it to the office.
7. A correction after the readback comes back as a 🔁 correction ticket; a change before the office finished
   marks the open ticket 🚫 Replaced.

```bash
# Test channels: create #route-12-test (route 12's customers), invite @Service Desk there and to #service-desk-test
# Real time (needs SLACK_APP_TOKEN, the xapp- token): ⏳ within a second, full backup pass every 5 minutes
python -m service_changes.desk --data service_changes/data --desk service-desk-test --routes route-12-test --listen
# Or poll every 30 seconds (no app token needed)
python -m service_changes.desk --data service_changes/data --desk service-desk-test --routes route-12-test --watch 30
```

Speed: Slack pushes each message over Socket Mode, the bot reacts ⏳ at once and swaps it for 👀 when it has read the
message. The instructions and each route's customer list are prompt-cached, so only the message itself is new to
Claude, and a one- or two-word answer right after a question skips the "is this an answer?" check.

State lives in Slack (👀 reactions and message metadata on the bot's posts), so a pass can rerun safely and
`--watch N` repeats it every N seconds. Real `#route-N` channels need `SERVICE_DESK_LIVE=1`; `#route-N-anything`
channels are treated as tests. Office staff are recognised from `office_staff.txt`; their replies in a driver's
thread are context, not new requests.

## Not built yet

- Writing changes into Alliant
