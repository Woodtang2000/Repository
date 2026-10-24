# Held domains (GoDaddy)

The domains are held, not used publicly. `python -m domains.audit` checks every domain in the GoDaddy account
and reports anything that makes a domain look used, or puts it at risk of being lost.

## A held domain should

| Setting | Why |
|---|---|
| Show nothing: no `@` A record, no GoDaddy parked page | GoDaddy parks new domains on an ad page by default. Ads on a page at another organization's name are the main evidence of bad faith in a UDRP complaint or a cybersquatting (ACPA) claim |
| No for-sale listing (Afternic, Sedo, Dan) | An offer to sell to the name holder or its competitor is the other main evidence of bad faith |
| Auto-renew on | A lapsed domain goes to whoever buys it next |
| Privacy on, transfer lock on | Keeps the owner details private and the domain from being moved out |

Holding a domain without using it doesn't fully protect you. A UDRP panel can still find bad faith in a "passive
hold" of a well-known name (Category A and B). Those categories are worth reviewing with counsel.

## Setup (once)

1. Sign in at https://developer.godaddy.com/keys and create a **Production** key. Save the key and secret.
   GoDaddy only allows the Domains API for accounts with 10+ domains (or a Domain Pro plan).
2. Put them where the audit runs, as `GODADDY_KEY` and `GODADDY_SECRET`: in `/etc/service-desk.env` on the
   server, or as environment secrets for a cloud session. A cloud session also needs `api.godaddy.com` added
   under Network access → Allowed domains.
3. Fill in `domains/categories.csv` (`domain,category`, with category A, B or C). Unlisted domains show under
   "Not categorized".

## Use

| To | Run |
|---|---|
| Audit (changes nothing) | `mkdir -p domains/out && python -m domains.audit > domains/out/audit.md` |
| See which parking records would go | `python -m domains.audit --fix` |
| Delete them (domains then show nothing) | `python -m domains.audit --fix --apply` |

`--apply` only deletes records that point to GoDaddy parking addresses or parking hosts. Auto-renew, privacy,
lock and marketplace listings are reported, not changed: fix those in GoDaddy (My Products → the domain).
