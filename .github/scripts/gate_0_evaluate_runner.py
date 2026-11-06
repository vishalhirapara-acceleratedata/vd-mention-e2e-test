"""Thin shell: fail Gate 0 when any blocking static check failed.

Reads each check's report under reports/ and exits 1 with an ::error:: naming
every failed check. A missing or unreadable report counts as a failure, and so
does a report whose shape can't be understood — it never counts as clean.

Usage:
    gate_0_evaluate_runner.py
"""

import json
import os
import sys

from vibedata_domain_ci import runner_io
from vibedata_domain_ci.static_reports import gitleaks_ok, ruff_ok, sqlfluff_ok


def _passed_field_ok(r):
    return isinstance(r, dict) and bool(r.get("passed"))


# (check name, report path); order is the order failures are reported in.
CHECKS = [
    ("ruff", "reports/ruff.json"),
    ("sqlfluff", "reports/sqlfluff.json"),
    ("gitleaks", "reports/gitleaks.json"),
    ("dbt compile", "reports/dbt_compile.json"),
    ("schema gate", "reports/schema_gate.json"),
    ("dbt scorecard", "reports/scorecard.json"),
]
_RULES = {
    "ruff": ruff_ok,
    "sqlfluff": sqlfluff_ok,
    "gitleaks": gitleaks_ok,
    "dbt compile": _passed_field_ok,
    "schema gate": _passed_field_ok,
    "dbt scorecard": _passed_field_ok,
}


def evaluate_gate_0(reports: dict) -> list[str]:
    """Return the failed checks, in CHECKS order. A None report is missing."""
    failures = []
    for name, _ in CHECKS:
        report = reports.get(name)
        if report is None:
            failures.append(f"{name} (report missing)")
        elif not _RULES[name](report):
            failures.append(name)
    return failures


def _load(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def main() -> None:
    failures = evaluate_gate_0({name: _load(path) for name, path in CHECKS})
    if failures:
        runner_io.error(f"Gate 0 failed: {', '.join(failures)}")
        sys.exit(1)
    print("Gate 0: all blocking checks passed.")


if __name__ == "__main__":
    main()
