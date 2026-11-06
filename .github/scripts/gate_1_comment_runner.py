"""Thin shell: post the Gate 1 (ci/state-modified+) PR comment.

Renders the closure from reports/deployment-manifest-<head-sha>.json (empty
when absent) with the Gate 1 outcome, and upserts under GATE_1_MARKER.

Usage:
    gate_1_comment_runner.py --job-status "$JOB_STATUS" --greenfield "$GREENFIELD_FALLBACK" \
        --error-mode "$PLATFORM_ERROR_MODE" --error-category "$PLATFORM_ERROR_CATEGORY" \
        --error-reason "$PLATFORM_ERROR_REASON" --head-sha "$HEAD_SHA" \
        --pr "$PR_NUMBER" --repo "$REPO"
"""

import argparse
import json

from vibedata_domain_ci import notify_render
from vibedata_domain_ci import pr_comment
def closure_from_deployment_manifest(dm: dict) -> list[dict]:
    return [
        {
            "name": a.get("name", "").split(".")[-1],
            "materialization": a.get("materialized", "unknown"),
            "closure_source": a.get("closure_source", "descendant"),
        }
        for a in dm.get("artifacts", [])
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-status", default="failure")
    parser.add_argument("--greenfield", default="false")
    parser.add_argument("--error-mode", default="artifact")
    parser.add_argument("--error-category", default="")
    parser.add_argument("--error-reason", default="")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--pr", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    platform_error = None
    if args.error_category:
        platform_error = {"mode": args.error_mode, "category": args.error_category, "reason": args.error_reason}
    try:
        with open(f"reports/deployment-manifest-{args.head_sha}.json") as f:
            closure = closure_from_deployment_manifest(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        closure = []
    body = notify_render.render_gate_1_comment(
        closure,
        greenfield=args.greenfield.lower() == "true",
        passed=args.job_status.lower() == "success",
        platform_error=platform_error,
    )
    pr_comment.upsert(notify_render.GATE_1_MARKER, body, args.pr, args.repo)


if __name__ == "__main__":
    main()
