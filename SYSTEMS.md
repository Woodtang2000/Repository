# What we've built: the system map

Everything Scott and Claude have built for Snow White Linen Supply (Snow White Inc) and The Laundry Group LLC: what
each process does, what it works with, who it works for, where it runs, and how to tell it's healthy.

**Keep this file current.** Any session that adds, changes or retires a process updates this file in the same commit
(see `CLAUDE.md`). Last full review: 2026-10-25.

## Where things run

| Place | What runs there | Code lives in |
|---|---|---|
| **Lightsail server** `service-desk` (always on) | Service Desk, texting, email, website chat, hourly Dropbox sync | This repo, branch `claude/admiring-planck-rdk120` (`deploy/update.sh` pulls it) |
| **Cloud scheduled tasks** (claude.ai, fresh session each run) | CFO nightly posting, revenue file, payroll checks, AR collections, digests, watchdog | Prompt in the task; scripts in **Dropbox** `agent-files/` folders (not in git) |
| **Plant Mac mini** | Nightly Alliant SQL export, Alliant → Salesforce sync, Sales Manager, route report | On the Mac (`~/snowwhite-sales-sync` etc.), not in this repo |
| **Claude chat sessions** | Inbox sweep, domain audit, one-off work | This repo (`.claude/skills/`, `domains/`) |

## 1. Service Desk (Slack bot) — `service_changes/`

The center of the operation. Runs on the Lightsail server as `service-desk` (Slack Socket Mode, `desk.py --listen`).

| Feature | Who uses it | Channel | What it does |
|---|---|---|---|
| Service changes | Drivers → office | `#route-N` → `#service-desk` | Reads a driver's post (Claude), asks ≤2 clarifying questions, posts a clean ticket (account, Alliant item/SKU, wearer, count before → after, frequency). Office keys it into Alliant, reacts ✅, bot sends the readback to the driver. Corrections 🔁, replacements 🚫 |
| Account lookups | Drivers (own route only, 15/day), office (any account, incl. DMs) | route channels, `#service-desk`, DMs | Items with autocount and frequency, wearers and sizes, stop, contact, card notes. Never prices or contract terms. Every driver lookup logged to the office |
| Route questions | Drivers, office | route channels, `#service-desk` | Today's stops, who's next, stop order by day, non-weekly frequencies |
| Garment lookups | Drivers, office | route channels, `#service-desk` | Barcode history (washes, repairs, last scan, status), open garment POs and expected dates, find a wearer by name |
| Service codes | Drivers → office | route channels | Repair / inspection / relabel / inventory requests become tickets with the exact Alliant steps (`service_codes.py`) |
| Alliant how-to | All employees | `#alliant-help`, DMs | Answers from the Alliant University notes; tags Scott and logs to `alliant_unanswered.jsonl` when the notes don't cover it (`alliant_help.py`) |
| Entry check | Office | `#service-desk` | Morning after a ✅, compares the ticket with the Alliant export and flags mismatches (`verify.py`) |
| Weekly garment check | Ana Faamita (stockroom) | `#stockroom`, Mondays | Wearer lines with inventory above autocount and nothing on order (`garment_check.py`) |
| Data cleanup lists | Scott / office | run by hand | `ratio_check.py` (inventory not equal to or double the autocount), `garment_check.py` |
| Stale data / Claude down warnings | Office | `#service-desk` | Data older than 36 h; Claude out of credit or a bad key |

**Works with:** Slack, Anthropic API (Claude), the Alliant data (below). **People lists:** `office_staff.txt`
(Ana Faamita, Sonja Burke, Bill Dubinsky, Scott Woodland), `plant_staff.txt` (Kirk Kosiek, Michael Morales, Maria
Aukuso: their notes to drivers are left alone). Nicknames for customers: `aliases.csv`.

**Does not do:** write to Alliant (it has no API). The office still keys every change; the ticket is the work order.

### Customer texting — `sms.py` (service `service-desk-sms`)
Customers text (866) 827-8398 or (888) 519-7604 (Twilio). Each phone is pinned to its accounts in
`data/customer_phones.csv`, approved by the office; Claude only ever sees that number's accounts. Tickets go to the
office like driver tickets; ✅ texts the readback back, signed Snow White Linen or The Laundry Group by account branch.
Legal pages for carrier verification: `legal/sms-terms.html`, `legal/sms-privacy.html`. Flyers (opt-in proof): `flyer/`.

### Customer email — `email_desk.py` (service `service-desk-email`)
Same as texting, by email through **Bird** (inbound address + sending domain mail.snowwhitelinen.com). Senders pinned in
`data/customer_emails.csv`; SPF/DKIM failures are flagged. Staff can forward a customer's email in.

### Website chat and sales desk — `webchat.py`, `sales.py`, `salesforce.py`, `website_chat/` (service `service-desk-webchat`)
Chat bubble on snowwhitelinen.com and thelaundrygroup.com (served from chat.snowwhitelinen.com behind Caddy).
Answers from the owner-approved fact sheets only; never sees customer data. New business → `#sales-desk` with
**Give to Bill / Jesse / Scott** buttons: a click looks the business up on the web and creates the **Salesforce**
Account, Contact and Prospecting Opportunity. Existing-customer requests → `#service-desk`. Has rate limits and a daily cap.

### Alliant data feed (what the desk knows)
The plant Mac writes the nightly Alliant SQL export (customers, cards, items, garments, wearers, holds, garment
tracking) to `Dropbox/Apps/SWL Service Desk/` at 11:45 PM. `deploy/dropbox_sync.py` (hourly cron) installs a new set
when `feed_done.txt` changes, after sanity checks. Fallback: the Customer Record Cards PDF (`refresh_data.sh`).
The Alliant University notes sync the same way. Data lives in `service_changes/data/` (never in git: it has pricing).

## 2. Back-office agents (cloud scheduled tasks)

All run as fresh sessions; their scripts and rules are read from Dropbox each run. Times are Alaska time.

| Task | When | Works with | Reports to | What it does |
|---|---|---|---|---|
| **Alliant → NetSuite nightly posting** (the CFO agent) | Daily 5:37 AM | NetSuite, Dropbox, Slack, Jobber | `#cfo-desk`; Sonja (payments to apply) | Posts Alliant invoices, credits, payments, deposits, Jobber (TLG) invoices, routine bank lines and SW insurance entries within standing authority. Rules: newest `CFO/PLAYBOOK current *.md` |
| **customer_revenue.csv nightly** | Daily 2:52 AM | NetSuite, Dropbox | `#cfo-desk` only on failure | Rebuilds `Alliant Operations/customer_revenue.csv` for the Sales agent |
| **AR Collections daily batch** | Weekdays 7:52 AM (15:52 UTC) | NetSuite, Dropbox, Gmail (accounting@), Slack | Scott/Sonja DM | Reminder email **drafts** (never sent), contact log, batch list for Sonja; Monday progress report. Code: branch `claude/loving-mccarthy-t3axhf`, `ar_collections/` |
| **Weekly reconciliation check** | Mondays 8:34 AM | NetSuite (read-only) | `#cfo-desk` | Which bank/card accounts have old uncleared items and which statements are ready |
| **Daily times check for Bill** | Weekdays 6:52 AM | Deputy, Dropbox, Slack | `#hr-desk` (Bill, Scott) | Out-of-norm Deputy timesheets + PDF in house style; Mondays the routes-and-drivers PDF |
| **Pre-payroll Deputy check** | Hourly on 1st–4th and 16th–19th; acts once Bill says "payroll ready" | Deputy, NetSuite, Dropbox, Slack | Scott + Bill DM | Full closed-period check; the only write is fixing a leave timesheet's area through `.claude/deputy_write.py` |
| **Sales digest to Slack** | Daily 7:56 AM | Dropbox (Mac sync output) | `#sales-digest` (Scott, Bill) | Overnight Salesforce sync results, per-rep activity, scoreboard |
| **Morning agent watchdog** | Daily 7:22 AM | Dropbox, scheduled tasks list | `#cfo-desk` | One line: did the Alliant feed, NetSuite posting, revenue file, sales sync and sales manager all run? |

Retired or paused (kept for history): the old session-bound versions of the posting, revenue and payroll tasks,
"CFO inbox filing", and "Rep morning briefs to Slack".

## 3. Other tools in this repo

| Tool | Who | What |
|---|---|---|
| `.claude/skills/inbox-sweep/` | Scott | Marks notification email Done across every Superhuman account; Scott's overrides in `rules.md` |
| `domains/` | Scott | Read-only GoDaddy audit of held domains; `--dark --apply` takes them dark and locks them |
| `brand/brand.py` | All PDF reports | House style (logos, navy/gold) for generated PDFs |
| `brand/icons/` | Slack | Service Desk and AP Agent app icons |
| `handoff/` | Other Claude chats | Notes passed between the Service Desk and CFO chats |

## Keeping it working

**Check health**

- Tests: `python -m pytest service_changes domains` (110 pass as of 2026-10-25; needs `anthropic pydantic slack_sdk pytest httpx`).
- Server: `journalctl -u service-desk -f` (also `-sms`, `-email`, `-webchat`), `tail ~/dropbox_sync.log`.
- Overnight agents: the watchdog's ✅/⚠ line in `#cfo-desk` every morning.

**Known gaps to close**

1. The watchdog doesn't cover the Lightsail server (desk, texting, email, chat). If the server stops, nothing says so.
2. AR collections code is on its own branch (`claude/loving-mccarthy-t3axhf`), not merged with the rest.
3. The scheduled tasks' scripts live in Dropbox `agent-files/`, outside git, so there's no history of changes to them.
4. The Mac mini jobs (Alliant export, Salesforce sync, Sales Manager, route report) aren't documented here beyond
   their outputs.

**Making it better**

- 👎 reactions and `alliant_unanswered.jsonl` show where the desk is wrong or the notes are thin.
- The entry check and garment/ratio checks show where Alliant data needs cleanup.
- Once a quarter, ask Claude: "Review SYSTEMS.md against the repo and the scheduled tasks, update it, and list what's
  broken or stale."
