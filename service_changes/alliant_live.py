"""Live read-only Alliant lookups for Donna (spec: Dropbox "Alliant Operations/Donna - live Alliant read-back spec
2026-11-04.md"). Donna runs on agent-server, which reaches Alliant's SQL Server over the TEXO VPN.

A FIXED list of lookups, each a parameterised SELECT taken from the Alliant Feed scripts (alliant_service_desk.py,
alliant_export_mac.py, sku_pricing_export.py) and scoped to one account, garment or SKU. Donna (the model) can only pick
a lookup and pass its parameters; she never writes SQL.

Guardrails (all required by the spec):
- SELECT only, every table WITH (NOLOCK), parameters bound by the driver; the session is never committed.
- 15-second query timeout; at most ALLIANT_LIVE_PER_HOUR (30) lookups an hour across all of Donna's services; results
  cached 2 minutes; one query at a time (a lock file shared by every Donna process).
- Each lookup is logged (name, parameters, rows, seconds) to <data>/alliant_live.log; never the login.
- Credentials: ALLIANT_SQL_USER / ALLIANT_SQL_KEY in /etc/service-desk.env (Scott enters them). Never printed: they
  are scrubbed from any error text.
- Fallback: VPN / SQL unreachable, not configured, or the hourly limit hit -> the hourly Service Desk files, marked
  "may be up to an hour old".
- Price and cost fields (the SKU lookup) are for Scott or the office only: summary() drops them for anyone else.

  python -m service_changes.alliant_live sample 1205-1-00004 5045-1-00000 A123456   # 2 accounts and a barcode
  python -m service_changes.alliant_live describe     # INFORMATION_SCHEMA columns for the fields still to map
"""
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import time

SERVER = os.environ.get("ALLIANT_SQL_SERVER", "10.19.99.10")  # 525-snow-db2
DATABASE = "Alliant"
TIMEOUT = 15
CACHE_SECONDS = 120
MAX_ROWS = 300  # a safety cap per query; answers show at most ~5 rows of detail
DATA = os.environ.get("SERVICE_DESK_DATA", os.path.join(os.path.dirname(__file__), "data"))
PRICE_WORDS = ("price", "amount", "rate", "charge", "balance", "cost", "replacement", "upcharge", "pr_", "_pr")
OLD = "From the hourly Alliant files: may be up to an hour old."

ACCT = "CAST(c.cu_account AS varchar(10)) + '-' + CAST(c.cu_branch AS varchar(3)) + '-' + RTRIM(c.cu_dept)"
ONE_ACCOUNT = "c.cu_account = %(acct)s AND c.cu_branch = %(branch)s AND RTRIM(c.cu_dept) = %(dept)s"

# ---- the fixed lookups --------------------------------------------------------------------------------------------

SQL = {
    # 1. Customer items: the account's own lines (employee 0), live or stopped in the last 60 days.
    "items": f"""
SELECT TOP 300 {ACCT} AS account, LTRIM(RTRIM(i.ei_skugrup)) AS sku, RTRIM(s.sg_desc) AS item,
       RTRIM(i.ei_size_a) AS size_a, RTRIM(i.ei_size_b) AS size_b, i.ei_ras_qty AS inventory,
       i.ei_auto_ct AS autocount, RTRIM(i.ei_freq) AS frequency, i.ei_mon AS d_mon, i.ei_tue AS d_tue,
       i.ei_wed AS d_wed, i.ei_thu AS d_thu, i.ei_fri AS d_fri, i.ei_sat AS d_sat, i.ei_sun AS d_sun,
       i.ei_hold AS on_hold, i.ei_stop_dt AS stop_date, i.ei_season AS seasonal, i.ei_sstopdt AS seasonal_stop,
       i.ei_restart_dt AS restart_date
FROM dbo.riempitm i WITH (NOLOCK)
JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = i.ei_CUID
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = i.ei_skugrup
WHERE {ONE_ACCOUNT} AND LTRIM(RTRIM(i.ei_empl)) = '0'
  AND (i.ei_stop_dt IS NULL OR i.ei_stop_dt < '1900-01-02' OR i.ei_stop_dt > DATEADD(day, -60, GETDATE()))
ORDER BY i.ei_skugrup""",
    # 2. Wearers and their garments (optionally one wearer).
    "wearers": f"""
SELECT TOP 300 {ACCT} AS account, LTRIM(RTRIM(e.em_empl)) AS wearer, RTRIM(e.em_firstn) AS first,
       RTRIM(e.em_lastn) AS last, e.em_locker AS locker, e.em_CostCenter AS department, e.em_startdt AS start_date,
       e.em_stop_dt AS stop_date, LTRIM(RTRIM(g.is_garment)) AS barcode, LTRIM(RTRIM(g.is_skugrup)) AS sku,
       RTRIM(s.sg_desc) AS item, RTRIM(g.is_size_a) AS size_a, RTRIM(g.is_size_b) AS size_b,
       RTRIM(g.is_proc_st) AS status, RTRIM(st.st_desc) AS status_desc
FROM dbo.riemploy e WITH (NOLOCK)
JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = e.em_cuid
LEFT JOIN dbo.ingarmis g WITH (NOLOCK) ON g.is_CUID = e.em_cuid AND g.is_empl = e.em_empl
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = g.is_skugrup
LEFT JOIN dbo.instatus st WITH (NOLOCK) ON RTRIM(st.st_code) = RTRIM(g.is_proc_st)
WHERE {ONE_ACCOUNT} AND LTRIM(RTRIM(e.em_empl)) <> '0'
  AND (%(wearer)s = '' OR LTRIM(RTRIM(e.em_empl)) = %(wearer)s)
ORDER BY e.em_empl, g.is_skugrup""",
    # 3. Garment by barcode: in service (ingarmis), else retired in the last 2 years (inghist).
    "garment": f"""
SELECT TOP 5 LTRIM(RTRIM(g.is_garment)) AS barcode, {ACCT} AS account, RTRIM(c.cu_name) AS customer,
       LTRIM(RTRIM(g.is_empl)) AS wearer, LTRIM(RTRIM(RTRIM(e.em_firstn) + ' ' + RTRIM(e.em_lastn))) AS wearer_name,
       LTRIM(RTRIM(g.is_skugrup)) AS sku, RTRIM(s.sg_desc) AS item, RTRIM(g.is_size_a) AS size_a,
       RTRIM(g.is_size_b) AS size_b, RTRIM(g.is_proc_st) AS status, RTRIM(st.st_desc) AS status_desc,
       g.is_stat_dt AS status_date, g.is_n_washs AS washes, NULLIF(s.sg_maxwash, 0) AS max_washes,
       g.is_delv_dt AS last_delivered, CASE WHEN g.is_proc_st = 'U' THEN LTRIM(RTRIM(g.is_cust_stat)) END AS scan_text,
       NULLIF(RTRIM(g.is_srv_cod), '') AS service_code, RTRIM(sv.se_descrip) AS service_desc, g.is_srv_dt AS service_date
FROM dbo.ingarmis g WITH (NOLOCK)
LEFT JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = g.is_CUID
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = g.is_skugrup
OUTER APPLY (SELECT TOP 1 x.em_firstn, x.em_lastn FROM dbo.riemploy x WITH (NOLOCK)
             WHERE x.em_cuid = g.is_CUID AND x.em_empl = g.is_empl ORDER BY x.em_startdt DESC) e
LEFT JOIN dbo.instatus st WITH (NOLOCK) ON RTRIM(st.st_code) = RTRIM(g.is_proc_st)
LEFT JOIN dbo.insrvcod sv WITH (NOLOCK) ON RTRIM(sv.se_code) = RTRIM(g.is_srv_cod)
WHERE LTRIM(RTRIM(g.is_garment)) = %(barcode)s""",
    "garment_retired": f"""
SELECT TOP 5 LTRIM(RTRIM(g.gh_garment)) AS barcode, {ACCT} AS account, RTRIM(c.cu_name) AS customer,
       LTRIM(RTRIM(g.gh_empl)) AS wearer, LTRIM(RTRIM(g.gh_skugrup)) AS sku, RTRIM(s.sg_desc) AS item,
       RTRIM(g.gh_size_a) AS size_a, RTRIM(g.gh_size_b) AS size_b, g.gh_stat_dt AS retired_date,
       CASE RTRIM(g.gh_proc_st) WHEN 'D' THEN 'Deassigned' WHEN 'R' THEN 'Ragged out' ELSE RTRIM(g.gh_proc_st) END AS retired_as,
       g.gh_n_washs AS washes
FROM dbo.inghist g WITH (NOLOCK)
LEFT JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = g.gh_CUID
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = g.gh_skugrup
WHERE g.gh_stat_dt >= DATEADD(year, -2, GETDATE()) AND LTRIM(RTRIM(g.gh_garment)) = %(barcode)s 1 = 1
ORDER BY g.gh_stat_dt DESC""",
    # 4. Holds / seasonal: the account's status and stop date, and every item line on hold.
    "holds": f"""
SELECT TOP 100 {ACCT} AS account, RTRIM(c.cu_name) AS name, c.cu_active AS active, c.cu_stopdt AS account_stop_date,
       RTRIM(r.oh_desc) AS account_hold_reason, c.cu_sstopdt AS seasonal_stop, c.cu_restart AS seasonal_restart,
       c.cu_restart_dt AS restart_date, c.cu_SeasonalRoll AS seasonal_roll,
       LTRIM(RTRIM(i.ei_empl)) AS wearer, LTRIM(RTRIM(i.ei_skugrup)) AS sku, RTRIM(s.sg_desc) AS item,
       i.ei_hold AS on_hold, i.ei_holdout AS hold_out, i.ei_season AS item_seasonal, i.ei_sstopdt AS item_seasonal_stop,
       i.ei_restart_dt AS item_restart, i.ei_stop_dt AS item_stop_date
FROM dbo.ricustmr c WITH (NOLOCK)
LEFT JOIN dbo.OnHoldReasons r WITH (NOLOCK) ON r.oh_ID = c.cu_OnHoldReason
LEFT JOIN dbo.riempitm i WITH (NOLOCK) ON i.ei_CUID = c.cu_ID
          AND (i.ei_hold = 1 OR i.ei_season = 1 OR i.ei_sstopdt > '1900-01-02' OR i.ei_holdout > '1900-01-02')
          AND (i.ei_stop_dt IS NULL OR i.ei_stop_dt < '1900-01-02' OR i.ei_stop_dt > GETDATE())
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = i.ei_skugrup
WHERE {ONE_ACCOUNT}""",
    # 5. Next delivery: route days, and any delivery ticket already created for today or later.
    "next_delivery": f"""
SELECT TOP 5 {ACCT} AS account, RTRIM(c.cu_name) AS name, c.cu_route AS route, c.cu_mon_route, c.cu_tue_route,
       c.cu_wed_route, c.cu_thu_route, c.cu_fri_route, c.cu_sat_route, c.cu_sun_route,
       RTRIM(h.hh_invoice) AS ticket, h.hh_date AS ticket_date
FROM dbo.ricustmr c WITH (NOLOCK)
OUTER APPLY (SELECT TOP 3 x.hh_invoice, x.hh_date FROM dbo.rainvhhd x WITH (NOLOCK)
             WHERE x.hh_cuid = c.cu_ID AND x.hh_date >= CAST(GETDATE() AS date) ORDER BY x.hh_date) h
WHERE {ONE_ACCOUNT}
ORDER BY h.hh_date""",
    # 6. Work orders: wearer garment orders (popending_wo) with what has been received.
    "work_orders": f"""
SELECT TOP 50 w.pp_wo AS work_order, w.pp_po AS po, w.pp_line AS line, LTRIM(RTRIM(w.pp_empl)) AS wearer,
       LTRIM(RTRIM(w.pp_skugrup)) AS sku, RTRIM(w.pp_desc) AS item, RTRIM(w.pp_size_a) AS size_a,
       RTRIM(w.pp_size_b) AS size_b, w.pp_ordered AS qty, d.pd_received AS qty_received, w.pp_released AS released,
       w.pp_dt_ordr AS ordered_date, w.pp_delv_dt AS delivery_date, d.pd_expected AS expected_date,
       d.pd_rcv_dt AS received_date, RTRIM(em.ei_emblem) AS emblem,
       CASE WHEN d.pd_received >= w.pp_ordered THEN 'received' WHEN d.pd_received > 0 THEN 'part received'
            WHEN w.pp_released = 1 THEN 'released' ELSE 'open' END AS status
FROM dbo.popending_wo w WITH (NOLOCK)
JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = w.pp_cuid
LEFT JOIN dbo.podetail d WITH (NOLOCK) ON d.pd_po = w.pp_po AND d.pd_line = w.pp_line
OUTER APPLY (SELECT TOP 1 x.ei_emblem FROM dbo.riempitm x WITH (NOLOCK)
             WHERE x.ei_CUID = w.pp_cuid AND x.ei_empl = w.pp_empl AND x.ei_skugrup = w.pp_skugrup) em
WHERE {ONE_ACCOUNT} AND (%(wearer)s = '' OR LTRIM(RTRIM(w.pp_empl)) = %(wearer)s)
  AND (w.pp_dt_ordr >= DATEADD(day, -180, GETDATE())
       OR (w.pp_dt_ordr >= DATEADD(year, -2, GETDATE()) AND ISNULL(d.pd_received, 0) < w.pp_ordered))
ORDER BY w.pp_dt_ordr DESC, w.pp_line""",
    # 8. Billing read-back for a new location (#new-accounts, the invoice check): each item's price, price type,
    #    frequency and days. Prices: Scott / the #new-accounts thread only (PRICED).
    "billing": f"""
SELECT TOP 300 {ACCT} AS account, LTRIM(RTRIM(i.ei_skugrup)) AS sku, RTRIM(s.sg_desc) AS item,
       i.ei_ras_qty AS inventory, i.ei_auto_ct AS autocount, RTRIM(i.ei_freq) AS frequency,
       i.ei_unit_pr AS unit_price, i.ei_pr_type AS price_type, i.ei_mon AS d_mon, i.ei_tue AS d_tue, i.ei_wed AS d_wed,
       i.ei_thu AS d_thu, i.ei_fri AS d_fri, i.ei_sat AS d_sat, i.ei_sun AS d_sun
FROM dbo.riempitm i WITH (NOLOCK)
JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = i.ei_CUID
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = i.ei_skugrup
WHERE {ONE_ACCOUNT} AND LTRIM(RTRIM(i.ei_empl)) = '0'
  AND (i.ei_stop_dt IS NULL OR i.ei_stop_dt < '1900-01-02' OR i.ei_stop_dt > GETDATE())
ORDER BY i.ei_skugrup""",
    # 9. Garment watch (#stockroom, new garment accounts): each wearer's garment line, its auto count (what's billed),
    #    its inventory (the contracted changes), the garments issued to that wearer (barcodes in service, not C) and
    #    what's still on order. Counts only, no prices.
    "garment_billing": f"""
SELECT TOP 300 {ACCT} AS account, LTRIM(RTRIM(i.ei_empl)) AS wearer,
       LTRIM(RTRIM(RTRIM(e.em_firstn) + ' ' + RTRIM(e.em_lastn))) AS name, LTRIM(RTRIM(i.ei_skugrup)) AS sku,
       RTRIM(s.sg_desc) AS item, RTRIM(i.ei_size_a) AS size_a, RTRIM(i.ei_size_b) AS size_b,
       i.ei_ras_qty AS inventory, i.ei_auto_ct AS autocount,
       (SELECT COUNT(*) FROM dbo.ingarmis g WITH (NOLOCK)
         WHERE g.is_CUID = i.ei_CUID AND LTRIM(RTRIM(g.is_empl)) = LTRIM(RTRIM(i.ei_empl))
           AND LTRIM(RTRIM(g.is_skugrup)) = LTRIM(RTRIM(i.ei_skugrup)) AND ISNULL(RTRIM(g.is_proc_st), '') <> 'C') AS issued,
       (SELECT ISNULL(SUM(w.pp_ordered - ISNULL(d.pd_received, 0)), 0) FROM dbo.popending_wo w WITH (NOLOCK)
         LEFT JOIN dbo.podetail d WITH (NOLOCK) ON d.pd_po = w.pp_po AND d.pd_line = w.pp_line
         WHERE w.pp_cuid = i.ei_CUID AND LTRIM(RTRIM(w.pp_empl)) = LTRIM(RTRIM(i.ei_empl))
           AND LTRIM(RTRIM(w.pp_skugrup)) = LTRIM(RTRIM(i.ei_skugrup)) AND ISNULL(d.pd_received, 0) < w.pp_ordered) AS on_order
FROM dbo.riempitm i WITH (NOLOCK)
JOIN dbo.ricustmr c WITH (NOLOCK) ON c.cu_ID = i.ei_CUID
OUTER APPLY (SELECT TOP 1 x.em_firstn, x.em_lastn FROM dbo.riemploy x WITH (NOLOCK)
             WHERE x.em_cuid = i.ei_CUID AND x.em_empl = i.ei_empl ORDER BY x.em_startdt DESC) e
LEFT JOIN dbo.riskugrp s WITH (NOLOCK) ON s.sg_skugrup = i.ei_skugrup
WHERE {ONE_ACCOUNT} AND LTRIM(RTRIM(i.ei_empl)) <> '0'
  AND (i.ei_stop_dt IS NULL OR i.ei_stop_dt < '1900-01-02' OR i.ei_stop_dt > GETDATE())
ORDER BY i.ei_empl, i.ei_skugrup""",
    # 7. SKU catalog read-back (the SKU update project): the SKU group's whole row and its sizes. Prices and costs:
    #    Scott / office only (summary() drops them for everyone else).
    "sku": """
SELECT TOP 1 * FROM dbo.riskugrp s WITH (NOLOCK) WHERE LTRIM(RTRIM(s.sg_skugrup)) = %(sku)s""",
}
# The sizes table's SKU column, found in INFORMATION_SCHEMA at run time (not guessed); its rows are read with it.
SQL_SIZE_COLUMN = """
SELECT TOP 1 COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME = 'inskusiz' AND COLUMN_NAME LIKE '%skugrup%' ORDER BY ORDINAL_POSITION"""
SQL_SIZES = "SELECT TOP 60 * FROM dbo.inskusiz z WITH (NOLOCK) WHERE LTRIM(RTRIM(z.{col})) = %(sku)s"
SQL_DESCRIBE = """
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME IN ('ricustmr', 'riempitm', 'riskugrp', 'inskusiz', 'popending_wo', 'OnHoldReasons')
  AND (TABLE_NAME IN ('inskusiz', 'popending_wo', 'OnHoldReasons')
       OR COLUMN_NAME LIKE '%seas%' OR COLUMN_NAME LIKE '%hold%' OR COLUMN_NAME LIKE '%restart%' OR COLUMN_NAME LIKE '%stop%'
       OR COLUMN_NAME LIKE '%emb%' OR COLUMN_NAME LIKE '%min%' OR COLUMN_NAME LIKE '%lrc%' OR COLUMN_NAME LIKE '%brc%'
       OR COLUMN_NAME LIKE '%lost%' OR COLUMN_NAME LIKE '%abuse%' OR COLUMN_NAME LIKE '%prep%' OR COLUMN_NAME LIKE '%upch%'
       OR COLUMN_NAME LIKE '%pr_type%' OR COLUMN_NAME LIKE '%ptype%' OR COLUMN_NAME LIKE '%price%' OR COLUMN_NAME LIKE '%exp%'
       OR COLUMN_NAME LIKE '%wash%' OR COLUMN_NAME LIKE '%merch%')
ORDER BY TABLE_NAME, ORDINAL_POSITION"""
LOOKUPS = ("items", "wearers", "garment", "holds", "next_delivery", "work_orders", "sku", "billing", "garment_billing")
PRICED = {"sku", "billing"}  # lookups whose rows carry price / cost fields
FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|exec|execute|drop|alter|create|truncate|grant|revoke|into|"
                       r"openrowset|opendatasource|xp_\w+|sp_\w+|waitfor|shutdown|backup|restore)\b|;|--", re.I)


def safe_sql(sql: str) -> bool:
    """SELECT only, nothing that writes or runs anything, and every table read WITH (NOLOCK)."""
    body = re.sub(r"'[^']*'", "''", sql)  # literals can't hide keywords
    if not re.match(r"\s*SELECT\b", body, re.I) or FORBIDDEN.search(body):
        return False
    reads = re.findall(r"\b(?:FROM|JOIN)\s+([\w.\[\]]+)((?:\s+(?!WITH\b|WHERE\b|ON\b|ORDER\b)\w+)?)\s*(WITH\s*\(NOLOCK\))?",
                       body, re.I)
    # Alliant's tables (dbo.) must be read WITH (NOLOCK); INFORMATION_SCHEMA is Alliant's own column list (metadata).
    return bool(reads) and all(t.lower().startswith("information_schema.") or (t.lower().startswith("dbo.") and nolock)
                               for t, _, nolock in reads)


for _name, _sql in list(SQL.items()) + [("size_column", SQL_SIZE_COLUMN), ("describe", SQL_DESCRIBE),
                                        ("sizes", SQL_SIZES.format(col="sz_skugrup"))]:
    assert safe_sql(_sql), f"alliant_live: {_name} is not a NOLOCK SELECT"


# ---- parameters --------------------------------------------------------------------------------------------------

def parse_account(account: str) -> dict:
    """1205-1-00004 -> account 1205, branch 1 (Snow White; 2 = The Laundry Group), dept 00004."""
    m = re.fullmatch(r"\s*(\d{1,6})-([12])-(\w{1,8})\s*", account or "")
    if not m:
        raise ValueError("an account looks like 1205-1-00004")
    return {"acct": int(m.group(1)), "branch": int(m.group(2)), "dept": m.group(3)}


def _params(lookup: str, **kw) -> dict:
    if lookup == "garment":
        bc = re.sub(r"\s", "", str(kw.get("barcode") or ""))
        if not re.fullmatch(r"[\w-]{3,30}", bc):
            raise ValueError("a garment barcode is 3 to 30 letters or digits")
        return {"barcode": bc}
    if lookup == "sku":
        sku = str(kw.get("sku") or "").strip()
        if not re.fullmatch(r"[\w-]{1,20}", sku):
            raise ValueError("a SKU looks like 2-0413WH")
        return {"sku": sku}
    p = parse_account(kw.get("account", ""))
    if lookup in ("wearers", "work_orders"):
        p["wearer"] = re.sub(r"\D", "", str(kw.get("wearer") or ""))[:8]
    return p


# ---- running a lookup: lock, limit, cache, timeout, log ----------------------------------------------------------

def configured() -> bool:
    return bool(os.environ.get("ALLIANT_SQL_USER") and os.environ.get("ALLIANT_SQL_KEY"))


def _scrub(text: str) -> str:
    for v in (os.environ.get("ALLIANT_SQL_KEY"), os.environ.get("ALLIANT_SQL_USER")):
        if v:
            text = text.replace(v, "***")
    return text[:300]


def _log(data_dir: str, line: str) -> None:
    try:
        with open(os.path.join(data_dir, "alliant_live.log"), "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")
    except OSError:
        pass


class _State:
    """Cache and hourly count, shared by every Donna process through one file under an exclusive lock (which also
    keeps it to one query at a time)."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "alliant_live_state.json")
        self.lock = open(os.path.join(data_dir, "alliant_live.lock"), "a+")

    def __enter__(self):
        deadline = time.time() + TIMEOUT + 5
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TimeoutError("another live lookup is still running")
                time.sleep(0.2)
        try:
            self.data = json.load(open(self.path))
        except (OSError, ValueError):
            self.data = {"times": [], "cache": {}}
        now = time.time()
        self.data["times"] = [t for t in self.data["times"] if now - t < 3600]
        self.data["cache"] = {k: v for k, v in self.data["cache"].items() if now - v["at"] < CACHE_SECONDS}
        return self

    def __exit__(self, *exc):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, default=str)
        os.replace(tmp, self.path)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()


def _connect():
    import pymssql
    return pymssql.connect(server=SERVER, user=os.environ["ALLIANT_SQL_USER"], password=os.environ["ALLIANT_SQL_KEY"],
                           database=DATABASE, login_timeout=TIMEOUT, timeout=TIMEOUT, as_dict=True, autocommit=False)


def _clean(v):
    if isinstance(v, (dt.datetime, dt.date)):
        if v.year <= 1900:
            return ""
        return f"{v:%Y-%m-%d}" if not isinstance(v, dt.datetime) or v.time() == dt.time() else f"{v:%Y-%m-%d %H:%M}"
    if isinstance(v, str):
        return " ".join(v.split())
    if isinstance(v, (bytes, bytearray)):
        return ""
    if hasattr(v, "is_integer") or type(v).__name__ == "Decimal":
        f = float(v)
        return int(f) if f.is_integer() else round(f, 4)
    return v


def _run(conn, lookup: str, params: dict) -> list[dict]:
    """The lookup's statement(s), read only. Never commits."""
    cur = conn.cursor()
    try:
        cur.execute(SQL[lookup], params)
        rows = cur.fetchmany(MAX_ROWS)
        if lookup == "garment" and not rows:
            cur.execute(SQL["garment_retired"], params)
            rows = cur.fetchmany(MAX_ROWS)
        if lookup == "sku" and rows:
            cur.execute(SQL_SIZE_COLUMN)
            col = (cur.fetchone() or {}).get("COLUMN_NAME", "")
            if re.fullmatch(r"\w{1,40}", col or ""):  # a name from Alliant's own schema
                cur.execute(SQL_SIZES.format(col=col), params)
                rows[0]["sizes"] = [{k: _clean(v) for k, v in r.items()} for r in cur.fetchmany(60)]
    finally:
        conn.rollback()
    return [{k: (_clean(v) if k != "sizes" else v) for k, v in r.items()} for r in rows]


def lookup(name: str, data_dir: str = DATA, fresh: bool = False, **kw) -> dict:
    """One fixed lookup, live when it can be, else from the hourly files. fresh=True skips the 2-minute cache (a
    read-back right after Donna saves must see the new value, not one from before the save); it still counts
    toward the hourly limit. -> {"lookup", "params", "rows", "source": "live" | "files", "at", "note"}"""
    if name not in LOOKUPS:
        raise ValueError(f"unknown lookup {name}")
    params = _params(name, **kw)
    key = hashlib.sha1(json.dumps([name, params], sort_keys=True).encode()).hexdigest()
    shown = {k: v for k, v in params.items() if v != ""}
    why = ""
    if configured() and os.environ.get("ALLIANT_LIVE_OFF", "") != "1":
        try:
            with _State(data_dir) as st:
                hit = None if fresh else st.data["cache"].get(key)
                if hit:
                    _log(data_dir, f"{name} {shown} cached rows={len(hit['rows'])}")
                    return {"lookup": name, "params": shown, "rows": hit["rows"], "source": "live", "at": hit["when"],
                            "note": f"Live from Alliant at {hit['when']} (cached)."}
                if len(st.data["times"]) >= int(os.environ.get("ALLIANT_LIVE_PER_HOUR", "30")):
                    why = "the hourly limit of live lookups is reached"
                else:
                    st.data["times"].append(time.time())
                    t0 = time.time()
                    conn = _connect()
                    try:
                        rows = _run(conn, name, params)
                    finally:
                        conn.close()
                    when = dt.datetime.now().strftime("%-I:%M %p")
                    st.data["cache"][key] = {"at": time.time(), "when": when, "rows": rows}
                    _log(data_dir, f"{name} {shown} live rows={len(rows)} secs={time.time() - t0:.1f}")
                    return {"lookup": name, "params": shown, "rows": rows, "source": "live", "at": when,
                            "note": f"Live from Alliant at {when}."}
        except Exception as e:  # VPN down, SQL timeout, login: never the credentials
            why = f"Alliant didn't answer ({type(e).__name__}: {_scrub(str(e))})"
    else:
        why = "live lookups aren't set up" if not configured() else "live lookups are off"
    rows = from_files(name, data_dir, **params)
    _log(data_dir, f"{name} {shown} files rows={len(rows)} ({why})")
    return {"lookup": name, "params": shown, "rows": rows, "source": "files", "at": "", "note": f"{OLD} ({why})"}


# ---- fallback: the hourly Service Desk files ---------------------------------------------------------------------

def _csv(data_dir: str, name: str) -> list[dict]:
    try:
        with open(os.path.join(data_dir, name), newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def from_files(name: str, data_dir: str = DATA, **p) -> list[dict]:
    if name == "garment":
        bc = p["barcode"].upper()
        return ([r for r in _csv(data_dir, "garments_in_service.csv") if r.get("barcode", "").upper() == bc] or
                [r for r in _csv(data_dir, "garments_retired.csv") if r.get("barcode", "").upper() == bc])[:5]
    if name == "sku":  # skus.csv has no price or cost columns
        return [r for r in _csv(data_dir, "skus.csv") if r.get("sku", "").upper() == p["sku"].upper()][:1]
    acct = f"{p['acct']}-{p['branch']}-{p['dept']}"
    mine = lambda rows, col="account": [r for r in rows if r.get(col) == acct]
    wearer = p.get("wearer") or ""
    if name == "items":
        return mine(_csv(data_dir, "current_items.csv"))
    if name == "wearers":
        return [r for r in mine(_csv(data_dir, "garments.csv")) if not wearer or r.get("employee") == wearer]
    if name == "holds":
        return mine(_csv(data_dir, "holds.csv"))
    if name == "next_delivery":
        return mine(_csv(data_dir, "customers.csv"))
    if name == "work_orders":
        return [r for r in mine(_csv(data_dir, "purchase_orders.csv"), "cust_code") if not wearer or r.get("wearer") == wearer]
    return []


# ---- answers ------------------------------------------------------------------------------------------------------

def is_priced(field: str) -> bool:
    f = field.lower()
    return any(w in f for w in PRICE_WORDS) or f in ("sg_mspr", "sg_sspr", "sg_pvprice", "sg_svprice")


# The SKU lookup reads whole rows, and Alliant's column names don't always say "price" (lrc, brc...): anyone but Scott
# or the office sees only these fields of it (an allow-list, so a new or cryptic column is never shown by mistake).
SKU_SAFE = {"sg_skugrup", "sg_desc", "sg_supergr", "sg_type", "sg_garment", "sg_discontinued", "sg_phaseout",
            "sg_maxwash", "sg_explife", "sg_exp_wks", "sg_maxrewashes", "sg_weight", "sg_pvendor", "sg_pvstyle", "sg_color", "sg_style", "sg_matsize"}
SIZE_SAFE = re.compile(r"(?:^|_)(?:size|siz|size_a|size_b|sizea|sizeb|desc|skugrup)$", re.I)


def without_prices(rows: list[dict], lookup: str = "") -> list[dict]:
    out = []
    for r in rows:
        if lookup in PRICED:
            sizes = r.get("sizes")
            r = {k: v for k, v in r.items() if k.lower() in SKU_SAFE}
            if isinstance(sizes, list):
                r["sizes"] = [{k: v for k, v in z.items() if SIZE_SAFE.search(k) and not is_priced(k)} for z in sizes]
        else:
            r = {k: v for k, v in r.items() if not is_priced(k)}
        out.append(r)
    return out


def rows_for(result: dict, audience: str) -> list[dict]:
    """The rows this person may see: prices and costs only for Scott or the office ("owner" / "office")."""
    return result["rows"] if audience in ("owner", "office") else without_prices(result["rows"], result["lookup"])


COUNTS = {"issued", "on_order", "inventory", "autocount", "quantity", "qty", "qty_received", "washes", "max_washes", "sg_maxwash",
          "sg_exp_wks", "sg_explife"}


def summary(result: dict, audience: str = "staff", limit: int = 5) -> str:
    rows = rows_for(result, audience)
    head = f"{result['lookup'].replace('_', ' ')} {' '.join(str(v) for v in result['params'].values())}: {len(rows)} row(s)"
    lines = [head, result["note"]]
    for r in rows[:limit]:
        lines.append("  " + "; ".join(f"{k}={v}" for k, v in r.items() if k != "sizes" and v not in ("", None)
                                      and not (v in (0, False) and (k.startswith("d_") or k not in COUNTS))))
        for z in (r.get("sizes") or [])[:12]:
            lines.append("    size: " + "; ".join(f"{k}={v}" for k, v in z.items() if v not in ("", None)))
    if len(rows) > limit:
        lines.append(f"  … and {len(rows) - limit} more")
    return "\n".join(lines)


# ---- the command line: sample output and the schema check ---------------------------------------------------------

def describe() -> str:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(SQL_DESCRIBE)
        rows = cur.fetchall()
    finally:
        conn.rollback()
        conn.close()
    out, table = [], ""
    for r in rows:
        if r["TABLE_NAME"] != table:
            table = r["TABLE_NAME"]
            out.append(f"\n{table}:")
        out.append(f"  {r['COLUMN_NAME']} ({r['DATA_TYPE']})")
    return "\n".join(out)


def main(argv: list[str]) -> None:
    data_dir = os.environ.get("SERVICE_DESK_DATA", DATA)
    if argv[:1] == ["describe"]:
        print(describe())
        return
    if argv[:1] == ["sample"] and len(argv) >= 2:
        for a in argv[1:]:
            if re.fullmatch(r"\d{1,6}-[12]-\w{1,8}", a):
                for name in ("items", "wearers", "holds", "next_delivery", "work_orders"):
                    print(summary(lookup(name, data_dir, account=a), "staff"), "\n")
            else:
                print(summary(lookup("garment", data_dir, barcode=a), "staff"), "\n")
        return
    if argv[:1] in (["sku"],) and len(argv) == 2:  # Scott's own check of the SKU project read-back (prices shown)
        print(summary(lookup("sku", data_dir, sku=argv[1]), "owner", limit=1))
        return
    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
