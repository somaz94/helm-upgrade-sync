"""Shared helmfile-flavored helpers for upgrade_core template modules.

Extracted once a third helmfile-using template (``external-oci``) confirmed
that it, ``external-standard`` and ``external-with-image-tag`` share the
same Chart.yaml / helmfile.yaml parsing surface and the same
chart-flavored backup list + rollback semantics. ``ansible-github-release``
intentionally does NOT use this module — its backup list / rollback are
ansible-flavored and its yaml parsing has different quote-preserving
semantics.

Exported helpers — all stdlib-only:

- :func:`read_yaml_field` — value of a top-level ``field:`` line.
- :func:`detect_helmfile` — `(path, name)` for `helmfile.yaml[.gotmpl]`.
- :func:`print_helmfile_releases` — pretty-print of `releases:` block.
- :func:`extract_top_keys` — top-level keys in a values YAML file.
- :func:`used_top_level_keys` — top-level keys actually present (skipping
  comments / indented lines).
- :func:`run_subprocess` — thin wrapper enforcing text mode + capture +
  ``check=False``.
- :func:`helm` — ``helm <args>`` via :func:`run_subprocess`.
- :func:`diff` — ``diff left right`` stdout.
- :func:`update_helmfile_pins` — bash sed-based 4 expression replacement.
- :func:`list_backups` — chart-flavored backup listing (reads `Chart.yaml`
  from each backup dir).
- :func:`record_mirror_rewrote` — marks a backup whose values files the
  image-mirror step had already rewritten.
- :func:`restore_backup_files` — the file-copy half of a rollback, shared
  with the argocd-pin rollback.
- :func:`do_rollback` — chart-flavored rollback (prompt, then
  :func:`restore_backup_files`).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from ._common import (
    backup_file_names,
    print_backup_list,
    prompt_select_backup,
    sorted_backups,
)


def read_yaml_field(path: Path, field: str) -> str:
    """Return the value of a top-level ``field:`` line, or empty string."""
    if not path.is_file():
        return ""
    prefix = f"{field}:"
    with path.open() as f:
        for line in f:
            if line.startswith(prefix):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    return parts[1].strip()
                return ""
    return ""


def detect_helmfile(chart_dir: Path) -> tuple[Path | None, str]:
    """Return (path, name) for the chart's helmfile, or (None, "") if absent.

    Prefers ``helmfile.yaml.gotmpl`` over ``helmfile.yaml`` when both exist.
    """
    gotmpl = chart_dir / "helmfile.yaml.gotmpl"
    if gotmpl.is_file():
        return gotmpl, "helmfile.yaml.gotmpl"
    plain = chart_dir / "helmfile.yaml"
    if plain.is_file():
        return plain, "helmfile.yaml"
    return None, ""


def print_helmfile_releases(helmfile_path: Path) -> None:
    """Mimic the bash awk pretty-print of releases under helmfile.yaml."""
    in_releases = False
    name = ""
    with helmfile_path.open() as f:
        for line in f:
            if not in_releases:
                if line.startswith("releases:"):
                    in_releases = True
                continue
            if "#" in line:
                continue
            tokens = line.split()
            # `- name: foo`  -> ["-", "name:", "foo"]
            if "- name:" in line and len(tokens) >= 3:
                name = tokens[2]
            elif "version:" in line and name and len(tokens) >= 2:
                idx = tokens.index("version:") if "version:" in tokens else -1
                if idx >= 0 and idx + 1 < len(tokens):
                    ver = tokens[idx + 1]
                    print(f"    - {name:<30} version: {ver}")
                    name = ""


def extract_top_keys(path: Path) -> set[str]:
    """Return top-level keys (line starts with a letter, before first ':')."""
    keys: set[str] = set()
    if not path.is_file():
        return keys
    with path.open() as f:
        for line in f:
            if not line:
                continue
            ch = line[0]
            if ch.isascii() and ch.isalpha():
                head = line.split(":", 1)[0]
                keys.add(head)
    return keys


def used_top_level_keys(path: Path) -> set[str]:
    """Return the set of top-level keys actually present in a user values file."""
    used: set[str] = set()
    if not path.is_file():
        return used
    with path.open() as f:
        for line in f:
            if not line or line[0].isspace() or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            head = line.split(":", 1)[0].strip()
            if head:
                used.add(head)
    return used


def run_subprocess(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper enforcing text mode + capture + check=False.

    Bash parity: failures are tolerated (caller inspects ``returncode``
    and/or empty stdout) — never raises.
    """
    return subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        **kwargs,
    )


def helm(*args: str) -> subprocess.CompletedProcess:
    return run_subprocess(["helm", *args])


def diff(left: Path, right: Path) -> str:
    """Return the stdout of ``diff left right`` (empty string when files match)."""
    return run_subprocess(["diff", str(left), str(right)]).stdout


def update_helmfile_pins(
    helmfile_path: Path, current_version: str, latest_version: str
) -> int:
    """Replace chart version pins in helmfile. Returns the count of matched pins.

    Mirrors the bash ``grep -cE ... | sed -E ...`` flow: three pin forms for the
    plain helmfile (`version: X.Y.Z`, `version: "X.Y.Z"`) and one gotmpl hoist
    (`{{- $chartVersion := "X.Y.Z" }}``).
    """
    content = helmfile_path.read_text()
    cv = re.escape(current_version)

    # Count: matches either `version: X.Y.Z` / `version: "X.Y.Z"` followed by
    # whitespace-or-EOL, OR the gotmpl `$chartVersion := "X.Y.Z"` form.
    count_re = re.compile(
        rf'(version:[ \t]+"?{cv}"?(?:[ \t]|$)|\$chartVersion[ \t]*:=[ \t]+"{cv}")',
        re.MULTILINE,
    )
    pins = len(count_re.findall(content))

    # 4 substitutions, in the same order as the bash sed -e chain. The replacement
    # uses \g<N> form so a numeric suffix in latest_version (e.g. "1.1.0") doesn't
    # collide with backreference parsing.
    content = re.sub(
        rf'(version:[ \t]+)"{cv}"',
        rf'\g<1>"{latest_version}"',
        content,
    )
    content = re.sub(
        rf'(version:[ \t]+){cv}([ \t]+)',
        rf'\g<1>{latest_version}\g<2>',
        content,
    )
    content = re.sub(
        rf'(version:[ \t]+){cv}$',
        rf'\g<1>{latest_version}',
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        rf'(\$chartVersion[ \t]*:=[ \t]+)"{cv}"',
        rf'\g<1>"{latest_version}"',
        content,
    )

    helmfile_path.write_text(content)
    return pins


_LITERAL_PIN_RE = re.compile(
    r'^[ \t]+version:[ \t]+"?\d|\$chartVersion[ \t]*:=[ \t]+"\d', re.MULTILINE
)


def align_kept_helmfile_pin(chart_dir: Path, have: str, target: str) -> None:
    """Move the chart pin of a helmfile kept on disk from ``have`` to ``target``.

    For rollbacks that never copy a helmfile out of the backup (ArgoCD delivers
    the component, and an old copy can hold hooks removed since): the kept file's
    pin follows the chart the way an upgrade's pin rewrite does. A literal pin
    already off ``have`` is only reported, since nothing says which one is right.
    """
    have, target = have.strip("\"'"), target.strip("\"'")
    if not have or not target or have == target:
        return
    for name in ("helmfile.yaml.gotmpl", "helmfile.yaml"):
        path = chart_dir / name
        if not path.is_file():
            continue
        if update_helmfile_pins(path, have, target):
            print(f"  Updated {name} chart pin {have} -> {target}")
        elif _LITERAL_PIN_RE.search(path.read_text()):
            print(
                f"  WARNING: {name} pins a chart version other than {have}; set it "
                f"to {target} by hand if it tracks this chart."
            )


def kept_helmfile_literal_pin(chart_dir: Path) -> str:
    """Name of a helmfile on disk that pins a literal chart version, or ""."""
    for name in ("helmfile.yaml.gotmpl", "helmfile.yaml"):
        path = chart_dir / name
        if path.is_file() and _LITERAL_PIN_RE.search(path.read_text()):
            return name
    return ""


def list_backups(backup_dir: Path) -> None:
    """Chart-flavored backup listing — reads Chart.yaml.version for each entry."""

    def describe(d: Path) -> str:
        chart_yaml = d / "Chart.yaml"
        chart_ver = "unknown"
        if chart_yaml.is_file():
            chart_ver = read_yaml_field(chart_yaml, "version") or "unknown"
        return f"(Chart: {chart_ver}) — {backup_file_names(d)}"

    print_backup_list(backup_dir, describe)


# Not *.yaml: a rollback treats every top-level *.yaml of a backup as a values file.
MIRROR_REWROTE_FILE = "mirror-rewrote-values"


def record_mirror_rewrote(backup_target: Path, names: list[str]) -> None:
    """Record which backed-up values files the image-mirror step had already rewritten."""
    if not names:
        return
    (backup_target / MIRROR_REWROTE_FILE).write_text("".join(f"{n}\n" for n in names))
    print(
        f"  Note: the mirror step already set the new image tag in "
        f"{', '.join(f'values/{n}' for n in names)}; a rollback keeps it"
    )


def restore_backup_files(
    selected: Path,
    chart_dir: Path,
    values_dir: Path,
    *,
    restore_helmfile: bool = True,
) -> None:
    """Copy one backup's Chart.yaml, values.yaml, helmfile and values/*.yaml back.

    ``values.schema.json`` follows Chart.yaml when the component keeps a schema
    mirror. With ``restore_helmfile=False`` only the values files the component
    still has come back (one deleted since the backup was deliberate; an upgrade
    never removes one); a helmfile rollback restores the whole snapshot, because
    the old helmfile may reference files deleted since.

    ``restore_helmfile=False`` is for components ArgoCD delivers: their helmfile
    is retired or a bootstrap recipe the upgrade never rewrites, so the backed-up
    copy can only resurrect a retired file or undo later edits to the recipe.
    A backed-up helmfile whose name differs from the one the component uses now
    (a ``.yaml`` -> ``.gotmpl`` switch since) is skipped with a pin warning.

    Values files the image-mirror step rewrote before the backup was taken
    (``MIRROR_REWROTE_FILE``) come back with the upgraded image tag, so the
    image is not rolled back; a warning names them.
    """
    src = selected / "Chart.yaml"
    if src.is_file():
        shutil.copy2(src, chart_dir / "Chart.yaml")
        print("  Restored Chart.yaml")

    src = selected / "values.yaml"
    if src.is_file():
        shutil.copy2(src, chart_dir / "values.yaml")
        print("  Restored values.yaml")

    src = selected / "values.schema.json"
    if src.is_file() and (chart_dir / "values.schema.json").is_file():
        shutil.copy2(src, chart_dir / "values.schema.json")
        print("  Restored values.schema.json")

    _, current_helmfile = detect_helmfile(chart_dir)
    for name in ("helmfile.yaml.gotmpl", "helmfile.yaml"):
        if not (selected / name).is_file():
            continue
        if not restore_helmfile:
            print(f"  Skipped {name} (ArgoCD delivers this component; it is not the deploy path)")
        elif current_helmfile and name != current_helmfile:
            # Copied back, it would sit beside the current one, and helmfile refuses to run with both.
            target = read_yaml_field(selected / "Chart.yaml", "version").strip("\"'")
            print(
                f"  WARNING: skipped {name} (the component now uses {current_helmfile}); "
                f"its chart pin was NOT rolled back — set it to "
                f"{target or 'the backed-up chart version'} by hand."
            )
        else:
            shutil.copy2(selected / name, chart_dir / name)
            print(f"  Restored {name}")
        break

    restored: set[str] = set()
    for entry in sorted(selected.glob("*.yaml")):
        name = entry.name
        if name in ("Chart.yaml", "values.yaml", "helmfile.yaml"):
            continue
        if not restore_helmfile and not (values_dir / name).is_file():
            print(f"  Skipped values/{name} (no longer in values/; if renamed, roll the new file back by hand)")
            continue
        shutil.copy2(entry, values_dir / name)
        restored.add(name)
        print(f"  Restored values/{name}")

    rewrote = selected / MIRROR_REWROTE_FILE
    kept_tags = [n for n in rewrote.read_text().split() if n in restored] if rewrote.is_file() else []
    if kept_tags:
        names = ", ".join(f"values/{n}" for n in kept_tags)
        # Decided 2026-09-30: the backup stays after the mirror step; a database image rarely downgrades in place.
        print(
            f"  WARNING: the image was NOT rolled back — the mirror step had already set the new "
            f"tag in {names} when this backup was taken. If the image can downgrade in place, "
            f"take the previous tag from `git log -p -- values/`."
        )


def do_rollback(backup_dir: Path, chart_dir: Path, values_dir: Path) -> None:
    """Chart-flavored rollback for helmfile-using templates.

    Prompts for a backup and restores it with :func:`restore_backup_files`,
    which says what it skips.
    """
    backups = sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        sys.exit(1)

    list_backups(backup_dir)

    selected = prompt_select_backup(backups)
    print()
    print(f"Restoring from backup/{selected.name}...")
    restore_backup_files(selected, chart_dir, values_dir)

    print()
    print("Rollback complete! Run 'helmfile diff' to verify.")
