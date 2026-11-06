"""Unified Fabric/Azure transport: token acquisition, retry, LRO polling, OneLake DFS reads and writes.

Public surface:
  get_token(audience) -> str
  request(method, path, body=None, audience='fabric', retries=3) -> dict
  request_long_running(method, path, body, audience='fabric', timeout_s=120, poll_interval_s=5, retries=3) -> dict
  dfs_request(method, url, audience='storage', data=None, params=None) -> None
  blob_request(method, workspace, path, ...) -> (bytes, headers)
  dfs_delete_recursive(workspace, path, page_limit=1000) -> bool
  dfs_list(workspace, directory, page_limit=100) -> list[dict] | None   (raises OneLakeListError)
  livy_run(workspace_id, lakehouse_id, statements, poll_interval_s=5, timeout_s=1800) -> list[list]

Audiences: fabric, powerbi, storage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import CancelledError


class ConditionalWriteUnsupported(RuntimeError):
    """The provider explicitly rejected a required write condition."""


def blob_request(method, workspace, path, *, data=None, headers=None, params=None, limit=4096):
    """One bounded Blob request; authenticated error bodies never enter deploy output."""
    base = os.environ.get("ONELAKE_BLOB_BASE_URL", "https://onelake.blob.fabric.microsoft.com")
    url = base.rstrip("/") + "/" + urllib.parse.quote(workspace, safe="")
    if path:
        url += "/" + urllib.parse.quote(path, safe="/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {get_token('storage')}")
        req.add_header("x-ms-version", "2021-12-02")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=60) as response:
            rejected = {s.strip().lower() for s in response.headers.get("x-ms-rejected-headers", "").split(",")}
            if rejected & {"if-match", "if-none-match", "x-ms-blob-type"}:
                raise ConditionalWriteUnsupported("OneLake rejected a required Blob write condition")
            size = response.headers.get("Content-Length")
            if size is not None and (int(size) < 0 or int(size) > limit):
                raise ValueError("Blob response exceeds bounds")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("Blob response exceeds bounds")
            return body, response.headers
    except (CancelledError, ConditionalWriteUnsupported):
        raise
    except urllib.error.HTTPError as exc:
        status, code = exc.code, exc.headers.get("x-ms-error-code", "")
        exc.close()
        if code in ("UnsupportedHeader", "UnsupportedOperation", "NotImplemented"):
            raise ConditionalWriteUnsupported("OneLake rejected the required Blob operation") from None
        if status == 404 and code in ("BlobNotFound", "PathNotFound", "ResourceNotFound"):
            raise FileNotFoundError("ingestion provider path not found") from None
        if method == "PUT" and status in (409, 412) and code in ("BlobAlreadyExists", "PathAlreadyExists", "ConditionNotMet", "TargetConditionNotMet"):
            raise FileExistsError("conditional ingestion write conflict") from None
        raise RuntimeError(f"ingestion Blob operation failed (HTTP {status})") from None
    except Exception as exc:
        raise RuntimeError(f"ingestion Blob operation failed ({type(exc).__name__})") from None


def dfs_delete_recursive(workspace, path, *, page_limit=1000):
    """Delete a OneLake directory tree; False when it was already absent.

    ADLS answers a large recursive delete in pages, handing back `x-ms-continuation`
    until the tree is gone, so a repeated or unending token must not read as success.
    """
    base = os.environ.get("ONELAKE_DFS_BASE_URL", "https://onelake.dfs.fabric.microsoft.com")
    url = (base.rstrip("/") + "/" + urllib.parse.quote(workspace, safe="")
           + "/" + urllib.parse.quote(path, safe="/"))
    token, seen = None, set()
    for _ in range(page_limit):
        params = {"recursive": "true", **({"continuation": token} if token else {})}
        try:
            req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), method="DELETE")
            req.add_header("Authorization", f"Bearer {get_token('storage')}")
            req.add_header("x-ms-version", "2021-12-02")
            with urllib.request.urlopen(req, timeout=60) as response:
                token = response.headers.get("x-ms-continuation")
        except CancelledError:
            raise
        except urllib.error.HTTPError as exc:
            status, code = exc.code, exc.headers.get("x-ms-error-code", "")
            exc.close()
            # Absent only on the first page: a 404 mid-way means the tree changed under us.
            if status == 404 and code in ("PathNotFound", "BlobNotFound", "ResourceNotFound") and token is None:
                return False
            raise RuntimeError(f"OneLake folder delete failed (HTTP {status})") from None
        except Exception as exc:
            raise RuntimeError(f"OneLake folder delete failed ({type(exc).__name__})") from None
        if not token:
            return True
        if token in seen:
            raise RuntimeError("OneLake folder delete repeated its continuation token")
        seen.add(token)
    raise RuntimeError("OneLake folder delete exceeded its page limit")


class OneLakeListError(RuntimeError):
    """A failed OneLake listing; `status` is the HTTP status when the service answered one."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def dfs_list(workspace, directory, *, page_limit=100):
    """List one level of a OneLake directory; None when the directory does not exist.

    Absence and emptiness are different answers on purpose: a Warehouse whose schemas hold no
    tables has no `Tables` directory at all, which is a new Domain rather than a failure, while
    an existing directory with nothing in it is the empty list. ADLS pages a wide listing with
    `x-ms-continuation`, so a repeated or unending token must fail rather than loop.
    """
    base = os.environ.get("ONELAKE_DFS_BASE_URL", "https://onelake.dfs.fabric.microsoft.com")
    url = base.rstrip("/") + "/" + urllib.parse.quote(workspace, safe="")
    entries, token, seen = [], None, set()
    for _ in range(page_limit):
        params = {"resource": "filesystem", "recursive": "false", "directory": directory}
        if token:
            params["continuation"] = token
        try:
            req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), method="GET")
            req.add_header("Authorization", f"Bearer {get_token('storage')}")
            req.add_header("x-ms-version", "2021-12-02")
            with urllib.request.urlopen(req, timeout=60) as response:
                token = response.headers.get("x-ms-continuation")
                entries.extend(json.loads(response.read() or b"{}").get("paths", []))
        except CancelledError:
            raise
        except urllib.error.HTTPError as exc:
            status, code = exc.code, exc.headers.get("x-ms-error-code", "")
            exc.close()
            # Absent only on the first page: a 404 mid-way means the tree changed under us.
            if status == 404 and token is None:
                return None
            raise OneLakeListError(f"OneLake listing of {directory!r} failed (HTTP {status} {code})".rstrip(), status) from None
        except Exception as exc:
            # A refused connection, a timeout or an unreadable body is a failed read, never an
            # empty directory — the same contract the delete above keeps.
            # The cause is kept: an expired az login (get_token) must say so, and none of these
            # non-HTTP errors carries a provider response body.
            raise OneLakeListError(f"OneLake listing of {directory!r} failed ({type(exc).__name__}: {exc})") from None
        if not token:
            return entries
        if token in seen:
            raise OneLakeListError(f"OneLake listing of {directory!r} repeated its continuation token")
        seen.add(token)
    raise OneLakeListError(f"OneLake listing of {directory!r} exceeded its page limit")


def _fabric_api_base() -> str:
    """Read FABRIC_API_BASE_URL per call (not at import time) so a
    caller can override it without reloading this module — same pattern as
    emit_status.py's GITHUB_API_BASE_URL / fetch_prod_state.py's
    ONELAKE_DFS_BASE_URL."""
    return os.environ.get("FABRIC_API_BASE_URL", "https://api.fabric.microsoft.com/v1")


def _powerbi_api_base() -> str:
    """Read POWERBI_API_BASE_URL per call — same pattern as _fabric_api_base()."""
    return os.environ.get("POWERBI_API_BASE_URL", "https://api.powerbi.com/v1.0/myorg")


FABRIC_API = _fabric_api_base()  # kept for existing external readers; reflects env at import time only
POWERBI_API = _powerbi_api_base()  # kept for existing external readers; reflects env at import time only

_AUDIENCE_RESOURCE: dict[str, str] = {
    "fabric": "https://api.fabric.microsoft.com",
    "powerbi": "https://analysis.windows.net/powerbi/api",
    "storage": "https://storage.azure.com",
}


def _base_url(audience: str) -> str:
    if audience == "fabric":
        return _fabric_api_base()
    if audience == "powerbi":
        return _powerbi_api_base()
    raise KeyError(audience)


def get_token(audience: str) -> str:
    """Acquire an Azure access token for the given audience via az CLI."""
    resource = _AUDIENCE_RESOURCE[audience]
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", resource],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["accessToken"]


def _retry_delay(code: int, attempt: int, headers) -> int:
    """Return seconds to wait before retry attempt. 430 uses exponential backoff; others honor Retry-After."""
    if code == 430:
        return 30 * (2 ** attempt)
    if code == 500:
        return 1
    return int(headers.get("Retry-After", 5))


def request(method: str, path: str, body: dict = None, audience: str = "fabric", retries: int = 3) -> dict:
    """Fabric REST API call with retry on 429/430/500/503 honoring Retry-After."""
    base = _base_url(audience)
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body else None

    for attempt in range(retries):
        token = get_token(audience)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 430, 500, 503) and attempt < retries - 1:
                delay = _retry_delay(e.code, attempt, e.headers)
                print(f"Retrying after {delay}s (attempt {attempt + 1}/{retries}): {e.code} {e.msg}", flush=True)
                time.sleep(delay)
                continue
            body_text = e.read().decode(errors="replace")
            print(f"HTTP {e.code} {method} {url}: {body_text}", file=sys.stderr)
            raise
    raise RuntimeError(f"Failed after {retries} retries: {method} {path}")


def request_multipart(method: str, path: str, file_content: bytes, filename: str, content_type: str = "text/plain", audience: str = "fabric", retries: int = 3) -> dict:
    """Fabric REST API call with a multipart/form-data file payload.

    Used for endpoints that require file upload (e.g. staging/libraries).
    """
    boundary = b"boundary12345"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
        b"Content-Type: " + content_type.encode() + b"\r\n"
        b"\r\n"
        + file_content +
        b"\r\n--" + boundary + b"--\r\n"
    )

    base = _base_url(audience)
    url = f"{base}{path}"

    for attempt in range(retries):
        token = get_token(audience)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary.decode()}")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 430) and attempt < retries - 1:
                delay = _retry_delay(e.code, attempt, e.headers)
                print(f"Retrying after {delay}s (attempt {attempt + 1}/{retries}): {e.code} {e.msg}", flush=True)
                time.sleep(delay)
                continue
            body_text = e.read().decode(errors="replace")
            print(f"HTTP {e.code} {method} {url}: {body_text}", file=sys.stderr)
            raise
    raise RuntimeError(f"Failed after {retries} retries: {method} {path}")


def request_long_running(
    method: str, path: str, body: dict,
    audience: str = "fabric", timeout_s: int = 120, poll_interval_s: int = 5, retries: int = 3,
) -> dict:
    """POST/PATCH/PUT to Fabric; handle 202 + Location/operationId polling.

    The initial request is retried on 429/430/503 (never on a 500 — see below).

    Returns the initial response body dict (contains item id on 201; empty on 202).
    Raises RuntimeError on LRO failure or timeout.
    """
    base = _base_url(audience)
    url = f"{base}{path}"
    data = json.dumps(body).encode()

    # Retry only the answers that mean the request was not processed (throttled, unavailable).
    # Unlike request(), never a 500: a create can have happened behind one, and this function
    # creates items, so a retry there could meet — or duplicate — what the first attempt made.
    for attempt in range(retries):
        token = get_token(audience)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                location = resp.getheader("Location")
                raw = resp.read()
                parsed = (json.loads(raw) or {}) if raw else {}
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 430, 503) and attempt < retries - 1:
                delay = _retry_delay(e.code, attempt, e.headers)
                print(f"Retrying after {delay}s (attempt {attempt + 1}/{retries}): {e.code} {e.msg}", flush=True)
                time.sleep(delay)
                continue
            body_text = e.read().decode(errors="replace")
            print(f"HTTP {e.code} {method} {url}: {body_text}", file=sys.stderr)
            raise

    if not location and "operationId" in parsed:
        location = f"{_fabric_api_base()}/operations/{parsed['operationId']}"

    if status == 202:
        _poll_operation(location, token, timeout_s, poll_interval_s)

    return parsed


def _poll_operation(
    operation_url: str | None, token: str, timeout_s: int, poll_interval_s: int
) -> None:
    if not operation_url:
        raise RuntimeError(
            "Fabric returned 202 Accepted but no operation URL was found in the "
            "Location header or response body."
        )
    print(f"Polling Fabric operation: {operation_url}", flush=True)
    deadline = time.monotonic() + timeout_s
    last_status = "Unknown"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        req = urllib.request.Request(operation_url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(
                f"Failed to poll Fabric operation (HTTP {e.code}): {body_text}"
            ) from e
        last_status = result.get("status", "Unknown")
        print(f"  Operation status [{attempt}]: {last_status}", flush=True)
        if last_status == "Succeeded":
            return
        if last_status == "Failed":
            error = result.get("error", {})
            msg = f"{error.get('errorCode', 'UnknownError')}: {error.get('message', str(result))}"
            raise RuntimeError(f"Fabric operation failed — {msg}")
        time.sleep(poll_interval_s)
    raise RuntimeError(
        f"Fabric operation timed out after {timeout_s}s "
        f"(last status: {last_status!r}). "
        "The item it creates or runs may not be available in the workspace yet."
    )


def dfs_request(
    method: str, url: str, audience: str = "storage", data: bytes = None, params: dict = None
) -> None:
    """ADLS Gen2 DFS REST call (PUT/PATCH). Raises on non-2xx."""
    token = get_token(audience)
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    effective_data = data if data is not None else b""
    req = urllib.request.Request(url, data=effective_data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Length", str(len(effective_data)))
    try:
        with urllib.request.urlopen(req):
            pass
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        print(f"HTTP {e.code} {method} {url}: {body_text}", file=sys.stderr)
        raise


_LIVY = "/workspaces/{workspace}/lakehouses/{lakehouse}/livyapi/versions/2023-12-01/sessions"


class LivyError(RuntimeError):
    """A Livy session that would not start, or a statement that did not finish `ok`."""


def _livy_wait(path: str, done: set, timeout_s: int, poll_interval_s: int, what: str) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        body = request("GET", path)
        state = (body.get("livyInfo") or {}).get("currentState") or body.get("state", "")
        if state in done:
            return body
        fabric_state = (body.get("fabricSessionStateInfo") or {}).get("state", "")
        if {state, fabric_state} & {"dead", "error", "killed", "cancelled", "shutting_down"}:
            raise LivyError(f"{what} ended in state {state or fabric_state}")
        if time.monotonic() > deadline:
            raise LivyError(f"{what} did not finish within {timeout_s}s (state {state})")
        time.sleep(poll_interval_s)


def livy_run(workspace_id: str, lakehouse_id: str, statements: list, poll_interval_s: int = 5,
             timeout_s: int = 1800) -> list:
    """Run `statements` — `(kind, code)` pairs, kind `sql` or `pyspark` — in order in one Livy session attached
    to the lakehouse, then close the session. Returns each statement's result rows (`[]` when it has none)."""
    sessions = _LIVY.format(workspace=workspace_id, lakehouse=lakehouse_id)
    session = request("POST", sessions, {"name": "vibedata-domain-ci"})["id"]
    try:
        _livy_wait(f"{sessions}/{session}", {"idle"}, timeout_s, poll_interval_s, "the Livy session")
        rows = []
        for kind, code in statements:
            statement = request("POST", f"{sessions}/{session}/statements", {"code": code, "kind": kind})["id"]
            body = _livy_wait(f"{sessions}/{session}/statements/{statement}", {"available", "error", "cancelled"},
                              timeout_s, poll_interval_s, f"Livy statement {code[:80]!r}")
            output = body.get("output") or {}
            if output.get("status") != "ok":
                raise LivyError(f"Livy statement {code[:80]!r} failed: {output.get('ename', '')}: "
                                f"{output.get('evalue', '')}".rstrip(": "))
            data = (output.get("data") or {}).get("application/json")
            rows.append(data.get("data", []) if isinstance(data, dict) else [])
        return rows
    finally:
        try:
            request("DELETE", f"{sessions}/{session}")
        except Exception as exc:  # a session left open times out on its own; never the caller's outcome
            print(f"::warning::could not close Livy session {session}: {exc}", flush=True)
