"""Strict roll-number input audit; original files are never modified."""

import csv
import hashlib
from pathlib import Path
import re


def audit(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or rows[0] not in (["Roll Numbers"], ["Roll Number"]):
        raise ValueError('CSV header must be one column: "Roll Numbers" or "Roll Number"')
    valid = []
    invalid_rows = []
    blank_rows = 0
    for line, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            blank_rows += 1
        elif len(row) != 1 or not re.fullmatch(r"[0-9]{6}", row[0].strip()):
            invalid_rows.append(line)
        else:
            valid.append(row[0].strip())
    return {
        "source": str(path),
        "roll_column": rows[0][0],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "records": len(rows) - 1,
        "valid_records": len(valid),
        "unique_rolls": len(set(valid)),
        "duplicate_records": len(valid) - len(set(valid)),
        "blank_rows": blank_rows,
        "invalid_rows": invalid_rows,
    }
