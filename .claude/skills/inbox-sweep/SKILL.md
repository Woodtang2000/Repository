---
name: inbox-sweep
description: Clear notification and marketing email out of Scotty's Superhuman inboxes by marking it Done, across every linked account, and report the few threads that need him. Use when asked to sweep, clean, triage or clear the inbox, get to inbox zero, or clear out notifications.
---

# Inbox sweep

Goal: get every linked Superhuman inbox as close to zero as possible without
Scotty losing anything he needs to act on. Most of his mail is automated
notifications about the businesses. Those get marked **Done** (archived; never
deleted, still searchable). Real conversations and anything that needs action
stay in the inbox.

The rules below are the defaults. `rules.md` in this folder holds Scotty's own
overrides (senders that are always Done, senders that always stay). **Read
`rules.md` first; it wins over the defaults.**

## What you may do

- `update_thread` with `mark_done: true` (and `mark_read: true`) only.
- Never trash, mark spam, unsubscribe, reply, send, draft, or change labels
  unless Scotty asks in this conversation.
- When unsure, leave the thread in the inbox. A missed clear costs a second; a
  wrong clear can cost a customer.

## Steps

1. `list_accounts`. Sweep **every** linked account, not just the primary.
2. For each account, page through `list_threads` with `labels: ["INBOX"]`,
   `limit: 50`, following `next_cursor` until it runs out. Run accounts in
   parallel. Results are large; if a result is saved to a file, read it with
   `jq` (sender of the last message, subject, labels, message count,
   `user_is_participant`, thread_id, last_message_id).
3. Classify each thread **DONE** or **KEEP** using the rules below. Use the
   snippet; open the thread with `get_thread` only when the snippet doesn't
   settle it.
4. **Preview mode (the default):** show the plan and change nothing. Use one
   table per account. List KEEP threads first, each with a few words on why,
   then DONE threads grouped by type with counts. Ask Scotty to confirm. He
   may move items between the lists; if he says "always" about a sender or
   type, add it to `rules.md`.
   **Go mode** (Scotty says "go", "just do it", "sweep it", or it runs on a
   schedule he set up as auto): skip the confirmation.
5. Mark the DONE threads: `update_thread(thread_id, last_message_id,
   mark_done: true, mark_read: true, acting_email: <account>)`. Run them in
   parallel. If one fails because the thread got a new message, re-read it and
   classify it again.
6. Report briefly: how many were cleared per account and by type, then the
   **Needs you** list (KEEP threads), most urgent first, one line each with
   account, sender, subject and why.

## Mark DONE: automated notices nobody needs to act on

The sender is a system (no-reply, notifications@, alerts@, billing systems,
marketing platforms) **and** the message only informs. Typical:

- **Security and sign-in:** Google "Security alert" / "new sign-in" / "You
  allowed X access", recovery-email copies of alerts, NetSuite "Additional
  Authentication Provided", verification codes and password-reset
  confirmations older than an hour.
- **Money in or scheduled:** payment confirmations, remittance advice, "payment
  will be deposited", State of Alaska payment notifications, Paymode, Bill.com
  deposits, IRS EFTPS *scheduled* confirmations, autopay receipts.
- **Statements and bills on autopay:** "your bill is ready to view", monthly
  statements, "your subscription is renewing", "your domains are set to
  continue", policy documents available.
- **Shipping:** ordered, shipped, delivered.
- **Marketing and news:** promotions, newsletters, sponsored messages, vendor
  sales, product and feature announcements, webinars, "get started with",
  job-board alerts, rewards points, community or event mass mailings. Labels
  like `CATEGORY_PROMOTIONS`, `[Superhuman]/AI/Marketing` and
  `[Superhuman]/AI/News` are strong hints.
- **Info-only system notices:** planned outages, "we received your update",
  "finish setting up your account" from tools he hasn't asked about, calendar
  invitations for events that have already passed.
- **Repeat reminders:** when the same automated reminder appears several times
  (e.g. four "your practitioner needs additional information"), keep the newest
  and mark the older copies Done.
- **Already handled elsewhere:** a reminder for something another email shows
  is done (a "donations due" notice when a donation receipt is in the inbox; an
  "approve your child's account" after the "thanks for approving" email).
- **Acknowledgment-only replies:** a person replying only "received, I'll get
  back to you" to something Scotty sent. Nothing is waiting on him. Leave it
  if the thread carries a snooze or reminder label he set; mention the label
  in the report.
- **Events that have already happened:** invitations, reminders and
  registrations for dates in the past.
- **Copies of his own automation:** form-submission echoes and
  CustomerConnect confirmations sent from his own service@ addresses.

## KEEP: leave it in the inbox

Any one of these keeps the thread, even if it looks automated:

- **A real person wrote it:** a customer, vendor rep, employee, Bill, family,
  a board, an attorney or accountant. A message forwarded by a person counts
  too ("Fwd: … please pay").
- **Scotty already replied in the thread** and the other side answered, or the
  thread carries `[Superhuman]/AI/Respond`, `[Superhuman]/AI/Waiting`, a
  reminder (`reminder_returned`), a draft (`has_draft`), or a star.
- **Money going wrong:** past due, second or final notice, collections,
  returned or failed payment, declined card, card expiring on an autopay,
  "expired, renew now", an invoice that needs paying.
- **Action or deadline:** "action required", "needs additional information",
  open task reminders, compliance or inspection due (backflow, licenses,
  taxes owed), government or legal notices that ask for something, rejected
  or denied applications (e.g. Twilio verification), error reports from his
  systems (Salesforce flow errors, sync failures).
- **Signed or executed contracts and agreements**, insurance changes that need
  a decision, and meeting requests or calendar invitations still in the
  future.
- **Security alerts he didn't cause:** a sign-in from an unexpected device or
  place, or an alert that doesn't fit what he was doing. Alerts from his own
  account-linking are DONE.
- **Google Group spam-moderation digests:** real customer mail can be held
  there.
- **Anything you can't classify with confidence.** List these separately in
  the preview as judgment calls so Scotty can decide, and add his answer to
  `rules.md`.

## Tone of the report

Short. Scotty wants to get to inbox zero, not read a newsletter about his
newsletters. Counts for what was cleared; one line per thread that needs him.
