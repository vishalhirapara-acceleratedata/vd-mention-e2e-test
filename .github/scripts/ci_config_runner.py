"""Thin shell: resolve values from ci-config.yml into runner outputs.

Subcommands:
    indirection-keys --key K [--key K ...] [--field NAME=CONFIG_KEY ...]
        Writes each `*_VAR` indirection key, lowercased, plus each named config
        field, to $GITHUB_OUTPUT. Any missing key fails the step before
        anything is written.
    prod-db-name
        Writes PROD_DB_NAME to $GITHUB_ENV, and PROD_SCHEMA_NAME when the
        platform's config carries a production schema.
    prod-manifest-mode
        Writes `mode` to $GITHUB_OUTPUT; never fails (defaults to artifact).

Every subcommand takes --config (default .workflow/ci-config.yml).
"""

import argparse
import sys

from vibedata_domain_ci import runner_io
from vibedata_domain_ci.ci_config import _is_blank, locate_ci_config, parse_ci_config, require_indirection_key, resolve_prod_manifest_mode


def _parsed_config(path: str) -> dict:
    with open(path) as f:
        result = parse_ci_config(f.read())
    if not result["ok"]:
        runner_io.error(f"ci-config.yml parse failed: {result['error']}")
        sys.exit(1)
    return result["config"]


def _indirection_keys(args) -> None:
    config = _parsed_config(args.config)
    outputs = {}
    for key in args.key:
        found = require_indirection_key(config, key)
        if not found["ok"]:
            runner_io.error(found["error"])
            sys.exit(1)
        outputs[key.lower()] = found["value"]
    for field in args.field:
        name, config_key = field.split("=", 1)
        outputs[name] = config[config_key]
    for name, value in outputs.items():
        runner_io.set_output(name, value)


def _prod_db_name(args) -> None:
    config = _parsed_config(args.config)
    runner_io.set_env("PROD_DB_NAME", config["prod_db_name"])
    # A blank line is unset, not a schema named "None" or "": the profile then uses its default.
    if not _is_blank(config.get("prod_schema_name")):
        runner_io.set_env("PROD_SCHEMA_NAME", config["prod_schema_name"])


def _prod_manifest_mode(args) -> None:
    try:
        with open(args.config) as f:
            text = f.read()
    except OSError:
        text = ""
    mode = resolve_prod_manifest_mode(text)
    runner_io.set_output("mode", mode)
    print(f"prod_manifest_source.mode={mode}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    keys = sub.add_parser("indirection-keys")
    keys.add_argument("--key", action="append", required=True)
    keys.add_argument("--field", action="append", default=[])
    keys.set_defaults(func=_indirection_keys)
    sub.add_parser("prod-db-name").set_defaults(func=_prod_db_name)
    sub.add_parser("prod-manifest-mode").set_defaults(func=_prod_manifest_mode)
    for p in sub.choices.values():
        p.add_argument("--config", default=locate_ci_config())
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
