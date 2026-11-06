"""Thin shell: post the ci/unit-tests final status.

The job's own conclusion decides pass/fail; on success, reports/gate-3.json
distinguishes "no unit tests found" from "all passed".

Usage:
    gate_3_status_runner.py --job-status "$JOB_STATUS" --report reports/gate-3.json \
        --sha "$CI_HEAD_SHA" --target-url URL
"""

import argparse
import json
import os

from vibedata_domain_ci import emit_status
CONTEXT = "ci/unit-tests"


def decide_gate_3_status(job_status: str, report: dict | None) -> tuple[str, str]:
    if job_status != "success":
        return "failure", "Gate 3: unit tests failed — see PR comment"
    if isinstance(report, dict) and report.get("overall_status") == "skipped":
        return "success", "Gate 3: skipped — no unit tests found"
    return "success", "Gate 3: all unit tests passed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-status", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--target-url", default="")
    args = parser.parse_args()

    try:
        with open(args.report) as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        report = None
    state, description = decide_gate_3_status(args.job_status, report)
    emit_status.emit_status(os.environ["GITHUB_REPOSITORY"], args.sha, CONTEXT, state, description, args.target_url)


if __name__ == "__main__":
    main()
