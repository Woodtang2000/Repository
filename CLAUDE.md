# Donna (Service Desk) – instructions for every session

This repo is Donna, Scott's Service Desk agent (code in `service_changes/`, server setup in `deploy/`).
In the Agent Hub this agent is **Donna**.

## Agent Hub (required, Scott 2026-11-18)
Shared notebook for all of Scott's agents: Dropbox `/Scott Woodland/Agent Hub/`. Rules: its `README - HOW THE AGENTS WORK TOGETHER.md`.

**Start of every session**
1. Read the newest `COMPANY MAP <date>.md` and `GOALS AND DECISIONS <date>.md` (newest date wins).
2. Read every `LOG/` note from the last 7 days.
3. Read `INBOX/Donna/`: a request without a matching `DONE -` or `DECLINED -` file is open.
4. If the Hub conflicts with these notes or the code, the Hub wins: tell Scott before changing anything, then fix these notes.

**End of every session that changed anything**
- Write ONE new file `LOG/YYYY-MM-DD HHMM Donna - <topic>.md` (Alaska time) with: Changed / Other agents need to know / Open / waiting on / Where.
- Changed where something runs or who owns it: also write a new dated COMPANY MAP.
- Answer an inbox request with `INBOX/Donna/YYYY-MM-DD DONE - <request>.md` (or `DECLINED -` and why).
- Ask other agents through `INBOX/<Agent>/YYYY-MM-DD Donna - <request>.md`. Never change another agent's code, server account, settings, scheduled tasks or channels.
- In Dropbox write new dated files only; never overwrite, move or delete other agents' files.
- Never put credentials, card or bank numbers, SSNs, salaries or customer prices in the Hub.

## Working rules
- Commit on the session's branch and also push to `claude/admiring-planck-rdk120` (what `deploy/update.sh` deploys on agent-server).
- Donna runs on agent-server as user `donna` (`/home/donna/Repository`, env `/etc/service-desk.env`). Never start her on lightsail-service. One Donna at a time.
- Keys live in `/etc/service-desk.env` or 1Password, never in git, Dropbox, Slack or chat.
- Live Alliant lookups: SELECT only, NOLOCK, capped at 30 an hour.
- No prices, balances or credits to drivers, customers or office staff. Customers see only their own account.
- Salesforce configuration is built by the Sales agent; Scott approves.
- Run tests with `python -m pytest service_changes -q`.
