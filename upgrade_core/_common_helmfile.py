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
- :func:`do_rollback` — chart-flavored rollback (restores Chart.yaml +
  values.yaml + helmfile.yaml[.gotmpl] + per-env values/*.yaml).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from ._common import prompt_select_backup, sorted_backups


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


def list_backups(backup_dir: Path) -> None:
    """Chart-flavored backup listing — reads Chart.yaml.version for each entry."""
    print("Available backups:")
    print()
    backups = sorted_backups(backup_dir)
    if not backups:
        print("  No backups found.")
        return
    for idx, d in enumerate(backups, start=1):
        chart_ver = "unknown"
        chart_yaml = d / "Chart.yaml"
        if chart_yaml.is_file():
            chart_ver = read_yaml_field(chart_yaml, "version") or "unknown"
        names = sorted(p.name for p in d.iterdir())
        files = ", ".join(names)
        print(f"  [{idx}] {d.name} (Chart: {chart_ver}) — {files}")
    print()


def do_rollback(backup_dir: Path, chart_dir: Path, values_dir: Path) -> None:
    """Chart-flavored rollback for helmfile-using templates.

    Restores Chart.yaml + values.yaml + helmfile.yaml[.gotmpl] + every
    other ``*.yaml`` in the selected backup into ``values_dir``.
    """
    backups = sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        sys.exit(1)

    list_backups(backup_dir)

    selected = prompt_select_backup(backups)
    print()
    print(f"Restoring from backup/{selected.name}...")

    src = selected / "Chart.yaml"
    if src.is_file():
        shutil.copy2(src, chart_dir / "Chart.yaml")
        print("  Restored Chart.yaml")

    src = selected / "values.yaml"
    if src.is_file():
        shutil.copy2(src, chart_dir / "values.yaml")
        print("  Restored values.yaml")

    helmfile_gotmpl = selected / "helmfile.yaml.gotmpl"
    helmfile_yaml = selected / "helmfile.yaml"
    if helmfile_gotmpl.is_file():
        shutil.copy2(helmfile_gotmpl, chart_dir / "helmfile.yaml.gotmpl")
        print("  Restored helmfile.yaml.gotmpl")
    elif helmfile_yaml.is_file():
        shutil.copy2(helmfile_yaml, chart_dir / "helmfile.yaml")
        print("  Restored helmfile.yaml")

    for entry in sorted(selected.glob("*.yaml")):
        name = entry.name
        if name in ("Chart.yaml", "values.yaml", "helmfile.yaml"):
            continue
        shutil.copy2(entry, values_dir / name)
        print(f"  Restored values/{name}")

    print()
    print("Rollback complete! Run 'helmfile diff' to verify.")
