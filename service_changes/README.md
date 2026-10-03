# Service change parser (prototype)

Reads a driver's message from a `#route-N` Slack channel and works out:

- **what kind of message it is**: item change, wearer change, hold or closure, special order, route or schedule, operational issue, or not a request
- **which customer it's for**: the route comes from the channel and the service day from the post date, which narrows the customer list
- **each change**: action, item, quantity, the driver's stated total, wearer, size, frequency, and start date
- **whether the numbers agree with Alliant**: for example, current 16 + 28 = 44 when the driver said 28 total
- **what to ask the driver**, when something is missing or doesn't add up
- **the readback** that staff post once the change is entered

Claude only reads and classifies the message (`parser.py`). The arithmetic and the readback text are plain code (`checks.py`), so the totals are always computed the same way.

## Files

| File | What it does |
|---|---|
| `schema.py` | The structured result for one message |
| `parser.py` | The prompt and the Claude call |
| `context.py` | Route and service day, plus loading the Alliant CSV exports |
| `checks.py` | Quantity checks against Alliant, driver questions, readback text |
| `run.py` | Command line: runs a batch of messages and prints the report |
| `alliant_report.py` | Converts Alliant's Item Usage report (Excel) into the CSVs below |
| `slack_manifest.yaml` | Slack app definition for the Service Desk bot |
| `examples/messages.json` | 27 real messages from the route channels (Jul–Oct 2026) |
| `examples/labels.json` | Hand-checked correct reading of each message, used to score the parser |
| `examples/demo_*.csv` | **Demo data only.** Placeholder accounts. Quantities were inferred from the Slack threads, not taken from Alliant |

## Running it

```bash
pip install anthropic pydantic pytest

# Unit tests (no API key needed)
python -m pytest service_changes

# Run the hand labels through the checks and readbacks (no API key needed)
python -m service_changes.run service_changes/examples/messages.json \
  --customers service_changes/examples/demo_customers.csv \
  --items service_changes/examples/demo_current_items.csv \
  --replay service_changes/examples/labels.json

# Let Claude read the messages and score it against the labels (needs ANTHROPIC_API_KEY)
python -m service_changes.run service_changes/examples/messages.json \
  --customers service_changes/examples/demo_customers.csv \
  --items service_changes/examples/demo_current_items.csv \
  --eval service_changes/examples/labels.json
```

## Alliant data

Export Alliant's **Item Usage** report (All SKU Groups, customer/employee order) to Excel, then:

```bash
python -m service_changes.alliant_report Alliant_Item_Usage_Report.xlsx --out service_changes/data
```

This writes `customers.csv` (account, name, route, service_days, frequency), `current_items.csv`
(account, item, quantity, sku, days, frequency, unit_price, delivery_unit) and `garments.csv`
(account, employee number, sku, size, item, quantity). `service_changes/data/` is kept out of git
because it holds customer pricing.

Run the parser against it with `--customers service_changes/data/customers.csv --items service_changes/data/current_items.csv`.

When Alliant already shows the total the driver asked for, the change is reported as "already entered"
rather than raised as a question.

## Not built yet

- Employee names for garment checks (the Item Usage report has employee numbers only)
- Watching Slack live and replying in threads
- Writing changes into Alliant
