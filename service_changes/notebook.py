"""Donna's Alliant notebook: what she figured out on Alliant's screens, one line each, kept on the server
(<data>/donna_notebook.md) so every day's session starts knowing what the last one learned. She reads it at the start
of each day and adds a line after a job taught her something (workstation, /api/donna/jobs/notebook). Scott reads it
with "Donna notebook" in his private messages. Never customer data, prices or anything from A/R: screen know-how only.
"""
import os
import re
import time

FILE = "donna_notebook.md"
MAX_LINES = 150
MONEY = re.compile(r"\$\s?\d|\bbalance\b|\bowes?\b|\bA/R\b|\bpassword\b", re.I)


def path(data_dir: str) -> str:
    return os.path.join(data_dir, FILE)


def read(data_dir: str) -> str:
    try:
        return open(path(data_dir), encoding="utf-8").read()
    except OSError:
        return ""


def add(data_dir: str, note: str) -> str:
    """One new line (dated), unless it's empty, already there, or about money or passwords."""
    note = re.sub(r"\s+", " ", note or "").strip().lstrip("-• ").strip()[:400]
    if len(note) < 10:
        return "too short"
    if MONEY.search(note):
        return "not saved: no prices, balances or passwords in the notebook"
    lines = [ln for ln in read(data_dir).splitlines() if ln.strip()]
    key = lambda s: re.sub(r"[^a-z0-9]", "", re.sub(r"^- \d{4}-\d\d-\d\d:\s*", "", s.lower()))
    if any(key(ln) == key(note) for ln in lines):
        return "already in the notebook"
    lines.append(f"- {time.strftime('%Y-%m-%d')}: {note}")
    lines = lines[-MAX_LINES:]
    with open(path(data_dir) + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(path(data_dir) + ".tmp", path(data_dir))
    return "saved"
