# House style for generated documents

Every PDF the agents produce (weekly routes report and any new report) uses `brand.py`, which matches the HR
onboarding forms: company logo top left (both when a document covers both companies), navy title, small uppercase
subtitle, 3 pt gold rule, navy uppercase section headings, navy table headers, footer with company names and page number.

- `logo_sw.png` – Snow White Linen Supply (navy #003746, gold #f9af10)
- `logo_tlg.png` – The Laundry Group (navy #003f50, gold #ffb807)
- `logo_hgc.png` – Homer Golf Course (navy #061533, coral #ef4136), cut from Scott's Illustrator file 10/25/2026.
  Homer Golf Course is Scott's alone, not part of Snow White or The Laundry Group: its documents use only this logo and
  go only to Scott. Payroll moves to NetSuite + Deputy for the 2027 season (plan in Dropbox HR/).

Scheduled tasks get the scripts from Dropbox (/Scott Woodland/HR/Payroll Agent/agent-files/) but the logos from here,
by `git clone`, because text tools can't carry image files exactly. `brand.py` checks each logo's SHA-256 and prints the
company name instead if a logo is missing or damaged.
