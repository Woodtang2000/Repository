# Par counting for the hospitals (replacing Linen Helper) - decisions so far

Scott, 11/23-11/26. Spec in progress: nothing is built yet.

## What it is
- The Laundry Group's hospital linen techs count what's on each delivery point's shelf on a phone. The app works out
  **need = par − on hand** and adds every delivery point into one order for the hospital's main linen room.
- Customers to start with: **6001-2-00000 Alaska Regional** (the pilot), then **ANTHC 6000-2-*** (ANMC, ULMC, ANPCC, Mt
  Marathon, Gottlieb, APMB, HCB, EDC, Airline Center, Egan Center). Later, a hotel version for the four new Pursuit
  hotels, to help them distribute to their buildings and to give us an early read on production.
- Reports for each hospital: par utilization by delivery point and item, and pounds per adjusted patient day.

## Decisions
- **Production never waits on Alliant or Donna's workstation.** Route makeup gets the combined pull list (and the
  per-delivery-point breakdown) straight from the app as soon as counts are done.
- **Alliant gets building totals only** (Scott, 11/26: "building totals are enough for now"). Donna enters one Request
  Entry per building customer with the combined quantities, before production starts loading carts. The
  per-delivery-point detail lives in the app and the reports.
- **Never involve TEXO / Alliant support in this project** (Scott, 11/26: "They own linen helper"). No questions to
  them about imports, APIs or anything that hints we're building our own par counting.
- Alliant's "Standard" quantities are deliveries, not shelf pars: the pars come from Linen Helper's export.
- Phones in hospital basements may lose signal: counts save on the phone and send when they can.

## Waiting on
- Linen Helper export of Alaska Regional's delivery points and pars (Scott).
- The cart-building cutoff time, and whether counts happen every day including weekends (Scott).
- The delivery unit list in Alliant (Donna's read-only screen task; waits on the Alliant Microsoft sign-in reset).
- Guardrail: add delivery units, standard quantities and cart weights to the nightly Alliant data (needs Scott's OK).

## Update 2026-11-26
- **Built (commit 05a8627, e4324f6):** Linen Helper import, the techs' counting page (offline saves), the combined
  pull list in #tlg-alaska-regional tagging Chris Gowen ("ready to build the carts"), and Donna's Alliant building
  totals behind PAR_ALLIANT. Not deployed for use yet.
- **Hospitals on hold:** Scott may get a direct read from Linen Helper. Then the techs keep counting in Linen Helper,
  and the pull list, Chris's notice, the Alliant entry and the reports run off Linen Helper's counts instead.
- **Pursuit is the reason to keep building** (Scott, 11/26: "something that pursuit can use in there operations next
  summer"). Target: ready before summer 2027. A customer version:
  - each hotel gets its own private link showing only its own account;
  - delivery points are their buildings or linen closets, with their own pars;
  - their counts give them a distribution list, and give us an early demand signal for production.
- To settle with Scott before spring:
  - do Pursuit's counts become orders, or planning only;
  - who uses it (their housekeeping, or our driver);
  - their accounts (Pursuit 6021) and buildings;
  - who sets the pars.

## Pursuit's flow (Scott, 11/26)
"The linen gets delivered to a central location. The housekeepers take the count then the linen runner takes each
building what they need to fill in what is missing from the pars in the linen room. We get the numbers back at
production which tells us how much they are using and allows us to forecast bulk deliveries or let Pursuit know that
we need to inject linen."
- Housekeepers count each building's linen room against its pars, in the app (their own private link).
- The linen runner gets a run sheet: per building, what to take from the central location to bring it to par.
- Production gets the counts as daily usage per item, building and hotel. These are planning numbers, not orders:
  - for forecasting bulk deliveries;
  - for a linen injection alert to Scott when usage outgrows the linen in circulation. This is best if the central
    room is counted too: buildings' total need more than what's in the center means inject linen.
- Still open: does Pursuit count the central room; which Pursuit 6021 locations are the four hotels and their
  buildings or linen rooms; who sets the pars.

## Pursuit's lodges (Scott, 11/26) - a COG (customer-owned goods) account
| Lodge | Setup | Notes |
|---|---|---|
| Seward Windsong Lodge | Multi-point: about 12 buildings of about 12 rooms, each with its own linen room and pars | Central delivery at Pursuit's Seward laundry (being shut down), or maybe the lodge itself; works the same |
| Talkeetna Lodge | One linen room count (about 250 rooms) | Traditional hotel |
| Denali Backcountry Lodge | One linen room count | Supplied from Talkeetna Lodge, driven to Healy |
| Kenai Fjords Lodge | One linen room count | Supplied out of Seward |

- **Central room count:** not counted, unless they want an inventory (optional in the app).
- **Runner flag:** the linen runner says when supply is shrinking or deliveries are getting harder to fill, with a
  button and an optional note. It goes to Scott and production.
- **What we bring:** set by usage. Donna shows the usage trend per lodge for production.
- **Injection:** COG, so injection means Pursuit adds linen. Donna alerts Scott when usage outgrows what's in
  circulation, or when the runner flags it.
- Still open: who sets the pars, and which Pursuit 6021 customer numbers are which lodge.

## Built from the linen room down (Scott, 11/26)
- **Linen room first:** each linen room is an Alliant account (the central linen room). **Delivery units** (buildings,
  floors, departments) are added under it, each with its own pars.
- **Totals roll up:** every unit's need adds up to the linen room's account. That total is what production builds and
  what goes to Alliant.
- **No units yet:** the linen room itself is counted, which covers Talkeetna, Denali Backcountry and Kenai Fjords. Add
  units later and they take over; the linen room's own pars are kept.
- **Who sets up:** setup is on the page at the managers' link, `/par/<PAR_ADMIN_KEY>/`, with the same page and a "Set up"
  button. The techs' link (`PAR_KEY`) can only count, and the server enforces that.
- **Linen Helper export:** still works for the hospitals. It fills in the delivery units of a linen room.

## No Slack channel (Scott, 11/26)
- **No channel post:** the pull list isn't posted in a channel. It stays on the page (printable).
- **Chris gets one direct message** (`PAR_NOTIFY`, Chris Gowen by default):
  - "✅ <linen room>: counts are done and entered in Alliant for <day>" once Donna's Request Entry is done.
  - If Donna couldn't enter them, or isn't working in Alliant (paused, or `PAR_ALLIANT` off), the message says the
    counts are done and lists the totals for him to enter by hand.
  - If everything was at par, it says so.
