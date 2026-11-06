"""Pure helpers for ci-config.yml loading and intent-slug validation.

Extracted from preflight.py so these functions are unit-testable in isolation
and reusable without going through the CLI shell.
"""
import os
import re

from vibedata_domain_ci.platform_enum import FABRIC_LAKEHOUSE, PLATFORMS, VALID_PLATFORMS

try:
    import yaml
except ImportError:
    yaml = None


def locate_ci_config(root: str = ".") -> str:
    """Return the ci-config.yml path: always .workflow/ci-config.yml."""
    return ".workflow/ci-config.yml"


def locate_project_dir(root: str = ".") -> str:
    """Return C6 dbt project path: transformation/ with root fallback."""
    candidate = os.path.join(root, "transformation", "dbt_project.yml")
    return "transformation" if os.path.isfile(candidate) else "."

INTENT_SLUG_RE = re.compile(r"^intent/[a-z0-9][a-z0-9\-]+$")


def load_config_keys() -> dict:
    """The released configuration-key list, `data/config-keys.yml`: `required_keys` per platform,
    `keys` (dotted path -> {alias?}), `var_keys` and `retired_keys`. The only source of these lists."""
    with open(os.path.join(os.path.dirname(__file__), "data", "config-keys.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


_CONFIG_KEYS = load_config_keys()
_REQUIRED_KEYS_BY_PLATFORM = _CONFIG_KEYS["required_keys"]
_KNOWN_KEYS = set(_CONFIG_KEYS["keys"]) | set(_CONFIG_KEYS["var_keys"])
_KEY_ALIASES = {k: v["alias"] for k, v in _CONFIG_KEYS["keys"].items() if (v or {}).get("alias")}


def validate_config(config: dict, known_keys, retired_keys: dict) -> list[dict]:
    """Findings for every key in `config`, walking nested mappings as dotted paths.

    A retired key is an error naming it and, for a rename, its replacement. A key neither known nor
    retired is a warning only. A mapping is walked only under a known key that has known or retired
    children, so a free-form value is never mistaken for keys.
    Returns `[{"severity": "error" | "warning", "message": str}]`.
    """
    nested = {k.rsplit(".", 1)[0] for k in [*known_keys, *retired_keys] if "." in k}
    findings = []

    def walk(mapping, prefix):
        for key, value in mapping.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            retired = retired_keys.get(path)
            if retired is not None:
                since = f" in {retired['since']}" if retired.get("since") else ""
                message = (f"`{path}` was renamed to `{retired['replaced_by']}`{since}"
                           if retired.get("replaced_by") else f"`{path}` was removed{since}")
                findings.append({"severity": "error", "message": message})
            elif path in known_keys:
                if isinstance(value, dict) and path in nested:
                    walk(value, path)
            else:
                findings.append({"severity": "warning", "message": f"unknown key `{path}`"})

    walk(config, "")
    return findings


def _is_blank(value) -> bool:
    """True when a declared key carries no usable value.

    `KEY:` with nothing after it parses as None, and `KEY: ""` as the empty
    string. Both satisfy a presence check while breaking every consumer, which
    is the same failure seen from the other direction.
    """
    return value is None or (isinstance(value, str) and value.strip() == "")


_PLACEHOLDER = re.compile(r"<[^<>]+>")


def _is_placeholder(value) -> bool:
    """True when a value is still the bundle template's `<...>` token.

    A leftover `<string>` satisfies both the presence and the blank check, so
    without this an unfilled template line reaches a consumer as real data —
    the prod-manifest publish once recorded `<string>` as the schema.
    """
    return isinstance(value, str) and _PLACEHOLDER.fullmatch(value.strip()) is not None


def require_indirection_key(config: dict, key: str) -> dict:
    """Return the GitHub Actions name a `*_VAR` indirection key holds, or an error naming the key.

    Pure and deliberately not part of `parse_ci_config`'s validation: that stays permissive about
    `*_VAR` keys. Callers with no safe default name (the MotherDuck cleanup) use
    this to fail closed instead of falling back to a literal secret name.
    """
    value = config.get(key)
    if not isinstance(value, str) or _is_blank(value):
        return {"ok": False, "value": None, "error": f".workflow/ci-config.yml must set {key} to a GitHub Actions secret or variable name"}
    return {"ok": True, "value": value.strip(), "error": None}


# duckdb-quack is handled below with a specific migration message before this check.
# The accepted values are owned by platform_enum so every consumer that branches
# on platform reads the same set.
_VALID_PLATFORMS = VALID_PLATFORMS


def _translate_config_keys(config: dict) -> dict:
    """Rename recognized VD_DOMAIN_* keys to the legacy lowercase names every
    existing consumer expects. All non-coordinate indirection values pass through
    unchanged so repository owners control their committed mapping."""
    return {_KEY_ALIASES.get(k, k): v for k, v in config.items()}


def validate_intent_slug(branch_name: str) -> tuple[bool, str | None]:
    """Return (valid, branch_name_if_valid_else_None)."""
    if INTENT_SLUG_RE.match(branch_name):
        return True, branch_name
    return False, None


def parse_ci_config(yaml_str: str) -> dict:
    """Parse ci-config.yml content.

    Returns:
        {
            "ok": bool,
            "config": dict,
            "error": str | None,
            "line_number": int | None,
            "missing_keys": list[str],
            "warnings": list[str],   # unknown keys; present once the keys are validated
        }
    A retired key fails, naming it and any replacement.
    """
    if yaml is None:
        return {"ok": False, "config": {}, "error": "pyyaml not installed", "line_number": None, "missing_keys": []}

    try:
        config = yaml.safe_load(yaml_str)
    except yaml.YAMLError as exc:
        line_number = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line_number = exc.problem_mark.line + 1
        return {"ok": False, "config": {}, "error": str(exc), "line_number": line_number, "missing_keys": []}

    if config is None:
        config = {}

    if not isinstance(config, dict):
        return {
            "ok": False,
            "config": {},
            "error": "ci-config.yml must be a YAML mapping (key: value pairs), not a list or scalar",
            "line_number": None,
            "missing_keys": [],
        }

    platform = config.get("VD_DOMAIN_DATA_PLATFORM")

    if platform == "duckdb-quack":
        return {
            "ok": False,
            "config": _translate_config_keys(config),
            "error": "platform: duckdb-quack is no longer supported. Use platform: motherduck instead.",
            "line_number": None,
            "missing_keys": [],
        }

    if platform is not None and platform not in _VALID_PLATFORMS:
        return {
            "ok": False,
            "config": _translate_config_keys(config),
            "error": f"Unknown platform: {platform!r}. Valid values: {', '.join(PLATFORMS)}.",
            "line_number": None,
            "missing_keys": [],
        }

    # platform is None (VD_DOMAIN_DATA_PLATFORM omitted) is the one deliberate
    # exception that still defaults to Fabric's keys, matching prior behavior.
    # Any non-None, recognized-but-unmapped platform raises KeyError rather than
    # silently inheriting Fabric's required keys.
    required_keys = _REQUIRED_KEYS_BY_PLATFORM[FABRIC_LAKEHOUSE if platform is None else platform]
    absent = [k for k in required_keys if k not in config]
    blank = [k for k in required_keys if k in config and _is_blank(config[k])]
    # Every key, not only required ones: an optional line left as `<string>`
    # is just as wrong as a required one.
    placeholder = [k for k, v in config.items() if _is_placeholder(v)]
    findings = validate_config(config, _KNOWN_KEYS, _CONFIG_KEYS["retired_keys"])
    retired = [f["message"] for f in findings if f["severity"] == "error"]
    warnings = [f["message"] for f in findings if f["severity"] == "warning"]

    if absent or blank or placeholder or retired:
        parts = []
        if absent:
            parts.append(f"Missing required keys: {absent}")
        if blank:
            parts.append(f"Required keys present but empty: {blank}")
        if placeholder:
            parts.append(f"Template placeholders left unfilled: {placeholder}")
        if retired:
            parts.append(f"Retired keys: {'; '.join(retired)}")
        return {
            "ok": False,
            "config": _translate_config_keys(config),
            "error": ". ".join(parts),
            "line_number": None,
            "missing_keys": absent + blank + placeholder,
            "warnings": warnings,
        }

    return {"ok": True, "config": _translate_config_keys(config), "error": None, "line_number": None, "missing_keys": [],
            "warnings": warnings}


def resolve_prod_manifest_mode(yaml_str: str) -> str:
    """Return VD_DOMAIN_PROD_MANIFEST_SOURCE.mode, or "artifact" when it can't be read.

    Lenient by design: Gate 1 must still pick a prod-state source when the
    config is absent or malformed — preflight is the gate that rejects it.
    """
    try:
        c = yaml.safe_load(yaml_str) or {}
        return (c.get("VD_DOMAIN_PROD_MANIFEST_SOURCE") or {}).get("mode", "artifact")
    except Exception:
        return "artifact"
