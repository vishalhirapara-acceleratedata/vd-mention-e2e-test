"""Run the canonical logical-model, ontology and data-contract checks over a Domain on every PR.

The Domain's canonical semantic model (Apache Ossie plus VIBEDATA extensions, one or more
`*.ossie.yaml` documents in `logical-model/`) and its ontology (an Ossie ontology document and
its mapping documents in `ontology/`) are checked by the `vibedata-ossie` package, run as
`python -m vibedata.ossie` (installed under the managed constraints). Each command runs to completion
so one failure does not hide another:

    validate        <logical-model>                    when the logical-model directory exists
    coverage        <logical-model> <models>
    suppliers       <logical-model>
    notes           <logical-model> <tests> <adr>
    generate-tests  <logical-model> <tests> --check
    validate        <ontology>                         when the ontology directory exists
    glossary        <ontology> <context>
    refs            <ontology> <logical-model>         when both exist

The Domain's data contracts (`{schema}/{table}.odcs.yaml` under `contracts/`) are checked by the
`vibedata-odcs` package, run as `python -m vibedata.odcs`:

    validate        <contracts>                        when the contracts directory exists
    diff            origin/<base> --contracts <contracts>   when GITHUB_BASE_REF names the base

`validate` is the per-file half of `vibedata.odcs check` (schema, empty sections, grain, name). The
map-dependent half (the `boundary/*` and `map/*` checks, orphans, `generated/stale`, `contract/undescribed`,
`servers`) needs `repo-map.json` and so runs in the plugin's `verifying` step, not here: CI never sees
`generated/stale`, so a Domain not yet regenerated is not failed by CI during the upgrade window. `diff` fails a
contract whose version bump is smaller than its change; it needs the base's history, so the job
checks out with `fetch-depth: 0`.

`coverage` searches the dbt models root recursively, unless the workflow passes `--repo-map <file>`:
then it takes the `path` of every boundary-role layer as a root and each `semantic_exclusions[].relation`
as `--exclude`. A layer's role is its `role` when valid, else `input` for `built_by: dlt`,
`boundary` for a name in `gold_layers`, and `internal` otherwise; only `boundary` layers are roots. A map
with no boundary layer and no exclusion leaves `coverage` on the models root, so a legacy Domain behaves
as before. `glossary` prints an object rather than a
diagnostics list and fails only on an entity type with no glossary term. Ontology mappings are
declaration-only: these checks prove shape, references, identity rules and glossary agreement,
never behaviour.

Paths come from the optional `logical_model:` block of CI's own `.workflow/ci-config.yml`
(`model_dir`, `models_dir`, `tests_dir`, `adr_dir`, `ontology_dir`, `context_file`,
`contracts_dir`); each key left out takes its default: `logical-model`, the dbt project's `models`
and `tests`, `docs/adr`, `ontology`, `CONTEXT.md` and `contracts`. The gate reads no Domain-owned
file such as `repo-map.json` unless the workflow names it with `--repo-map`. A missing directory skips its checks with a visible notice; a Domain
with none of the three passes.

`resolve_paths`, `plan_gates` and `render_summary` are pure; `main` gathers, calls them, runs the commands and
reports.

Neither tool takes a path by default. `--tooling` and `--contract-tooling` are optional overrides naming a
script to run in place of the package module, so a Domain repo that still has its old deployed
`.github/semantic-model/` and `.github/data-contract/` copies can point at them until it migrates.

CLI:
    logical_model_gate.py [--repo-root .] [--repo-map repo-map.json]
                          [--tooling <ossie.py>] [--contract-tooling <odcs.py>]

Exit 0 when every command exits 0 or the Domain has none of the directories, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

import yaml

DEFAULT_LOGICAL_MODEL = "logical-model"
DEFAULT_ADR = os.path.join("docs", "adr")
DEFAULT_ONTOLOGY = "ontology"
DEFAULT_CONTEXT = "CONTEXT.md"
DEFAULT_CONTRACTS = "contracts"
OSSIE_MODULE = "vibedata.ossie"
ODCS_MODULE = "vibedata.odcs"
CI_CONFIG = os.path.join(".workflow", "ci-config.yml")


@dataclass(frozen=True)
class BoundaryInputs:
    roots: list[str]       # the path of each boundary-role layer: ossie.py coverage's roots
    exclusions: list[str]  # each semantic_exclusions[].relation: ossie.py coverage's --exclude
    notes: list[str]       # entries the gate could not read; each is reported, none fails


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class Plan:
    commands: list[list[str]]  # ossie.py
    skipped: list[str]
    contract_commands: list[list[str]] = field(default_factory=list)  # odcs.py


def tool_argv(module: str, script: str | None, command: list[str]) -> list[str]:
    """The argv that runs a tool `command`: the package module (`python -m <module>`), or `script` when an
    override names one."""
    return [sys.executable, script, *command] if script else [sys.executable, "-m", module, *command]


def _join(base: str, *rest: str) -> str:
    return "/".join(p.strip("/") for p in (base, *rest) if p.strip("/") not in ("", "."))


def resolve_paths(config: dict | None, project_dir: str) -> dict[str, str]:
    """The logical-model, dbt models, singular-tests, ADR, ontology and contracts directories and
    the glossary file the checks read.

    `config` is ci-config.yml's `logical_model:` block (None when absent); a blank or
    non-string key takes its default. `project_dir` is the dbt project directory found on disk.
    """
    declared = {k: _join(v.strip()) for k, v in (config or {}).items() if isinstance(v, str) and _join(v.strip())}
    return {
        "logical_model": declared.get("model_dir", DEFAULT_LOGICAL_MODEL),
        "models": declared.get("models_dir", _join(project_dir, "models")),
        "tests": declared.get("tests_dir", _join(project_dir, "tests")),
        "adr": declared.get("adr_dir", DEFAULT_ADR),
        "ontology": declared.get("ontology_dir", DEFAULT_ONTOLOGY),
        "context": declared.get("context_file", DEFAULT_CONTEXT),
        "contracts": declared.get("contracts_dir", DEFAULT_CONTRACTS),
    }


LAYER_ROLES = ("input", "internal", "boundary")


def _layer_role(repo_map: dict, name: str, layer: dict) -> str:
    """`layers.<name>.role` when valid, else derived: dlt-built is input, a `gold_layers` name is boundary."""
    if layer.get("role") in LAYER_ROLES:
        return layer["role"]
    if layer.get("built_by") == "dlt":
        return "input"
    gold = repo_map.get("gold_layers")
    return "boundary" if isinstance(gold, list) and name in gold else "internal"


def boundary_inputs(repo_map: dict | None) -> BoundaryInputs:
    """The coverage roots and exclusions a repo map declares; empty for a legacy Domain."""
    if not isinstance(repo_map, dict):
        return BoundaryInputs([], [], [])
    roots, exclusions, notes = [], [], []
    layers = repo_map.get("layers")
    if layers is not None and not isinstance(layers, dict):
        notes.append("repo-map.json `layers` is not a mapping; no boundary coverage roots")
        layers = {}
    for name, layer in (layers or {}).items():
        layer = layer if isinstance(layer, dict) else {}
        if _layer_role(repo_map, name, layer) != "boundary":
            continue
        path = layer.get("path")
        parts = path.strip().split("/") if isinstance(path, str) else []
        if not isinstance(path, str) or path.startswith("/") or ".." in parts or not _join(path.strip()):
            notes.append(f"layer `{name}` is a boundary but has no usable repo-relative `path`; not a coverage root")
            continue
        if _join(path.strip()) not in roots:
            roots.append(_join(path.strip()))
    entries = repo_map.get("semantic_exclusions")
    if entries is not None and not isinstance(entries, list):
        notes.append("repo-map.json `semantic_exclusions` is not a list; nothing excluded")
        entries = []
    for entry in entries or []:
        relation = entry.get("relation") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if (isinstance(relation, str) and relation.count(".") == 1 and all(relation.split("."))
                and isinstance(reason, str) and reason.strip()):
            if relation not in exclusions:
                exclusions.append(relation)
        else:
            notes.append(f"semantic_exclusions entry {entry!r} needs a `relation` as `schema.table` and a `reason`; not excluded")
    return BoundaryInputs(roots, exclusions, notes)


def plan_gates(paths: dict[str, str], logical_model_exists: bool, ontology_exists: bool,
               contracts_exists: bool = False, base_branch: str | None = None,
               boundary_roots: list[str] | None = None, exclusions: list[str] | None = None) -> Skip | Plan:
    """The ossie.py and odcs.py commands to run over `paths` (from `resolve_paths`) and the checks
    skipped, or why none run. `base_branch` is the PR's base (GITHUB_BASE_REF); without it the
    contract `diff` is skipped. `coverage` takes `boundary_roots` (default: the models root) and
    one `--exclude` per entry of `exclusions`."""
    model, ontology, contracts = paths["logical_model"], paths["ontology"], paths["contracts"]
    if not logical_model_exists and not ontology_exists and not contracts_exists:
        return Skip(f"no {model}/, no {ontology}/ and no {contracts}/")
    commands, skipped = [], []
    if logical_model_exists:
        commands += [
            ["validate", model],
            ["coverage", model, *(boundary_roots or [paths["models"]]),
             *[arg for relation in exclusions or [] for arg in ("--exclude", relation)]],
            ["suppliers", model],
            ["notes", model, paths["tests"], paths["adr"]],
            ["generate-tests", model, paths["tests"], "--check"],
        ]
    else:
        skipped.append(f"Logical-model checks skipped: no {model}/")
    if ontology_exists:
        commands += [["validate", ontology], ["glossary", ontology, paths["context"]]]
        if logical_model_exists:
            commands.append(["refs", ontology, model])
        else:
            skipped.append(f"`refs` skipped: no {model}/")
    else:
        skipped.append(f"Ontology checks skipped: no {ontology}/")
    contract_commands = []
    if not contracts_exists:
        skipped.append(f"Data-contract checks skipped: no {contracts}/")
    else:
        contract_commands.append(["validate", contracts])
        if base_branch:
            contract_commands.append(["diff", f"origin/{base_branch}", "--contracts", contracts])
        else:
            skipped.append("`odcs.py diff` skipped: no base branch (GITHUB_BASE_REF is unset)")
    return Plan(commands, skipped, contract_commands)


def _findings(stdout: str) -> list[str]:
    """The failure lines in a command's JSON stdout: a diagnostics list's errors, or the
    entity types without a term from `glossary`'s object. Anything else yields none."""
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, dict):
        untermed = parsed.get("entity_types_without_term")
        return [f"entity type without a glossary term: {t}" for t in untermed] if isinstance(untermed, list) else []
    if not isinstance(parsed, list):
        return []
    diags = [d for d in parsed if isinstance(d, dict) and d.get("severity") not in ("warning", "info")]
    return [f"{d.get('code', d.get('severity', 'error'))}: {d.get('message', '')}" for d in diags]


def render_summary(results: list[tuple[list[str], int, str, str]], skipped: list[str] | None = None) -> tuple[bool, str]:
    """(passed, markdown) for each command's (argv, exit code, stdout, stderr) and each check
    the plan skipped. An odcs.py command's argv starts with `odcs.py`, so its row names the tool."""
    lines = ["## Logical model", "", "| Check | Result |", "|---|---|"]
    details = []
    for argv, code, stdout, stderr in results:
        verdict = "pass" if code == 0 else f"fail (exit {code})"
        lines.append(f"| `{' '.join(argv)}` | {verdict} |")
        if code == 0:
            continue
        findings = _findings(stdout)
        details.append(f"### `{' '.join(argv)}`")
        if findings:
            details += [f"- {f}" for f in findings]
        else:
            tail = (stderr or stdout).strip().splitlines()[-5:]
            details += ["```", *tail, "```"]
    passed = all(code == 0 for _, code, _, _ in results)
    notes = [""] + [f"- {s}" for s in skipped] if skipped else []
    return passed, "\n".join(lines + notes + ([""] + details if details else [])) + "\n"


def _read_logical_model_config(repo_root: str) -> dict | None:
    """ci-config.yml's `logical_model:` block. Absent or unreadable reads as None; ci/preflight,
    not this gate, rejects a malformed ci-config.yml."""
    try:
        with open(os.path.join(repo_root, CI_CONFIG), encoding="utf-8") as f:
            block = (yaml.safe_load(f) or {}).get("logical_model")
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    if block is not None and not isinstance(block, dict):
        print(f"::warning::{CI_CONFIG}: logical_model must be a mapping; using the default paths", flush=True)
        return None
    return block


def _read_repo_map(root: str, path: str) -> dict | None:
    """The repo map the workflow named. Missing or unparseable warns and reads as None, so the
    legacy models-root coverage runs; the plugin's `check` reports a malformed map."""
    try:
        with open(os.path.join(root, path), encoding="utf-8") as f:
            parsed = json.load(f)
    except (OSError, ValueError):
        print(f"::warning::{path}: not readable as JSON; coverage stays on the models root and excludes nothing", flush=True)
        return None
    if not isinstance(parsed, dict):
        print(f"::warning::{path}: not a JSON object; coverage stays on the models root and excludes nothing", flush=True)
        return None
    return parsed


def _write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--tooling", default=None, help=f"an ossie.py script to run instead of `python -m {OSSIE_MODULE}`")
    ap.add_argument("--contract-tooling", default=None, help=f"an odcs.py script to run instead of `python -m {ODCS_MODULE}`")
    ap.add_argument("--repo-map", default=None,
                    help="repo-map.json, relative to the repo root: its boundary layers become coverage roots and "
                         "its semantic_exclusions become --exclude; absent, coverage reads the models root only")
    args = ap.parse_args(argv)
    root = args.repo_root

    project = "transformation" if os.path.isfile(os.path.join(root, "transformation", "dbt_project.yml")) else "."
    paths = resolve_paths(_read_logical_model_config(root), project)
    boundary = boundary_inputs(_read_repo_map(root, args.repo_map) if args.repo_map else None)
    plan = plan_gates(paths, os.path.isdir(os.path.join(root, paths["logical_model"])),
                      os.path.isdir(os.path.join(root, paths["ontology"])),
                      os.path.isdir(os.path.join(root, paths["contracts"])),
                      os.environ.get("GITHUB_BASE_REF") or None,
                      boundary.roots, boundary.exclusions)
    if isinstance(plan, Skip):
        message = f"Logical model, ontology and data-contract checks skipped: {plan.reason}"
        print(f"::notice::{message}", flush=True)
        _write_step_summary(f"## Logical model\n\n{message}\n")
        return 0

    plan_notes = [*boundary.notes, *plan.skipped]
    for skipped in plan_notes:
        print(f"::notice::{skipped}", flush=True)
    results = []
    runs = ([(tool_argv(OSSIE_MODULE, args.tooling, c), "ossie.py", c, c) for c in plan.commands]
            + [(tool_argv(ODCS_MODULE, args.contract_tooling, c), "odcs.py", c, ["odcs.py", *c]) for c in plan.contract_commands])
    for argv, name, command, label in runs:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True)
        print(f"$ {name} {' '.join(command)}  -> exit {proc.returncode}", flush=True)
        print(proc.stdout, end="", flush=True)
        print(proc.stderr, end="", file=sys.stderr, flush=True)
        results.append((label, proc.returncode, proc.stdout, proc.stderr))
    passed, summary = render_summary(results, plan_notes)
    print(summary, flush=True)
    _write_step_summary(summary)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
