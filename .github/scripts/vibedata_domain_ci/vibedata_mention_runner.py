"""Thin shell entry point for the GitHub App mention workflow (vibedata-mention.yml).

Reads the comment from the event file (never from a shell-interpolated expression), masks the
Vibedata token before any other output, and hands the core real adapters: urllib for Vibedata
and the `gh` CLI (authenticated by the job's GITHUB_TOKEN) for GitHub.

Env: GITHUB_EVENT_PATH, GITHUB_TOKEN, APP_SLUG, STUDIO, DOMAIN_ID, PAT.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from vibedata_domain_ci.vibedata_mention import (
    ENVIRONMENT,
    MentionEvent,
    StudioError,
    StudioResponse,
    run_mention,
)


def event_from_payload(payload: dict) -> MentionEvent:
    comment = payload["comment"]
    created_at = datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return MentionEvent(
        repository=payload["repository"]["full_name"],
        pull_request=int(payload["issue"]["number"]),
        comment_id=int(comment["id"]),
        comment_body=comment["body"] or "",
        comment_created_at=created_at,
        commenter_login=comment["user"]["login"],
    )


def _json_object(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class UrllibStudio:
    def __init__(self, base_url: str, pat: str):
        self._base_url = base_url.rstrip("/")
        self._pat = pat

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> StudioResponse:
        all_headers = {"Accept": "application/json", "Authorization": f"Bearer {self._pat}", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base_url + path, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return StudioResponse(response.status, _json_object(response.read()), response.headers.get("ETag"))
        except urllib.error.HTTPError as error:
            problem = _json_object(error.read())
            raise StudioError(error.code, problem.get("code"), problem.get("detail")) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Unreachable, timed out, or an unusable Vibedata URL: nothing from Vibedata to relay.
            raise StudioError(None, None, None) from None


class GhCliGitHub:
    @staticmethod
    def _gh(*args: str, stdin: str | None = None) -> str:
        return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=True).stdout

    def permission(self, repository: str, login: str) -> str:
        return self._gh("api", f"repos/{repository}/collaborators/{login}/permission", "--jq", ".permission").strip()

    def environment_branch_policy(self, repository: str, name: str = ENVIRONMENT) -> dict | None:
        try:
            raw = self._gh("api", f"repos/{repository}/environments/{name}")
        except subprocess.CalledProcessError as error:
            if "HTTP 404" in (error.stderr or ""):
                return None
            raise
        return _json_object(raw.encode("utf-8")).get("deployment_branch_policy")

    def head_ref(self, repository: str, pull_request: int) -> str:
        return self._gh("api", f"repos/{repository}/pulls/{pull_request}", "--jq", ".head.ref").strip()

    def comment(self, repository: str, pull_request: int, body: str) -> None:
        self._gh("pr", "comment", str(pull_request), "--repo", repository, "--body-file", "-", stdin=body)

    def react_eyes(self, repository: str, comment_id: int) -> None:
        self._gh(
            "api", "--method", "POST", f"repos/{repository}/issues/comments/{comment_id}/reactions",
            "-f", "content=eyes",
        )


def main() -> int:
    pat = os.environ.get("PAT", "")
    if pat:
        print(f"::add-mask::{pat}", flush=True)
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        event = event_from_payload(json.load(f))
    studio_url = os.environ.get("STUDIO", "")
    return run_mention(
        event,
        app_slug=os.environ.get("APP_SLUG", ""),
        pat=pat,
        studio_url=studio_url,
        domain_id=os.environ.get("DOMAIN_ID", ""),
        studio=UrllibStudio(studio_url, pat),
        github=GhCliGitHub(),
        now=lambda: datetime.now(timezone.utc),
        log=print,
    )


if __name__ == "__main__":
    sys.exit(main())
