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
