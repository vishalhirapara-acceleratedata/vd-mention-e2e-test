"""Thin shell: post ci/design-drift failure when the job dies before its runner.

Relays the dbt parse rejection from gate-parse.log when the parse step itself
failed; otherwise posts a generic "failed before runner started" description.

Usage:
    design_drift_fallback_status.py --parse-outcome "$PARSE_OUTCOME" \
        --sha "$CI_HEAD_SHA" --target-url URL
"""

import argparse
import os

from vibedata_domain_ci import emit_status
from dbt_error_report import summarize_parse_failure

CONTEXT = "ci/design-drift"
DEFAULT_DESCRIPTION = "Gate failed before runner started: see job logs"
PARSE_LOG = "gate-parse.log"


def fallback_description(parse_outcome: str, parse_log: str | None) -> str:
    # Only the parse step's own failure puts a rejection in that log. This
    # emitter fires on any pre-runner failure, and a log from a parse that
    # succeeded would otherwise be relayed as the cause of something else.
    cause = ""
    if parse_outcome == "failure" and parse_log is not None:
        cause = summarize_parse_failure(parse_log)
    return cause or DEFAULT_DESCRIPTION


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parse-outcome", default="")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--target-url", default="")
    args = parser.parse_args()

    parse_log = None
    if args.parse_outcome == "failure":
        try:
            with open(PARSE_LOG) as f:
                parse_log = f.read()
        except OSError:
            parse_log = None
    description = fallback_description(args.parse_outcome, parse_log)
    emit_status.emit_status(os.environ["GITHUB_REPOSITORY"], args.sha, CONTEXT, "failure", description, args.target_url)


if __name__ == "__main__":
    main()
