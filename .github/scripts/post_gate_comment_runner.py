"""Thin shell: post a gate's PR comment from its report JSON.

Maps --gate to its notify_render renderer and marker, then upserts through
pr_comment.post_gate_comment, which owns the missing-report handling.

Usage:
    post_gate_comment_runner.py --gate gate-3 --report reports/gate-3.json \
        --pr "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" \
        [--real-work-result failure] [--run-url URL]
"""

import argparse
import functools

from vibedata_domain_ci import notify_render
from vibedata_domain_ci import pr_comment
GATES = {
    "preflight": (notify_render.render_preflight_comment, notify_render.PREFLIGHT_MARKER),
    "gate-2": (notify_render.render_gate_2_comment, notify_render.GATE_2_MARKER),
    "gate-3": (notify_render.render_gate_3_comment, notify_render.GATE_3_MARKER),
    "gate-4": (notify_render.render_gate_4_comment, notify_render.GATE_4_MARKER),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", required=True, choices=sorted(GATES))
    parser.add_argument("--report", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--real-work-result")
    parser.add_argument("--run-url", default="")
    args = parser.parse_args()

    render, marker = GATES[args.gate]
    if args.gate == "gate-2":
        render = functools.partial(render, run_url=args.run_url)
    pr_comment.post_gate_comment(
        args.report, render, marker, args.pr, args.repo,
        real_work_result=args.real_work_result,
    )


if __name__ == "__main__":
    main()
