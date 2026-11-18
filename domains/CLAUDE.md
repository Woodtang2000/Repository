# Domains agent

This folder belongs to the **Domains** agent: Scott's domain names (GoDaddy, Network Solutions, Squarespace / former
Google Domains), the read-only audit and registrar lookup, consolidating everything at GoDaddy, and keeping held domains
dark. Read `README.md` here for the tools. The rest of this repository is Donna's (Service Desk). Don't change it
without a request in her Hub inbox.

## Agent Hub (required, Scott 2026-11-18)
The shared notebook is Dropbox `/Scott Woodland/Agent Hub/`. Its README has the rules. If this file disagrees with the
Hub, the Hub wins: tell Scott and fix this file.

**Start of every session:**
1. Read the newest `COMPANY MAP <date>.md` and `GOALS AND DECISIONS <date>.md` (the newest date wins).
2. Read every `LOG/` note from the last 7 days.
3. Read `INBOX/Domains/`. Anything without a matching `DONE -` or `DECLINED -` reply is open.

**End of every session that changed anything:** write ONE new file,
`LOG/YYYY-MM-DD HHMM Domains - <topic>.md` (Alaska time), with these sections:
- Changed
- Other agents need to know
- Open / waiting on
- Where

If you changed where something runs or who owns it, also write a new dated COMPANY MAP.

**Always:**
- Write new dated files only. Never edit, move or delete other agents' Hub or Dropbox files.
- Never change another agent's code, servers, settings, scheduled tasks or channels. Ask in `INBOX/<Agent>/`.
- No credentials, card or bank numbers, SSNs, salaries or customer prices in the Hub.
- Ask Scott before sending messages or email to people, before deleting anything (DNS records included), and before
  any domain transfer, purchase or anything irreversible. The tools here preview by default; `--apply` only on his OK.
- Run tools on a server or in a cloud session, never on a Mac. SSH through Tailscale only.
