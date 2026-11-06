"""Pure core of the GitHub App mention runner.

A pull-request comment that mentions `@vibedata <request>` starts a short Vibedata "mention" Intent that runs the handling-vibedata-mentions skill. This module
decides whether a comment is a real mention, applies the gates in front of Vibedata, makes
the Vibedata calls through a port, and picks the single reply. All I/O goes through
`StudioPort` and `GitHubPort`, so it is stdlib only and runs unchanged under test.

The token is never logged or replied: the shell masks it before any output, and every reply
is scrubbed of it before posting.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol
from urllib.parse import quote, urlencode

ALLOWED_PERMISSIONS: frozenset[str] = frozenset({"write", "maintain", "admin"})
RETRYABLE_OPEN_CODES: frozenset[str] = frozenset({"intent_open_coordination_busy", "agent_server_startup_crashed"})
# Idempotency keys expire long before GitHub stops offering "Re-run job", so a late re-run
# must not start a second request.
MAX_COMMENT_AGE: timedelta = timedelta(hours=1)

ENVIRONMENT = "vibedata-mention"
CALL_TIMEOUT = 20.0
# Open starts a fresh agent container; a cold start alone takes several seconds before load.
OPEN_TIMEOUT = 90.0
OPEN_RETRIES = 2
OPEN_RETRY_DELAY = 5.0
# Composite statuses during which a mention Intent is still working on an earlier mention.
BUSY_STATUSES: frozenset[str] = frozenset({"working", "confirmation", "provisioning"})
PROMPT_HEADER = "Invoke the handling-vibedata-mentions skill now, even if you used it earlier in this conversation, and follow it for this input:"

REPLIES: dict[str, str] = {
    "unrestricted_environment": (
        "The vibedata-mention environment must be limited to the default branch before mentions can run. "
        "A repository admin sets this in Settings → Environments → vibedata-mention → Deployment branches."
    ),
    "missing_pat": (
        "A repository admin must add a Vibedata personal access token as the secret VIBEDATA_PAT in this "
        "repository's vibedata-mention environment; see \"Use a key for GitHub App mentions\" in the "
        "Vibedata user guide."
    ),
    "pr_unreadable": "Vibedata could not read this pull request. Comment again to retry.",
    "unauthorized": (
        "This repository's Vibedata token is invalid or expired. A repository admin must replace the "
        "VIBEDATA_PAT secret with a new token."
    ),
    "forbidden": "This repository's Vibedata token cannot create Intents in this Vibedata Domain.",
    "could_not_start": "Vibedata could not start the request. Comment again to retry.",
}


@dataclass(frozen=True)
class MentionEvent:
    repository: str
    pull_request: int
    comment_id: int
    comment_body: str
    comment_created_at: datetime  # timezone-aware UTC
    commenter_login: str


@dataclass(frozen=True)
class StudioResponse:
    status: int
    body: dict
    etag: str | None


class StudioError(Exception):
    """A non-2xx Vibedata response, or `status=None` when Vibedata could not be reached."""

    def __init__(self, status: int | None, code: str | None = None, detail: str | None = None):
        super().__init__(f"Vibedata {status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class StudioPort(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float,
    ) -> StudioResponse: ...


class GitHubPort(Protocol):
    def permission(self, repository: str, login: str) -> str: ...

    def environment_branch_policy(self, repository: str, name: str) -> dict | None: ...

    def head_ref(self, repository: str, pull_request: int) -> str: ...

    def comment(self, repository: str, pull_request: int, body: str) -> None: ...

    def react_eyes(self, repository: str, comment_id: int) -> None: ...

    def has_eyes_reaction(self, repository: str, comment_id: int) -> bool: ...


# The one trigger word on every install. The preceding-character rule rejects emails
# (`me@vibedata.com`) and paths (`x/@vibedata`); the following-character rule rejects longer
# names (`@vibedata-test`, `@vibedata_x`, `@vibedatabot`).
MENTION = re.compile(r"(?<![A-Za-z0-9_./-])@vibedata(?![A-Za-z0-9_-])", re.IGNORECASE)


def is_mention(body: str) -> bool:
    """`@vibedata` as a whole word, any case, on a line that is not a `>` quote."""
    return any(MENTION.search(line) for line in body.splitlines() if not line.lstrip().startswith(">"))


def build_skill_prompt(
    *,
    repository: str,
    pull_request: int,
    head_branch: str,
    comment: str,
    commenter: str,
    studio_url: str,
    domain_id: str,
) -> str:
    # json.dumps keeps the comment one escaped string, so text shaped like another key
    # cannot add or replace a field.
    payload = {
        "repository": repository,
        "pull_request": pull_request,
        "head_branch": head_branch,
        "comment": comment,
        "commenter": commenter,
        "studio_url": studio_url.rstrip("/"),
        "domain_id": domain_id,
    }
    return PROMPT_HEADER + "\n" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _failure_reply(error: StudioError) -> tuple[str, str]:
    if error.status == 401:
        return "unauthorized", REPLIES["unauthorized"]
    if error.status == 403:
        return "forbidden", REPLIES["forbidden"]
    reply = REPLIES["could_not_start"]
    if error.status is not None:
        code = f" {error.code}" if error.code else ""
        detail = f": {error.detail}" if error.detail else ""
        reply += f" (Vibedata {error.status}{code}{detail})"
    return "could_not_start", reply


def _open(studio: StudioPort, path: str, headers: dict[str, str], sleep: Callable[[float], None]) -> None:
    for attempt in range(OPEN_RETRIES + 1):
        try:
            studio.request("POST", path, headers=headers, timeout=OPEN_TIMEOUT)
            return
        except StudioError as error:
            retryable = error.status == 503 and error.code in RETRYABLE_OPEN_CODES
            if not retryable or attempt == OPEN_RETRIES:
                raise
            sleep(OPEN_RETRY_DELAY)


def _idle_mention_intent(studio: StudioPort, auth: dict[str, str], domain_id: str, title: str) -> str | None:
    """The pull request's own mention Intent when it exists and is idle, else None.

    The search is a contains match, so the title must also match exactly (PR #4 is not PR #42).
    """
    query = urlencode(
        {"creationTypes": "gha", "lifecycleStates": "ACTIVE", "query": title, "top": "20"}, quote_via=quote
    )
    listed = studio.request(
        "GET", f"/api/v1/domains/{quote(domain_id, safe='')}/intents?{query}", headers=auth, timeout=CALL_TIMEOUT
    )
    for item in listed.body["items"]:
        status = (item.get("compositeStatus") or {}).get("status")
        if item.get("title") == title and status not in BUSY_STATUSES:
            return str(item["id"])
    return None


# Another mention took the idle Intent between the lookup and this open: its version moved
# (412) or a turn is now running (409). Either way it is busy, so a new Intent is created.
LOST_RACE_STATUSES: frozenset[int] = frozenset({409, 412})


def _send_to_reused(
    studio: StudioPort, auth: dict[str, str], intent_id: str, prompt: str, sleep: Callable[[float], None]
) -> bool:
    """Open the reused Intent and send the prompt; False when another mention got there first."""
    base = f"/api/v1/intents/{intent_id}"
    try:
        current = studio.request("GET", base, headers=auth, timeout=CALL_TIMEOUT)
        open_headers = {**auth, "If-Match": current.etag} if current.etag else auth
        _open(studio, f"{base}/open", open_headers, sleep)
        studio.request(
            "POST", f"{base}/conversation/send-message", body={"message": prompt}, headers=auth, timeout=CALL_TIMEOUT
        )
    except StudioError as error:
        if error.status in LOST_RACE_STATUSES:
            return False
        raise
    return True


def _react(github: GitHubPort, event: MentionEvent) -> None:
    # The reaction only acknowledges the comment. The request has already reached Vibedata,
    # so a failed reaction must not turn a delivered mention into a failed run.
    try:
        github.react_eyes(event.repository, event.comment_id)
    except Exception:
        pass


def run_mention(
    event: MentionEvent,
    *,
    pat: str,
    studio_url: str,
    domain_id: str,
    studio: StudioPort,
    github: GitHubPort,
    now: Callable[[], datetime],
    log: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one mention; return the process exit code (0 done or deliberately stopped, 1 replied failure)."""
    intent_id: str | None = None

    def finish(outcome: str, code: int, reply: str | None = None) -> int:
        if reply is not None:
            github.comment(event.repository, event.pull_request, reply.replace(pat, "***") if pat else reply)
        log(f"::notice::vibedata-mention outcome={outcome} intent={intent_id or 'none'}")
        return code

    if not is_mention(event.comment_body):
        return finish("no_mention", 0)
    if github.permission(event.repository, event.commenter_login) not in ALLOWED_PERMISSIONS:
        return finish("not_writer", 0)
    if now() - event.comment_created_at > MAX_COMMENT_AGE:
        return finish("stale_comment", 0)
    # GitHub creates a referenced environment that does not exist with no protection rules,
    # which would release the token to any branch.
    if github.environment_branch_policy(event.repository, ENVIRONMENT) is None:
        return finish("unrestricted_environment", 1, REPLIES["unrestricted_environment"])
    if not pat:
        return finish("missing_pat", 1, REPLIES["missing_pat"])
    try:
        head_branch = github.head_ref(event.repository, event.pull_request)
    except Exception:
        return finish("pr_unreadable", 1, REPLIES["pr_unreadable"])

    # Our own reaction lands only after a request reached Vibedata, so it marks a comment
    # that an earlier run already handled.
    if github.has_eyes_reaction(event.repository, event.comment_id):
        return finish("rerun", 0)

    auth = {"Authorization": f"Bearer {pat}"}
    title = f"Vibedata mention on PR #{event.pull_request}"
    prompt = build_skill_prompt(
        repository=event.repository,
        pull_request=event.pull_request,
        head_branch=head_branch,
        comment=event.comment_body,
        commenter=event.commenter_login,
        studio_url=studio_url,
        domain_id=domain_id,
    )
    try:
        # One mention Intent per pull request: reuse it while it is idle, so the Domain
        # keeps one Intent and one conversation per pull request. A busy one is left alone.
        reusable = _idle_mention_intent(studio, auth, domain_id, title)
        if reusable is not None and _send_to_reused(studio, auth, reusable, prompt, sleep):
            intent_id = reusable
            _react(github, event)
            return finish("reused", 0)

        created = studio.request(
            "POST",
            "/api/v1/intents",
            body={
                "domainId": domain_id,
                "title": title,
                "agent": "build",
                "requiresIntentResources": False,
                "creationSource": "gha",
            },
            headers={**auth, "Idempotency-Key": f"gh-mention-{event.comment_id}"},
            timeout=CALL_TIMEOUT,
        )
        intent_id = str(created.body["data"]["id"])
        base = f"/api/v1/intents/{intent_id}"
        # A replayed create looks exactly like the first one; only this runner's activate makes
        # the Intent ACTIVE, so ACTIVE means an earlier run already got that far.
        current = studio.request("GET", base, headers=auth, timeout=CALL_TIMEOUT)
        if current.body["data"]["lifecycleState"] == "ACTIVE":
            _react(github, event)
            return finish("rerun", 0)
        activated = studio.request("POST", f"{base}/activate", headers=auth, timeout=CALL_TIMEOUT)
        open_headers = {**auth, "If-Match": activated.etag} if activated.etag else auth
        _open(studio, f"{base}/open", open_headers, sleep)
        studio.request(
            "POST", f"{base}/conversation/send-message", body={"message": prompt}, headers=auth, timeout=CALL_TIMEOUT
        )
    except StudioError as error:
        outcome, reply = _failure_reply(error)
        return finish(outcome, 1, reply)
    except (KeyError, TypeError):
        # A success status with a body that is not Vibedata's (for example VD_CI_STUDIO_URL
        # pointing at a host that serves an HTML page) still owes the reviewer a reply.
        return finish("could_not_start", 1, REPLIES["could_not_start"])

    _react(github, event)
    return finish("sent", 0)
