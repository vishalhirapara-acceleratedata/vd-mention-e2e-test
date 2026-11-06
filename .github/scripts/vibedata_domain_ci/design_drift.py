"""Deep module for ci/design-drift.

Public thin interfaces:
    build_llm_prompt(design_text, manifest, record_artifacts) -> str
    parse_llm_content(content) -> dict | None
    artifact_findings(record_artifacts, project_model_names) -> list[dict]
    run_design_drift(design_text, manifest, modified_names, llm_content,
                     record_artifacts=None) -> dict
    is_vertex_url(api_url) -> bool

Pure: no I/O, no LLM call. The shell (design_drift_runner.py) is responsible
for reading the PR's changed model design records, loading the manifest, calling the Claude API, and
threading the response into run_design_drift as `llm_content`.

Returns:
    {"has_drift": bool, "findings": [{"kind": str, "model": str, "detail": str}, ...]}

Finding kinds:
    missing_model (deterministic, from artifact_findings only — never the LLM),
    grain_mismatch, decision_violated, malformed_llm_response.

Malformed LLM responses produce a deterministic single-finding result of
kind="malformed_llm_response" — the function never raises.

Detection itself — whether the LLM's verdict is *correct* for a given
design-record/manifest pair — is the LLM's job and is covered by evals,
not by unit tests. Those only verify this module's passthrough/validation contract:
a well-formed verdict returned unmodified, a malformed one falling closed.
"""
from __future__ import annotations

import json
import re

from vibedata_domain_ci.dbt_manifest import Manifest

# The workflow's federation decision runs this exact pattern (its VERTEX literal, pinned equal by
# test_the_runner_and_every_bundle_share_one_vertex_pattern; the runner imports it from here), so the
# two can never disagree on a URL: a token minted for a URL the runner sends with a stored key, or a
# Vertex URL that reaches the runner with no token, would each fail with the wrong cause.
# https only, so a federated token never travels in clear text; the global host or a real region
# (us-central1, northamerica-northeast2), never any prefix such as "metadata-"; at most one
# trailing dot and a numeric port; anchored so a lookalike or a later suffix never matches.
# A match proves a Vertex AI host, not a usable endpoint: _vertex_endpoint_error checks the path,
# and the workflow's decision mirrors it, so a bare host mints no token and fails naming the URL
# rather than as a provider 404.
VERTEX_URL_PATTERN = r"^https://([^/?#@]*@)?([a-z]+-[a-z]+[1-9][0-9]?-)?aiplatform\.googleapis\.com\.?(:[0-9]+)?([/?#]|$)"
# ASCII-only case folding matches the workflow, which lowercases under LC_ALL=C.
_VERTEX_URL = re.compile(VERTEX_URL_PATTERN, re.ASCII | re.IGNORECASE)


def is_vertex_url(api_url: str) -> bool:
    """True when `api_url` is a Vertex AI host: design-drift then sends a Google token, never LLM_API_KEY."""
    # urllib strips CR, LF and tab before connecting, so a URL holding one could reach a host it
    # does not spell; the workflow's decision refuses those too.
    if any(c in api_url for c in "\r\n\t"):
        return False
    return bool(_VERTEX_URL.match(api_url))


# missing_model is decided only by artifact_findings; the LLM may return the other two.
_LLM_KINDS = {"grain_mismatch", "decision_violated"}


def artifact_findings(record_artifacts: set[str], project_model_names: set[str]) -> list[dict]:
    return [
        {"kind": "missing_model", "model": name,
         "detail": "named in a changed model design record's artifacts but no such model "
                   "exists in the dbt project"}
        for name in sorted(record_artifacts - project_model_names)
    ]


def _iter_balanced_json_objects(text: str):
    """Yield each substring forming a complete balanced {...} span in text,
    in the order they appear. Braces inside JSON string literals (respecting
    \\" escapes) are not counted as structural braces, so a finding's free-text
    detail field containing a literal brace can't corrupt the scan.

    Known limitation: an unescaped/unmatched literal quote in text outside
    any JSON object (e.g. prose quoting an identifier without closing the
    quote) desyncs the in_string flag for the remainder of the scan, since
    it is not scoped per-candidate. Accepted risk — see parse_llm_content.
    """
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:i + 1]
                    start = None


def parse_llm_content(content: str) -> dict | None:
    """Extract the last *successfully-parseable* JSON object found in raw LLM
    content — tolerates markdown code fences, leading prose, and multiple
    JSON-shaped fragments (e.g. an example the model echoes before its real
    answer). Returns None if nothing parses as a JSON object; run_design_drift
    feeds that into _validate_llm_response's existing fail-closed path.

    Known, accepted limitations (accepted rather than requiring a full JSON
    tokenizer):
    this returns the last fragment that *parses*, not necessarily the
    model's actual last-written one — if a complete valid example precedes a
    real answer that gets truncated mid-object, the complete example is
    returned instead of falling closed. See
    test_parse_llm_content_prefers_last_parseable_over_truncated_real_answer
    and test_parse_llm_content_unmatched_quote_in_prose_can_desync_scanning
    for the exact, tested boundaries of this behavior.
    """
    if not isinstance(content, str) or not content.strip():
        return None
    for candidate in reversed(list(_iter_balanced_json_objects(content))):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _compact_node(node: dict) -> dict:
    # Column names only (no metadata) — deliberately strips description, data_type, etc.
    # to keep prompt small. Present/absent is all the LLM needs for column-level drift.
    config = node.get("config", {})
    result = {"columns": sorted(node.get("columns", {}).keys())}
    if mat := config.get("materialized"):
        result["materialization"] = mat
    if uk := config.get("unique_key"):
        result["unique_key"] = uk
    return result


def build_llm_prompt(design_text: str, manifest: dict, record_artifacts: list[str]) -> str:
    recorded = set(record_artifacts)
    compact = {
        v.get("name", k): _compact_node(v)
        for k, v in manifest.get("nodes", {}).items()
        if v.get("name") in recorded
    }
    kind_enum = ", ".join(sorted(_LLM_KINDS))
    return (
        "You are a CI gate. The design records below are the durable model design records this "
        "PR changed (frontmatter artifacts, ## Grain, and append-only D-NN decisions). Compare "
        "them against the dbt manifest fragment for the models those records name. Report a "
        "grain_mismatch when a model's grain differs from its record's Grain, and a "
        "decision_violated (cite the D-NN in detail) when compiled SQL or config contradicts a "
        "recorded decision. Do not report anything a record does not state. A record's "
        "\"## Logical Model\" section names the canonical logical model the model serves; a "
        "separate check validates it against logical-model/, so never report drift from it.\n\n"
        "Respond with a single JSON object and nothing else — no prose, no markdown code "
        "fences — of the exact form:\n"
        '{"has_drift": <bool>, "findings": [{"kind": <string>, "model": <string>, '
        '"detail": <string>}, ...]}\n'
        f"Valid values for \"kind\" are exactly: {kind_enum}.\n"
        "If there is no drift, respond with has_drift=false and an empty findings array.\n\n"
        f"=== design records ===\n{design_text}\n\n"
        f"=== manifest (models the records name) ===\n{json.dumps(compact, indent=2)}\n"
    )


def _validate_llm_response(llm_response: dict) -> dict | None:
    """Returns the validated response, or None if malformed."""
    if not isinstance(llm_response, dict):
        return None
    if "has_drift" not in llm_response or "findings" not in llm_response:
        return None
    if not isinstance(llm_response["has_drift"], bool):
        return None
    if not isinstance(llm_response["findings"], list):
        return None
    for f in llm_response["findings"]:
        if not isinstance(f, dict):
            return None
        if not {"kind", "model", "detail"} <= f.keys():
            return None
        if f["kind"] not in _LLM_KINDS:
            return None
    return llm_response


def run_design_drift(design_text: str, manifest: dict, modified_names: list[str], llm_content: str,
                     record_artifacts: set[str] | None = None) -> dict:
    project_models = {n.get("name") for n in Manifest.from_dict(manifest).own_models().values()}
    deterministic = artifact_findings(set(record_artifacts or ()), project_models)
    parsed = parse_llm_content(llm_content)
    validated = _validate_llm_response(parsed) if parsed is not None else None
    if validated is None:
        return {"has_drift": True, "findings": deterministic + [{
            "kind": "malformed_llm_response", "model": "",
            "detail": "LLM response did not conform to the required schema.",
        }]}
    return {
        "has_drift": bool(deterministic) or validated["has_drift"],
        "findings": deterministic + list(validated["findings"]),
    }
