"""Thin shell: post the Gate 0 (ci/static-check) PR comment.

Loads every Gate 0 report present under reports/ (a missing or unreadable one
is None), renders with notify_render.render_gate_0_comment, and upserts under
GATE_0_MARKER.

Usage:
    gate_0_comment_runner.py --pr "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --run-url URL
"""

import argparse
import json

from vibedata_domain_ci import notify_render
from vibedata_domain_ci import pr_comment
def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-url", default="")
    args = parser.parse_args()

    body = notify_render.render_gate_0_comment(
        _load("reports/dbt_compile.json"),
        _load("reports/schema_gate.json"),
        ruff=_load("reports/ruff.json"),
        sqlfluff=_load("reports/sqlfluff.json"),
        gitleaks=_load("reports/gitleaks.json"),
        scorecard=_load("reports/scorecard.json"),
        # Written by Fabric's shortcut seeding only; absent elsewhere.
        shortcut_seeding=_load("reports/shortcut_seeding.json"),
        sqlfluff_stderr=_read_text("reports/sqlfluff.stderr"),
        run_url=args.run_url,
    )
    pr_comment.upsert(notify_render.GATE_0_MARKER, body, args.pr, args.repo)


if __name__ == "__main__":
    main()
