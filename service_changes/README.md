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

## Alliant exports it expects

Extra columns are ignored.

- `customers.csv`: `account, name, route, service_days`, where service_days looks like `Mon;Thu`
- `current_items.csv`: `account, item, quantity`

## Not built yet

- Wearer lists (garments and sizes per employee), for checking garment changes
- Watching Slack live and replying in threads
- Writing changes into Alliant
