"""Whether a static tool's JSON report is clean — shared by Gate 0 and the engine's static check.

A report that is not the expected shape is never clean.
"""


def ruff_ok(r):
    return isinstance(r, list) and len(r) == 0


def sqlfluff_ok(r):
    if isinstance(r, list):
        files = r
    elif isinstance(r, dict):
        files = r.get("files", [])
    else:
        return False
    return all(not f.get("violations") for f in files)


def gitleaks_ok(r):
    if isinstance(r, list):
        findings = r
    elif isinstance(r, dict):
        findings = r.get("findings", [])
    else:
        return False
    return len(findings) == 0
