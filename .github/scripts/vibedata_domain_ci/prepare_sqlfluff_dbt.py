"""Point sqlfluff's dbt templater at the Gate 0 compile profile.

Reads PROJ, SQLFLUFF_DBT_PROFILE, SQLFLUFF_DBT_TARGET and (optionally) SQLFLUFF_DBT_PROFILES_DIR
from the environment
and merges the dbt-templater coordinates into ./.sqlfluff (and $PROJ/.sqlfluff
when it exists).
"""

import configparser
import io
import os


def merge_sqlfluff_config(
    existing_text: str, profile: str, target: str, project_dir: str, profiles_dir: str = ".github/profiles",
) -> str:
    """Return existing_text with templater = dbt and the four dbt coordinates Gate 0 owns."""
    config = configparser.RawConfigParser()
    config.read_string(existing_text)
    existing_sqlfluff = dict(config["sqlfluff"]) if config.has_section("sqlfluff") else {}
    config["sqlfluff"] = {**existing_sqlfluff, "templater": "dbt"}
    # Preserve any other dbt-templater key the domain repo set (e.g.
    # dbt_skip_compilation, templater context vars) — override only the
    # four coordinates Gate 0 owns.
    existing_dbt = (
        dict(config["sqlfluff:templater:dbt"])
        if config.has_section("sqlfluff:templater:dbt")
        else {}
    )
    config["sqlfluff:templater:dbt"] = {
        **existing_dbt,
        "profiles_dir": profiles_dir,
        "profile": profile,
        "target": target,
        "project_dir": project_dir,
    }
    out = io.StringIO()
    config.write(out)
    return out.getvalue()


def main() -> None:
    proj = os.environ["PROJ"]
    # sqlfluff's dbt templater loads the dbt project once, in
    # sequence_files(), from the lint's ROOT config (./.sqlfluff) only;
    # a nearer $PROJ/.sqlfluff is applied per file, after that. So the
    # root file must carry these keys. An existing $PROJ/.sqlfluff gets
    # them too, so it can't override the root templater.
    sqlfluff_paths = [".sqlfluff"]
    proj_sqlfluff = os.path.join(proj, ".sqlfluff")
    if proj != "." and os.path.exists(proj_sqlfluff):
        sqlfluff_paths.append(proj_sqlfluff)
    for sqlfluff_path in sqlfluff_paths:
        # A missing file merges from empty — never clobbers an existing
        # domain-repo .sqlfluff's other settings.
        try:
            with open(sqlfluff_path) as f:
                existing = f.read()
        except FileNotFoundError:
            existing = ""
        merged = merge_sqlfluff_config(
            existing, os.environ["SQLFLUFF_DBT_PROFILE"], os.environ["SQLFLUFF_DBT_TARGET"], proj,
            os.environ.get("SQLFLUFF_DBT_PROFILES_DIR", ".github/profiles"),
        )
        with open(sqlfluff_path, "w") as f:
            f.write(merged)


if __name__ == "__main__":
    main()
