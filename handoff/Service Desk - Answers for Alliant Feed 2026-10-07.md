# Service Desk - answers for the Alliant feed (2026-10-07)

Written by the **Service Desk Claude chat**, replying to "Alliant Feed - Direction 2026-10-06.md" (CFO chat). It
answers the three OPEN Service Desk items and gives the exact files the Service Desk needs from the Mac feed.
Status as of today: the Service Desk is **built and running** on test channels; go-live on the real route
channels is planned for **Oct 19**.

## 1. What the Service Desk answers and does (built, not proposed)

**Drivers, in their #route-N Slack channels**
- They post change requests ("add 2 3x5 mats at Midas", "new wearer Tim, XL shirts, 34x34 pants, 11 each").
  The bot asks at most 3 clarifying questions, then posts a clean **ticket** to the office channel with the
  Alliant account, item, SKU, wearer number, current → new count and frequency.
- The office keys the change into Alliant and reacts ✅; the bot sends the driver a readback naming the account
  and Alliant customer name ("✅ 1205-1-00004 MIDAS #4 (FAIRBANKS) – 2 mats added – entered by Sonja").
- **Lookups**, for customers on that driver's own route only: per-delivery counts and frequency, delivery days,
  stop, contact, card notes, wearers and garments. **No prices, balances or contract terms.** Each answer is logged
  to the office; 15 lookups per driver per day.

**Office, in the office channel**: the same lookups for any account, plus ✅ / "done" on tickets.

**Customers, by text** (Twilio toll-free (888) 519-7604, pending carrier verification): changes and lookups for
**their own account(s) only**. Each phone number is linked to its accounts by the office (self sign-up, office ✅
approval). Claude is only ever given that number's accounts. No prices, inventory, internal notes or route details.

**Not in scope:**
- **Writing to Alliant.** Alliant confirmed no API and no write-back, so the office keeps keying the changes; the
  bot's ticket is the work order.
- **Finance answers (open balance, pricing) to drivers or customers.** If Scott wants open balance in the office
  channel only, that is an easy add from `open_ar.csv`, but it would need read access to that one file.

**PROPOSED questions from the CFO file:**
- Wearer garments and sizes: already answered.
- Regular inventory, delivery days, route and stop: already answered.
- On hold / open stop requests: **wanted**, needs the holds data below.
- Open balance: not for drivers or customers (see above).
- When a wearer was added or stopped: nice to have, needs dates on the wearer file.

## 2. Platform, and whether it can read Dropbox

- Runs on an **Amazon Lightsail Ubuntu server** (always on), as Python services: a Slack Socket Mode listener,
  the customer-texting poller and an hourly silent review. It uses the Claude API.
- **It reads Dropbox through its own Dropbox app with "App folder" access.** It can see only
  **`Dropbox/Apps/SWL Service Desk/`** and nothing else, including nothing in Accounting. It checks every hour
  (at :17) and reloads when files change. Today it reads `Alliant Customer Record Cards.pdf` from that folder.

## 3. Where its files should go

**`Dropbox/Apps/SWL Service Desk/`**, outside Accounting, not `Accounting/Alliant Feed/Operations/`. The Service
Desk's Dropbox access can't reach Accounting, so this keeps finance and operations apart by design. The Mac feed
should write the Service Desk files there; the finance files stay in `Accounting/Alliant Feed/`.

## 4. Files the Service Desk needs from the feed

Plain CSV, UTF-8, with a header row, all written to `Apps/SWL Service Desk/`. Write each file under a temporary
name and rename it when done, then write `feed_done.txt` last (one line: the export time) so the desk never reads
a half-written set. These are the columns the desk loads today from the PDF; matching them means no parser
changes.

| File | Columns | Alliant source (from the Mac handoff) |
|---|---|---|
| `customers.csv` | account, name, route, service_days | `ricustmr` |
| `customer_cards.csv` | account, name, route, service_days, stop_sequence, contact, phone, email, special_instructions | `ricustmr` |
| `current_items.csv` | account, item, quantity (inventory), autocount (per delivery), sku, days, frequency | `riempitm` + `riskugrp` (non-wearer lines) |
| `wearers.csv` | account, employee, first, last, department (+ added_date, stopped_date if available) | `riemploy`, **never `em_ss_num`** |
| `garments.csv` | account, employee, sku, size, item, quantity, days, frequency | `riempitm` + `riskugrp` (wearer lines) |
| `holds.csv` (new) | account, type (hold/stop), start, end, reason | `OnHoldReasons`, `cws_StopRequests`, `amStops` (confirm contents first) |

- `account` is the full Alliant customer number, e.g. `1205-1-00004`.
- `service_days` and `days` use the Mon/Tue/… style separated by `;`.
- `frequency` is the Alliant frequency code.
- **Leave out of the Service Desk files:** prices (`ricustitm`), balances, sales rep, contract dates, stop minimum
  and SSN. The desk doesn't use them, and they shouldn't sit outside Accounting.

**Refresh:** hourly during business hours (about 6 AM–6 PM Alaska) is plenty. These are small, light queries.
Nightly is the minimum. Once the feed is running, the Service Desk chat will switch the server from the PDF to
these CSVs, keep the PDF as a fallback, and add a warning in the office channel if the data is more than about
36 hours old (for example, the Mac restarted and FileVault is waiting for a login).

## 5. Shared with the finance side

- **One feed, two destinations:** finance files go to `Accounting/Alliant Feed/`; Service Desk files go to
  `Apps/SWL Service Desk/`.
- **Same account format everywhere:** `account-branch-customer`, e.g. 6000-2-00000.
- **Customer contact phones:** customer texting will want mobile numbers from `ricustmr`, linked to accounts. The
  office approves every number before it gets access, so the feed can supply candidates but never grants access
  by itself.
