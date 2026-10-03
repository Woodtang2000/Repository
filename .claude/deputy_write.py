#!/usr/bin/env python3
"""Narrow Deputy write helper for the pre-payroll check.

Only two changes are possible:
  area      <timesheet_id> <operational_unit_id>   set a leave timesheet's area
  leavecode <timesheet_id> <leave_rule_id>         change a leave timesheet's leave code

Each change refuses discarded or already-exported timesheets, sends exactly one
field, re-reads the timesheet and fails if anything else changed.
"""
import os
import sys

import requests

BASE = "https://86989323045744.na.deputy.com/api/v1"
CA = "/root/.ccr/ca-bundle.crt"
WATCH = ["Employee", "Date", "StartTime", "EndTime", "TotalTime", "OperationalUnit",
         "LeaveRule", "IsLeave", "Discarded", "TimeApproved", "Exported"]
ACTIONS = {"area": "OperationalUnit", "leavecode": "LeaveRule"}

session = requests.Session()
session.verify = CA
session.headers["Content-Type"] = "application/json"
if os.environ.get("DEPUTY_TOKEN"):
    session.headers["Authorization"] = "Bearer " + os.environ["DEPUTY_TOKEN"]


def get(path):
    r = session.get(BASE + path, timeout=60)
    r.raise_for_status()
    return r.json()


def main(argv):
    if len(argv) != 4 or argv[1] not in ACTIONS or not argv[2].isdigit() or not argv[3].isdigit():
        sys.exit("usage: deputy_write.py area|leavecode <timesheet_id> <target_id>")
    field, ts_id, target = ACTIONS[argv[1]], int(argv[2]), int(argv[3])

    before = get(f"/resource/Timesheet/{ts_id}")
    if not before.get("IsLeave"):
        sys.exit(f"REFUSED {ts_id}: not a leave timesheet")
    if before.get("Discarded"):
        sys.exit(f"REFUSED {ts_id}: discarded")
    if before.get("Exported"):
        sys.exit(f"REFUSED {ts_id}: already exported/imported")
    if field == "OperationalUnit":
        ou = get(f"/resource/OperationalUnit/{target}")
        emp = get(f"/resource/Employee/{before['Employee']}")
        if not ou.get("Active") or ou.get("Company") != emp.get("Company"):
            sys.exit(f"REFUSED {ts_id}: area {target} is inactive or not in the employee's location")
    else:
        get(f"/resource/LeaveRules/{target}")  # must exist
    if before.get(field) == target:
        print(f"NOCHANGE {ts_id}: {field} already {target}")
        return

    r = session.post(f"{BASE}/resource/Timesheet/{ts_id}", json={field: target}, timeout=60)
    if not r.ok:
        sys.exit(f"FAILED {ts_id}: HTTP {r.status_code}")

    after = get(f"/resource/Timesheet/{ts_id}")
    changed = {k: (before.get(k), after.get(k)) for k in WATCH if before.get(k) != after.get(k)}
    if changed != {field: (before.get(field), target)}:
        sys.exit(f"MISMATCH {ts_id}: unexpected changes {changed}")
    print(f"OK {ts_id}: {field} {before.get(field)} -> {target}")


if __name__ == "__main__":
    main(sys.argv)
