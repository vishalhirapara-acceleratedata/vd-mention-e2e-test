"""Write reports/modified.json — the bare names of the models a PR deploys.

Reads reports/deployment-manifest-<head-sha>.json; a missing manifest fails
the step.

Usage:
    write_modified_json.py --head-sha "$HEAD_SHA"
"""

import argparse
import json


def modified_names(dm: dict) -> list[str]:
    return [a["name"].split(".")[-1] for a in dm.get("artifacts", [])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args()

    with open(f"reports/deployment-manifest-{args.head_sha}.json") as f:
        dm = json.load(f)
    with open("reports/modified.json", "w") as f:
        json.dump(modified_names(dm), f)


if __name__ == "__main__":
    main()
