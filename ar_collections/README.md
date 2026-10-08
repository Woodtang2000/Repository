# AR collections agent

Friendly past-due reminders for Snow White Inc (NetSuite sub 8) and The Laundry Group LLC (sub 9), drafted for
Sonja Burke to review and send. The rules come from Scott's briefs in Dropbox
`Accounting/CFO/AR Collections Agent - brief 2026-10-08.md` and `... brief v2 2026-10-08.md`; read those first.

The agent never writes to NetSuite or Alliant, and never sends email itself (until Scott says otherwise).

## Each weekday morning

1. **Pull the data** (read-only) into a scratch folder:
   - NetSuite open invoices → `ns_open.json` (list of rows), 4 pages of 1000:
     ```sql
     SELECT t.id, t.tranid, t.externalid, t.subsidiary sub, c.entityid code, c.companyname nm,
            BUILTIN.DF(c.parent) parent, t.trandate, t.duedate, t.memo, t.foreignamountunpaid bal
     FROM transaction t JOIN customer c ON c.id = t.entity
     WHERE t.type = 'CustInvc' AND t.foreignamountunpaid > 0 ORDER BY t.id
     ```
   - Dropbox `Accounting/Alliant Feed/`: `customer_detail.csv`, `open_ar.csv`, `receipts_to_post.csv`, and
     `payments_YYYY-MM.csv` for the last 3-4 months (Dropbox `fetch`; direct download links are blocked by the
     network policy).
   - Dropbox `Accounting/AR Collections/`: every file in `log/` (into `log/`) and `holds.csv`.
2. **Build:** `python3 -m ar_collections.build <data_dir> <out_dir> YYYY-MM-DD`
3. **Deliver:**
   - drafts → the accounting mailbox (Superhuman `create_or_update_draft` as accounting@…) when connected, else
     Dropbox `AR Collections/drafts/YYYY-MM-DD/`;
   - `log_YYYY-MM-DD.csv` → Dropbox `AR Collections/log/YYYY-MM-DD.csv` (the Dropbox connector can only create
     files, so the log is one file per day);
   - Slack summary to the Scott–Sonja DM (D4KJ50AP4): "Today's collection batch: N customers, $X past due.
     Drafts are in …". No bank or card details, ever.
4. **Monday:** also the weekly progress file (`Weekly progress YYYY-MM-DD.md`) against the 10/8 baseline.

## What the build does

| Rule | Where |
|---|---|
| Age by original Alliant date (opening invoices: date in the memo, due = +30); over-45 = invoice > 45 days old | `load_invoices`, `main` |
| One message per parent account (NetSuite parent = Alliant account number), combined again when accounts share a billing email | `main` |
| Last payment from Alliant payments (write-offs excluded), at parent level; 45+ days → "no payment" reminder, otherwise the "may have slipped through" note | `main`, `draft` |
| Skip Unifirst accounts (Safeway, Costco, LSG; balances listed for Scott), COD remnants, holds, check-first accounts, receipts at the bank not yet applied, inactive customers | `UNIFIRST`, `COD_ACCOUNTS`, `CHECK_FIRST`, `main` |
| Items dated 2022-2024 are listed as write-off candidates, never chased | `WRITE_OFF_BEFORE` |
| A reminder only lists invoices Alliant still shows open, at the lower of the two balances | `alliant_open` |
| Contact the business day before the first delivery day of the week; no day → Monday batch (Friday contact) and on Sonja's fix list | `contact_day` |
| One contact per customer per week; week 1 reminder, week 2 follow-up, week 3 flag for a call | `log`, `step` |

Outputs in `<out_dir>`: `working_file.csv` (all over-45 customers), `batch_YYYY-MM-DD.csv`, `drafts/YYYY-MM-DD/*.md`,
`log_YYYY-MM-DD.csv`, `unifirst_balances.csv`, `writeoff_candidates.csv`, `sonja_fix_in_alliant.csv`.
