"""Thin shell for ci/design-drift (MotherDuck).

gather → call → dispatch:
  1. Read the PR's changed paths and the changed model design records (docs/design/models/**),
     manifest.json, modified.json.
  2. Read LLM_API_URL, LLM_API_KEY, LLM_MODEL from env. An Amazon Bedrock runtime LLM_API_URL
     is refused before LLM_API_KEY is read: this OpenAI-compatible request does not fit it.
     A Vertex AI LLM_API_URL reads LLM_GCP_ACCESS_TOKEN instead and never LLM_API_KEY.
  3. Build prompt; POST to OpenAI-compatible chat completions API with
     response_format=json_object; on HTTP 400 retry once without it;
     receive structured JSON as raw message.content text.
  4. Call run_design_drift (pure).
  5. Post ci/design-drift status; sys.exit(1) on drift or error.

All gate logic lives in design_drift.run_design_drift; this module owns only I/O.

Usage:
    design_drift_runner.py \\
        --pr-number 42 --head-sha abc123 --changed-paths changed-paths.txt \\
        --manifest target/manifest.json \\
        --modified reports/modified.json

Environment:
    GITHUB_REPOSITORY, GH_TOKEN, GITHUB_RUN_ID, GITHUB_SERVER_URL  (status post)
    LLM_API_KEY                                                      (injected by workflow)
    LLM_GCP_ACCESS_TOKEN  short-lived token minted by workload identity federation, sent to a
                  Vertex AI LLM_API_URL in place of LLM_API_KEY      (injected by workflow)
    LLM_GCP_WORKLOAD_IDENTITY_PROVIDER, LLM_GCP_SERVICE_ACCOUNT, LLM_GCP_AUTH_OUTCOME
                  read only to say why no token was minted           (injected by workflow)
    LLM_API_URL   base URL for OpenAI-compatible provider            (default: https://api.openai.com)
                  Amazon Bedrock runtime hosts (*.bedrock-runtime[-fips].*.amazonaws.com) are refused
    LLM_MODEL     model identifier                                   (default: gpt-4o)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from vibedata_domain_ci import emit_status
from vibedata_domain_ci import notify_render
from vibedata_domain_ci import pr_comment
from vibedata_domain_ci import design_record_io
from vibedata_domain_ci.design_records import select_design_records
from vibedata_domain_ci.design_drift import build_llm_prompt, is_vertex_url, run_design_drift
from vibedata_domain_ci.design_drift import VERTEX_URL_PATTERN  # noqa: F401 — the bundles' VERTEX literal is pinned to it

CONTEXT = "ci/design-drift"
DEFAULT_LLM_API_URL = "https://api.openai.com"
DEFAULT_LLM_MODEL = "gpt-4o"
MAX_OUTPUT_TOKENS = 4096


def _read_text(path: str) -> str:
    with open(path) as f:
        return f.read()


def build_llm_url(api_url: str) -> str:
    """Build the chat-completions URL for any LLM_API_URL base-URL shape."""
    base = api_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    # Databricks serves chat completions directly under /serving-endpoints (no /v1), and
    # Vertex AI under its versioned /endpoints/openapi path.
    if base.endswith(("/v1", "/serving-endpoints", "/endpoints/openapi")):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _post_chat_completion(url: str, api_key: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("content-type", "application/json")
    req.add_header("User-Agent", "python-httpx/0.27.0")
    # OpenCode Zen's Go-plan gateway rejects any request lacking this header
    # (MissingSessionID) — a fresh UUID per call satisfies it. Other
    # OpenAI-compatible providers ignore unrecognized headers.
    req.add_header("x-opencode-session", str(uuid.uuid4()))
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def call_llm(api_key: str, prompt: str, api_url: str, model: str) -> str:
    url = build_llm_url(api_url)
    body = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        payload = _post_chat_completion(url, api_key, body)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise RuntimeError(f"LLM API HTTP {e.code}: {e.read().decode(errors='replace')}") from e
        first_body = e.read().decode(errors="replace")
        fallback_body = {k: v for k, v in body.items() if k != "response_format"}
        try:
            payload = _post_chat_completion(url, api_key, fallback_body)
        except urllib.error.HTTPError as e2:
            raise RuntimeError(
                f"LLM API HTTP {e2.code} on response_format fallback retry (initial 400 was: "
                f"{first_body}): {e2.read().decode(errors='replace')}"
            ) from e2
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM response missing choices[0].message.content: {payload}") from e


def _run_url() -> str:
    base = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"{base}/{repo}/actions/runs/{run_id}"


def _post(head_sha: str, state: str, description: str) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    emit_status.emit_status(repo, head_sha, CONTEXT, state, description, _run_url())


def _post_pr_comment(pr_number: str, result: dict | None, compared_models: list[str] | None = None) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return
    body = notify_render.render_design_drift_comment(result, compared_models)
    try:
        pr_comment.upsert(notify_render.DESIGN_DRIFT_MARKER, body, pr_number, repo)
    except BaseException as e:
        print(f"Failed to post PR comment: {e}", flush=True)


def _summary(result: dict) -> str:
    if not result["has_drift"]:
        return "model design records match state:modified — no drift"
    kinds = sorted({f["kind"] for f in result["findings"]})
    return f"design drift detected: {', '.join(kinds)}"


def _is_bedrock_url(api_url: str) -> bool:
    # A trailing dot still resolves to AWS; matching a label also covers VPC endpoint hosts.
    host = (urllib.parse.urlparse(api_url).hostname or "").rstrip(".")
    labels = host.split(".")
    return host.endswith(".amazonaws.com") and ("bedrock-runtime" in labels or "bedrock-runtime-fips" in labels)


# The Vertex AI host test lives in design_drift (pure), shared with github_requirements' LLM-key rule.
_is_vertex_url = is_vertex_url


def _vertex_endpoint_error(api_url: str) -> str | None:
    path = urllib.parse.urlsplit(api_url).path.rstrip("/").removesuffix("/chat/completions")
    if path.endswith("/endpoints/openapi"):
        return None
    return "LLM_API_URL is a Vertex AI host without /endpoints/openapi: set the full endpoint URL on the profile"


def _empty_variable(env_name: str, remedy: str) -> str:
    # The workflow reads the federation identity from fixed VD_CI_* names only, never a ci-config.yml key.
    return f"VD_CI_{env_name} is empty: {remedy}, then sync"


def _vertex_access_token() -> str:
    """The federated token, or a ValueError naming the one cause to fix.

    Each message leads with its cause and stays short: GitHub truncates a status description
    at 140 characters, and the "design-drift error: ValueError: " prefix takes 32 of them.
    """
    token = os.environ.get("LLM_GCP_ACCESS_TOKEN") or ""
    if token:
        return token
    # The empty-variable causes come before the step outcome: the decision federates only when both
    # identity variables are set, so an empty one always arrives with the step skipped, and the
    # skipped message would then blame the URL. A failed step implies both are set.
    if not os.environ.get("LLM_GCP_WORKLOAD_IDENTITY_PROVIDER"):
        raise ValueError(_empty_variable("LLM_GCP_WORKLOAD_IDENTITY_PROVIDER", "set the CI provider on the profile"))
    if not os.environ.get("LLM_GCP_SERVICE_ACCOUNT"):
        raise ValueError(_empty_variable("LLM_GCP_SERVICE_ACCOUNT", "set the service account on the profile"))
    if os.environ.get("LLM_GCP_AUTH_OUTCOME") == "failure":
        print(
            "The 'Authenticate to Google Cloud' step failed. Check that LLM_GCP_WORKLOAD_IDENTITY_PROVIDER "
            "is the full provider resource name, that LLM_GCP_SERVICE_ACCOUNT grants "
            "roles/iam.workloadIdentityUser to this repository's principalSet, and that the pool's "
            "attribute condition admits this repository. The step's log holds Google's own error.",
            flush=True,
        )
        raise ValueError("Google auth step failed: check the provider, workloadIdentityUser binding, pool condition")
    if os.environ.get("LLM_GCP_AUTH_OUTCOME") == "skipped":
        # Both inputs are set, so the decision's URL, which is not the mapped one this run reads,
        # was not a full Vertex AI endpoint URL.
        raise ValueError("Google auth step skipped: VD_CI_LLM_API_URL is not a full Vertex AI endpoint URL")
    if os.environ.get("LLM_GCP_AUTH_OUTCOME") == "success":
        raise ValueError("Google auth step succeeded without an access_token: check token_format and the output name")
    raise ValueError("no Google access token was minted: see the 'Authenticate to Google Cloud' step")


def evaluate(changed_paths: str, manifest_path: str, modified_path: str) -> dict:
    """Gather the changed design records, ask the LLM, and judge drift — no status, no comment.

    Returns {"skipped": True} when no model design record changed, {"error": str} when a
    gather or LLM call failed, else {"result": run_design_drift's result, "compared_models"}.
    """
    try:
        changed = design_record_io.read_changed_paths(changed_paths)
        records = [r for r in select_design_records(changed, design_record_io.read_contents(changed))
                   if r.kind == "model"]
        if not records:
            return {"skipped": True}
        design_text = "\n\n".join(f"<!-- {r.path} -->\n{r.text}" for r in records)
        record_artifacts = {name for r in records for name in r.artifacts}
        manifest = json.loads(_read_text(manifest_path))
        modified_names = json.loads(_read_text(modified_path))
        api_url = os.environ.get("LLM_API_URL") or DEFAULT_LLM_API_URL
        # Checked before LLM_API_KEY is read: a Sync never deletes old secrets, so a key left by
        # an earlier provider would otherwise be sent as a Bearer token to an AWS host.
        if _is_bedrock_url(api_url):
            raise ValueError("Amazon Bedrock runtime hosts are not supported by the design-drift gate (LLM_API_URL)")
        if _is_vertex_url(api_url):
            # Never fall back to LLM_API_KEY: a key left by an earlier provider is not a Google token.
            # No x-goog-user-project header: Vertex AI bills and checks quota against the project
            # in the URL path, and the header would add a serviceusage.services.use requirement.
            if endpoint_error := _vertex_endpoint_error(api_url):
                raise ValueError(endpoint_error)
            api_key = _vertex_access_token()
        else:
            api_key = os.environ.get("LLM_API_KEY")
            # An empty key stays allowed for a configured URL: a keyless OpenAI-compatible
            # endpoint is legitimate. Only the default host is certain to refuse it.
            if api_key is None or (not api_key and not os.environ.get("LLM_API_URL")):
                raise ValueError("LLM_API_KEY is missing or empty and LLM_API_URL is not a Vertex AI URL")
        model = os.environ.get("LLM_MODEL") or DEFAULT_LLM_MODEL
        prompt = build_llm_prompt(design_text, manifest, sorted(record_artifacts))
        llm_content = call_llm(api_key, prompt, api_url, model)
    except Exception as e:  # gather/call failure
        return {"error": f"{type(e).__name__}: {e}"}

    result = run_design_drift(design_text, manifest, modified_names, llm_content,
                              record_artifacts=record_artifacts)
    return {"result": result, "compared_models": sorted(record_artifacts)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--changed-paths", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--modified", required=True)
    args = parser.parse_args(argv)

    evaluation = evaluate(args.changed_paths, args.manifest, args.modified)
    if evaluation.get("skipped"):
        _post(args.head_sha, "success", "no model design record changed — skipped")
        _post_pr_comment(args.pr_number, result={"has_drift": False, "skipped": True, "findings": []})
        return 0
    if "error" in evaluation:
        # Print before posting: a failed status post must not replace the original diagnostic.
        print(f"design-drift error: {evaluation['error']}", flush=True)
        _post(args.head_sha, "failure", f"design-drift error: {evaluation['error']}")
        _post_pr_comment(args.pr_number, result=None)
        return 1

    result = evaluation["result"]
    for finding in result["findings"]:
        print(f"design drift: {finding['kind']} {finding['model']}: {finding['detail']}", flush=True)
    _post(args.head_sha, "failure" if result["has_drift"] else "success", _summary(result))
    _post_pr_comment(args.pr_number, result=result, compared_models=evaluation["compared_models"])
    return 1 if result["has_drift"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
