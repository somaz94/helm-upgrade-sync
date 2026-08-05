"""Local Helm chart with templates upgrade runner (local-with-templates template).

Drives the upgrade flow for **local** charts — components that keep
``Chart.yaml`` / ``values.yaml`` / ``templates/`` directly in the repo
and need three local-specific behaviors when bumping the upstream
version:

  - ``CUSTOM_TEMPLATES`` are preserved across the upstream sync
    (e.g. ``pv.yaml`` / ``pvc.yaml`` that do not exist upstream).
  - ``_pod.tpl`` carries a PVC volume block patch that must be
    re-applied after the upstream sync replaces the template.
  - ``EXTRA_DIRS`` (``ci/`` / ``dashboards/``) are mirrored from
    upstream alongside the templates.

Two chart download modes — set in CONFIG:

  - helm repo (default): ``helm pull --untar`` after ``helm search repo``.
  - git source: ``git clone --depth 1 --branch v$VERSION`` when
    ``CHART_GIT_REPO`` is non-empty. Used for charts that aren't
    published to any helm repo.

This module is **independent** of :mod:`external_standard` because the
bash template's flow (helm pull → local templates/ replace → custom
template preserve → ``_pod.tpl`` patch → extra dirs sync) does not map
to the external-standard 7-step body via hooks — the chart download
mechanic and the template-tree replace stage are template-specific.
Helper modules :mod:`_common` (backup helpers + SEPARATOR) and
:mod:`_common_helmfile` (Chart.yaml field reader / helmfile detection /
helmfile pin rewrite / subprocess wrappers) are reused.

Migrated from the canonical bash template at
``templates/local-with-templates.sh`` as part of
the shell -> python migration.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from ._common import (
    DOUBLE_SEP,
    SEPARATOR,
    auto_prune_backups as _auto_prune_backups,
    cleanup_backups as _cleanup_backups,
    is_excluded as _is_excluded,
    now_timestamp,
    prompt_select_backup as _prompt_select_backup,
    read_keep_backups_env,
    sorted_backups as _sorted_backups,
)
from ._common_helmfile import (
    detect_helmfile as _detect_helmfile,
    diff as _diff,
    extract_top_keys as _extract_top_keys,
    helm as _helm,
    print_helmfile_releases as _print_helmfile_releases,
    read_yaml_field as _read_yaml_field,
    run_subprocess as _run,
    update_helmfile_pins as _update_helmfile_pins,
    used_top_level_keys as _used_top_level_keys,
)


# Upstream "extra dirs" that get mirrored alongside templates/. Kept as a
# module-level constant — the bash template hard-codes ("ci" "dashboards").
EXTRA_DIRS: tuple[str, ...] = ("ci", "dashboards")

# Tag-line semver pattern (``v?X.Y.Z`` form). Used by ``_fetch_latest_git``
# to pick the newest semver-shaped tag from ``git ls-remote`` output.
_TAG_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


# -----------------------------------------------
# Entry-point
# -----------------------------------------------

def run(config: dict, argv: list[str], script_path: str | os.PathLike) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Returns the process exit code (0 success, non-zero failure).

    CONFIG keys consumed:
      - ``SCRIPT_NAME`` — header label.
      - ``HELM_REPO_NAME`` / ``HELM_REPO_URL`` / ``HELM_CHART`` — helm
        repo + chart reference (default mode).
      - ``CHART_GIT_REPO`` / ``CHART_GIT_PATH`` — git source mode (when
        non-empty, helm repo lookup is skipped).
      - ``CHANGELOG_URL`` — completion banner link.
      - ``CUSTOM_TEMPLATES`` — list of filenames in templates/ that
        must survive the upstream sync (e.g. ``["pv.yaml", "pvc.yaml"]``).
      - ``CUSTOM_POD_PATCH`` — multi-line string patch inserted into
        ``_pod.tpl`` before the ``extraVolumes`` block.
    """
    script = Path(script_path).resolve()
    chart_dir = script.parent
    backup_dir = chart_dir / "backup"
    values_dir = chart_dir / "values"
    templates_dir = chart_dir / "templates"
    timestamp = now_timestamp()
    keep_backups = read_keep_backups_env()

    helmfile_path, helmfile_name = _detect_helmfile(chart_dir)
    prog = script.name

    args = _parse_args(
        argv, prog, keep_backups,
        backup_dir, chart_dir, values_dir, templates_dir,
    )
    if args is None:
        return 0

    return _main_flow(
        config=config,
        chart_dir=chart_dir,
        backup_dir=backup_dir,
        values_dir=values_dir,
        templates_dir=templates_dir,
        timestamp=timestamp,
        keep_backups=keep_backups,
        helmfile_path=helmfile_path,
        helmfile_name=helmfile_name,
        dry_run=args["dry_run"],
        target_version=args["target_version"],
        exclude_patterns=args["exclude_patterns"],
    )


# -----------------------------------------------
# Argument parsing (8 CLI flags — ``external_standard`` baseline + --list-backups uses
# local backup format with template/value counts)
# -----------------------------------------------

def _parse_args(
    argv: list[str],
    prog: str,
    keep_backups: int,
    backup_dir: Path,
    chart_dir: Path,
    values_dir: Path,
    templates_dir: Path,
) -> dict | None:
    dry_run = False
    target_version = ""
    exclude_patterns = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            _usage(prog, keep_backups)
            sys.exit(0)
        elif arg == "--list-backups":
            _list_backups(backup_dir)
            sys.exit(0)
        elif arg == "--rollback":
            _do_rollback(backup_dir, chart_dir, values_dir, templates_dir)
            sys.exit(0)
        elif arg == "--cleanup-backups":
            _cleanup_backups(backup_dir, keep_backups)
            sys.exit(0)
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg == "--exclude":
            exclude_patterns = argv[i + 1] if i + 1 < len(argv) else ""
            if not exclude_patterns:
                print(
                    "ERROR: --exclude requires a pattern "
                    "(e.g., --exclude old-release,test)"
                )
                sys.exit(1)
            i += 2
        elif arg == "--version":
            target_version = argv[i + 1] if i + 1 < len(argv) else ""
            if not target_version:
                print("ERROR: --version requires a version number")
                sys.exit(1)
            i += 2
        else:
            print(f"Unknown option: {arg}")
            print()
            _usage(prog, keep_backups)
            sys.exit(0)

    return {
        "dry_run": dry_run,
        "target_version": target_version,
        "exclude_patterns": exclude_patterns,
    }


def _usage(prog: str, keep_backups: int) -> None:
    print(f"""Usage: {prog} [COMMAND] [OPTIONS]

Checks for new versions, backs up current files (including templates),
downloads the upstream chart, and applies the upgrade while preserving
custom templates (pv.yaml, pvc.yaml) and _pod.tpl patches.

Commands:
  (default)           Check latest version and upgrade
  --version <VER>     Upgrade to a specific chart version
  --exclude <PATTERN> Exclude values files whose name contains PATTERN (substring match,
                      comma-separated; also skipped from backup copy)
  --dry-run           Preview changes only (no files will be modified)
  --rollback          Restore from a previous backup
  --list-backups      List available backups
  --cleanup-backups   Keep only the last {keep_backups} backups, remove older ones
  -h, --help          Show this help message

Examples:
  {prog}                                # Upgrade to latest
  {prog} --dry-run                      # Preview upgrade without changes
  {prog} --version 0.49.0               # Upgrade to specific version
  {prog} --exclude old-release,test     # Skip files with 'old-release' or 'test' in name
  {prog} --dry-run --version 0.49.0     # Combine flags
  {prog} --rollback                     # Restore from backup
  {prog} --list-backups                 # Show available backups
  {prog} --cleanup-backups              # Remove old backups (keep last {keep_backups})""")


# -----------------------------------------------
# list_backups / do_rollback — local-specific (template/value counts +
# templates/ + EXTRA_DIRS restore). External_standard's helpers cannot
# be reused here because the ``local_with_templates`` backup tree includes additional dirs.
# -----------------------------------------------

def _list_backups(backup_dir: Path) -> None:
    print("Available backups:")
    print()
    backups = _sorted_backups(backup_dir)
    if not backups:
        print("  No backups found.")
        return

    for idx, backup_path in enumerate(backups, start=1):
        chart_ver = "unknown"
        chart_yaml = backup_path / "Chart.yaml"
        if chart_yaml.is_file():
            chart_ver = _read_yaml_field(chart_yaml, "version") or "unknown"

        tpl_count = 0
        templates_subdir = backup_path / "templates"
        if templates_subdir.is_dir():
            tpl_count = sum(1 for _ in templates_subdir.iterdir())

        val_count = 0
        for entry in backup_path.iterdir():
            if (
                entry.is_file()
                and entry.suffix == ".yaml"
                and entry.name not in {"Chart.yaml", "helmfile.yaml"}
            ):
                val_count += 1

        print(
            f"  [{idx}] {backup_path.name} (Chart: {chart_ver}) — "
            f"templates: {tpl_count}, values: {val_count}"
        )
    print()


def _do_rollback(
    backup_dir: Path,
    chart_dir: Path,
    values_dir: Path,
    templates_dir: Path,
) -> None:
    backups = _sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        sys.exit(1)

    _list_backups(backup_dir)

    selected = _prompt_select_backup(backups)
    print()
    print(f"Restoring from backup/{selected.name}...")

    chart_yaml = selected / "Chart.yaml"
    if chart_yaml.is_file():
        shutil.copy2(chart_yaml, chart_dir / "Chart.yaml")
        print("  Restored Chart.yaml")

    values_yaml = selected / "values.yaml"
    if values_yaml.is_file():
        shutil.copy2(values_yaml, chart_dir / "values.yaml")
        print("  Restored values.yaml")

    helmfile_gotmpl = selected / "helmfile.yaml.gotmpl"
    helmfile_plain = selected / "helmfile.yaml"
    if helmfile_gotmpl.is_file():
        shutil.copy2(helmfile_gotmpl, chart_dir / "helmfile.yaml.gotmpl")
        print("  Restored helmfile.yaml.gotmpl")
    elif helmfile_plain.is_file():
        shutil.copy2(helmfile_plain, chart_dir / "helmfile.yaml")
        print("  Restored helmfile.yaml")

    backup_templates = selected / "templates"
    if backup_templates.is_dir():
        if templates_dir.exists():
            shutil.rmtree(templates_dir)
        shutil.copytree(backup_templates, templates_dir)
        file_count = sum(1 for _ in templates_dir.iterdir())
        print(f"  Restored templates/ ({file_count} files)")

    for edir in EXTRA_DIRS:
        backup_extra = selected / edir
        target_extra = chart_dir / edir
        if backup_extra.is_dir():
            if target_extra.exists():
                shutil.rmtree(target_extra)
            shutil.copytree(backup_extra, target_extra)
            print(f"  Restored {edir}/")
        else:
            print(f"  Skipped {edir}/ (not in this backup)")

    for entry in selected.iterdir():
        if not entry.is_file() or entry.suffix != ".yaml":
            continue
        if entry.name in {"Chart.yaml", "values.yaml", "helmfile.yaml"}:
            continue
        shutil.copy2(entry, values_dir / entry.name)
        print(f"  Restored values/{entry.name}")

    print()
    print("Rollback complete! Run 'helmfile diff' to verify.")


# -----------------------------------------------
# patch_pod_tpl — re-apply PVC volume block to upstream _pod.tpl
# -----------------------------------------------

# Marker line that precedes the PVC patch. Matched as a substring on
# the trimmed line so the original whitespace is preserved.
_POD_TPL_MARKER = "if .Values.extraVolumes"


def _patch_pod_tpl(pod_tpl: Path, custom_pod_patch: str) -> int:
    """Inject ``custom_pod_patch`` before the ``extraVolumes`` block.

    Returns 0 on success / already-patched, 1 when the marker line
    cannot be found. Mirrors the bash ``patch_pod_tpl`` byte-for-byte
    (prints + early-exit on existing patch + WARNING on missing marker).
    """
    text = pod_tpl.read_text()
    if "persistentVolumeClaims.enabled" in text:
        print("  _pod.tpl: PVC patch already present, skipping")
        return 0

    if _POD_TPL_MARKER not in text:
        print("  WARNING: Could not find extraVolumes marker in _pod.tpl")
        print("  Manual patching may be required for PVC volume support")
        return 1

    new_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if _POD_TPL_MARKER in line:
            # Bash uses `echo "$CUSTOM_POD_PATCH"` which trims trailing
            # blanks but emits a final newline. Match that.
            new_lines.append(custom_pod_patch.rstrip("\n") + "\n")
        new_lines.append(line)
    pod_tpl.write_text("".join(new_lines))

    print("  _pod.tpl: PVC patch applied successfully")
    return 0


# -----------------------------------------------
# Step 2 — fetch latest (helm search OR git ls-remote)
# -----------------------------------------------

def _fetch_latest_helm(config: dict) -> tuple[str, str]:
    """Default mode — helm search repo. Returns (version, app_version)."""
    _helm("repo", "add", config["HELM_REPO_NAME"], config["HELM_REPO_URL"])
    _helm("repo", "update")

    search = _helm("search", "repo", config["HELM_CHART"], "--output", "json")
    try:
        data = json.loads(search.stdout or "[]")
        if isinstance(data, list) and data:
            return (
                data[0].get("version", "") or "",
                data[0].get("app_version", "") or "",
            )
    except (json.JSONDecodeError, ValueError):
        # Bash parity: ``jq -r '.[0].version'`` returns empty on parse
        # error; we mirror by falling through to the empty-result path.
        pass
    return "", ""


def _fetch_latest_git(chart_git_repo: str) -> str:
    """Git source mode — `git ls-remote --tags --sort='-v:refname'`.

    Returns the newest semver-shaped tag with the optional `v` prefix
    stripped. Empty on failure.
    """
    result = _run(
        ["git", "ls-remote", "--tags", "--refs",
         "--sort=-v:refname", chart_git_repo],
    )
    if result.returncode != 0:
        return ""
    for raw in (result.stdout or "").splitlines():
        # `<sha>\trefs/tags/<tag>` format.
        parts = raw.split("\t", 1)
        if len(parts) != 2:
            continue
        tag = parts[1].removeprefix("refs/tags/").strip()
        if _TAG_SEMVER_RE.match(tag):
            return tag.lstrip("v")
    return ""


# -----------------------------------------------
# Step 3 — download upstream chart (helm pull OR git clone)
# -----------------------------------------------

def _download_chart_helm(
    helm_chart: str, version: str, temp_dir: Path
) -> Path | None:
    """``helm pull --untar`` into ``temp_dir`` and return the unpacked dir.

    Bash parity: the original used ``helm pull ... 2>/dev/null`` and then
    checked ``[ ! -d "$UPSTREAM_DIR/templates" ]`` to detect failure. We
    mirror that contract — the helm rc is intentionally not inspected
    here; the caller validates by checking that the returned dir has a
    ``templates/`` subdir.
    """
    _helm(
        "pull", helm_chart,
        "--version", version,
        "--untar",
        "--untardir", str(temp_dir),
    )
    for entry in sorted(temp_dir.iterdir()):
        if entry.is_dir():
            return entry
    return None


def _download_chart_git(
    chart_git_repo: str, chart_git_path: str, version: str, temp_dir: Path
) -> Path | None:
    """``git clone --depth 1 --branch`` into ``temp_dir/git-src``.

    Tries ``v$VERSION`` first, falls back to bare ``$VERSION``.
    """
    git_tag = f"v{version}"
    tag_check = _run(
        ["git", "ls-remote", "--tags", "--refs", chart_git_repo, git_tag],
    )
    if tag_check.returncode != 0 or not (tag_check.stdout or "").strip():
        git_tag = version

    clone_target = temp_dir / "git-src"
    clone = _run(
        ["git", "clone", "--depth", "1", "--branch", git_tag,
         chart_git_repo, str(clone_target)],
    )
    if clone.returncode != 0:
        return None
    if chart_git_path:
        return clone_target / chart_git_path
    return clone_target


# -----------------------------------------------
# Step 5 — template scan (modified / new / removed + tests/ subdir)
# -----------------------------------------------

def _diff_templates(
    templates_dir: Path,
    upstream_dir: Path,
    custom_templates: set[str],
) -> tuple[int, int, int]:
    """Return (changed, added, removed) counts and print classification."""
    changed = added = removed = 0

    upstream_templates = upstream_dir / "templates"

    # Local templates classified against upstream.
    if templates_dir.is_dir():
        for local_tpl in sorted(templates_dir.iterdir()):
            if not local_tpl.is_file():
                continue
            if local_tpl.suffix not in (".yaml", ".tpl", ".txt"):
                continue
            name = local_tpl.name
            if name in custom_templates:
                continue
            upstream_tpl = upstream_templates / name
            if upstream_tpl.is_file():
                if not _files_identical(local_tpl, upstream_tpl):
                    print(f"    MODIFIED: templates/{name}")
                    changed += 1
            else:
                print(f"    REMOVED:  templates/{name} (not in upstream)")
                removed += 1

    # New templates introduced upstream.
    if upstream_templates.is_dir():
        for upstream_tpl in sorted(upstream_templates.iterdir()):
            if not upstream_tpl.is_file():
                continue
            if upstream_tpl.suffix not in (".yaml", ".tpl", ".txt"):
                continue
            name = upstream_tpl.name
            local_tpl = templates_dir / name
            if not local_tpl.is_file():
                print(f"    NEW:      templates/{name}")
                added += 1

    # tests/ subdir handled separately (bash treats it specially).
    upstream_tests = upstream_templates / "tests"
    if upstream_tests.is_dir():
        local_tests = templates_dir / "tests"
        for upstream_tpl in sorted(upstream_tests.iterdir()):
            if not upstream_tpl.is_file() or upstream_tpl.suffix != ".yaml":
                continue
            name = upstream_tpl.name
            local_tpl = local_tests / name
            if local_tpl.is_file():
                if not _files_identical(local_tpl, upstream_tpl):
                    print(f"    MODIFIED: templates/tests/{name}")
                    changed += 1
            else:
                print(f"    NEW:      templates/tests/{name}")
                added += 1

    return changed, added, removed


def _files_identical(a: Path, b: Path) -> bool:
    """``diff -q`` equivalent — byte-for-byte comparison."""
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


# -----------------------------------------------
# Main 8-step flow
# -----------------------------------------------

def _main_flow(
    *,
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    values_dir: Path,
    templates_dir: Path,
    timestamp: str,
    keep_backups: int,
    helmfile_path: Path | None,
    helmfile_name: str,
    dry_run: bool,
    target_version: str,
    exclude_patterns: str,
) -> int:
    print(DOUBLE_SEP)
    print(f" {config['SCRIPT_NAME']}")
    if dry_run:
        print(" Mode: DRY-RUN (no files will be changed)")
    if target_version:
        print(f" Target: v{target_version}")
    if exclude_patterns:
        print(f" Exclude: {exclude_patterns}")
    print(DOUBLE_SEP)

    # Step 1
    print()
    print("[Step 1/8] Checking current version...")
    chart_yaml = chart_dir / "Chart.yaml"
    current_version = _read_yaml_field(chart_yaml, "version")
    current_app_version = _read_yaml_field(chart_yaml, "appVersion")
    print(
        f"  Installed - Chart: {current_version} / App: {current_app_version}"
    )

    if helmfile_path is not None:
        print()
        print(f"  Helmfile releases ({helmfile_name}):")
        _print_helmfile_releases(helmfile_path)

    # Step 2
    print()
    print("[Step 2/8] Checking latest version...")
    chart_git_repo = config.get("CHART_GIT_REPO", "") or ""

    if chart_git_repo:
        latest_version_found = _fetch_latest_git(chart_git_repo)
        latest_app_version = "(from-git)"
        if not latest_version_found:
            print(f"  ERROR: Failed to list tags from {chart_git_repo}")
            return 1
    else:
        latest_version_found, latest_app_version = _fetch_latest_helm(config)
        if not latest_version_found:
            print("  ERROR: Failed to fetch latest version.")
            print(
                f"  Try: helm repo add {config['HELM_REPO_NAME']} "
                f"{config['HELM_REPO_URL']} && helm repo update"
            )
            return 1

    if target_version:
        print(
            f"  Latest available - Chart: {latest_version_found} / "
            f"App: {latest_app_version}"
        )
        latest_version = target_version
        print(f"  Using target     - Chart: {target_version}")
    else:
        latest_version = latest_version_found
        print(
            f"  Latest    - Chart: {latest_version} / "
            f"App: {latest_app_version}"
        )

    if current_version == latest_version:
        print()
        print("  Already up to date! Nothing to do.")
        return 0

    print()
    print(f"  Upgrade: {current_version} -> {latest_version}")
    print(f"  Changelog: {config['CHANGELOG_URL']}")

    # Steps 3-8 share a tempdir for fetched chart files.
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir = Path(tmp)
        return _apply_upgrade(
            config=config,
            chart_dir=chart_dir,
            backup_dir=backup_dir,
            values_dir=values_dir,
            templates_dir=templates_dir,
            temp_dir=temp_dir,
            timestamp=timestamp,
            keep_backups=keep_backups,
            helmfile_path=helmfile_path,
            helmfile_name=helmfile_name,
            dry_run=dry_run,
            target_version=target_version,
            exclude_patterns=exclude_patterns,
            current_version=current_version,
            latest_version=latest_version,
        )


def _apply_upgrade(
    *,
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    values_dir: Path,
    templates_dir: Path,
    temp_dir: Path,
    timestamp: str,
    keep_backups: int,
    helmfile_path: Path | None,
    helmfile_name: str,
    dry_run: bool,
    target_version: str,
    exclude_patterns: str,
    current_version: str,
    latest_version: str,
) -> int:
    # Step 3
    print()
    print(
        f"[Step 3/8] Downloading upstream chart v{latest_version}..."
    )
    chart_git_repo = config.get("CHART_GIT_REPO", "") or ""
    chart_git_path = config.get("CHART_GIT_PATH", "") or ""
    if chart_git_repo:
        upstream_dir = _download_chart_git(
            chart_git_repo, chart_git_path, latest_version, temp_dir
        )
    else:
        upstream_dir = _download_chart_helm(
            config["HELM_CHART"], latest_version, temp_dir
        )

    if upstream_dir is None or not (upstream_dir / "templates").is_dir():
        print(
            f"  ERROR: Failed to download chart for version {latest_version}"
        )
        return 1

    upstream_chart_yaml = upstream_dir / "Chart.yaml"
    latest_app_version = _read_yaml_field(upstream_chart_yaml, "appVersion")
    upstream_tpl_count = sum(
        1 for _ in (upstream_dir / "templates").iterdir()
        if _.is_file()
    )
    print(
        f"  Downloaded successfully (App: {latest_app_version}, "
        f"Templates: {upstream_tpl_count} files)"
    )

    # Step 4
    print()
    print("[Step 4/8] Chart.yaml diff (current vs target)...")
    print(SEPARATOR)
    sys.stdout.write(_diff(chart_dir / "Chart.yaml", upstream_chart_yaml))
    print(SEPARATOR)

    # Step 5
    print()
    print("[Step 5/8] values.yaml diff (current vs target)...")
    local_values = chart_dir / "values.yaml"
    upstream_values = upstream_dir / "values.yaml"
    diff_text = _diff(local_values, upstream_values)
    diff_lines = diff_text.count("\n")
    print(f"  Total diff lines: {diff_lines} (showing first 80)")
    print(SEPARATOR)
    for line in diff_text.splitlines()[:80]:
        print(line)
    print(SEPARATOR)

    print()
    print("  Template changes:")
    custom_templates = set(config.get("CUSTOM_TEMPLATES") or [])
    changed, added, removed = _diff_templates(
        templates_dir, upstream_dir, custom_templates,
    )
    print(f"  Summary: {changed} modified, {added} new, {removed} removed")
    custom_list = " ".join(config.get("CUSTOM_TEMPLATES") or [])
    print(f"  Custom templates preserved: {custom_list}")

    # Step 6 — _pod.tpl patch check
    print()
    print("[Step 6/8] Custom _pod.tpl patch check...")
    custom_pod_patch = config.get("CUSTOM_POD_PATCH") or ""
    upstream_pod_tpl = upstream_dir / "templates" / "_pod.tpl"
    if not custom_pod_patch or not upstream_pod_tpl.is_file():
        print(
            "  Skipped (this chart does not use _pod.tpl patching)."
        )
    elif "persistentVolumeClaims.enabled" in upstream_pod_tpl.read_text():
        print(
            "  Upstream _pod.tpl already includes PVC support! "
            "No patching needed."
        )
    else:
        print("  Upstream _pod.tpl does NOT include PVC support.")
        print("  Will inject PVC volume block after upgrade.")
        print()
        print("  Patch to apply:")
        print("  ------------------------------------------------")
        for line in custom_pod_patch.splitlines():
            print(f"  {line}")
        print("  ------------------------------------------------")

    # Step 7 — breaking changes scan (same as ``external_standard`` Step 6 logic, but the
    # baseline comparison uses local values.yaml vs upstream values.yaml).
    print()
    print(
        "[Step 7/8] Checking custom values for breaking changes..."
    )
    if exclude_patterns:
        print(f"  Excluding patterns: {exclude_patterns}")

    old_keys = _extract_top_keys(local_values)
    new_keys = _extract_top_keys(upstream_values)
    removed_keys = sorted(old_keys - new_keys)
    added_keys = sorted(new_keys - old_keys)

    if values_dir.is_dir():
        files = sorted(values_dir.glob("*.yaml"))
    else:
        files = []

    for values_file in files:
        if not values_file.is_file():
            continue
        filename = values_file.name
        if _is_excluded(filename, exclude_patterns):
            print()
            print(f"=== values/{filename} === (SKIPPED)")
            continue

        print()
        print(f"=== values/{filename} ===")

        if removed_keys:
            print("  !!  Removed top-level keys in target values.yaml:")
            used_in_file = _used_top_level_keys(values_file)
            for key in removed_keys:
                if key in used_in_file:
                    print(f"    - {key}  <-- USED in your {filename}!")
                else:
                    print(f"    - {key}")

        if added_keys:
            print("  ++  New top-level keys in target values.yaml:")
            for key in added_keys:
                print(f"    - {key}")

        if not removed_keys and not added_keys:
            print("  OK  No breaking top-level key changes detected")

    # Step 8 — apply (or dry-run exit)
    print()
    if dry_run:
        print("[Step 8/8] DRY-RUN complete. No files were changed.")
        print()
        print("  To apply: ./upgrade.py")
        if target_version:
            print(f"  To apply: ./upgrade.py --version {target_version}")
        return 0

    print("[Step 8/8] Applying upgrade...")

    backup_target = backup_dir / timestamp
    (backup_target / "templates").mkdir(parents=True, exist_ok=True)

    shutil.copy2(chart_dir / "Chart.yaml", backup_target / "Chart.yaml")
    shutil.copy2(local_values, backup_target / "values.yaml")

    local_schema = chart_dir / "values.schema.json"
    if local_schema.is_file():
        shutil.copy2(local_schema, backup_target / "values.schema.json")

    if helmfile_path is not None and helmfile_path.is_file():
        shutil.copy2(helmfile_path, backup_target / helmfile_name)

    # Backup all templates (including subdirectories).
    if templates_dir.is_dir():
        for entry in templates_dir.iterdir():
            target = backup_target / "templates" / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)

    # Backup extra dirs (ci, dashboards, ...).
    for edir in EXTRA_DIRS:
        src = chart_dir / edir
        if src.is_dir():
            shutil.copytree(src, backup_target / edir, dirs_exist_ok=True)

    # Backup custom values (matches ``external_standard``: skip Chart/values/helmfile names).
    if values_dir.is_dir():
        for values_file in sorted(values_dir.glob("*.yaml")):
            if not values_file.is_file():
                continue
            if _is_excluded(values_file.name, exclude_patterns):
                continue
            shutil.copy2(values_file, backup_target / values_file.name)

    print(f"  Backed up to: backup/{timestamp}/")
    print("    - Chart.yaml, values.yaml")
    backup_templates_dir = backup_target / "templates"
    backup_tpl_count = sum(
        1 for _ in backup_templates_dir.rglob("*") if _.is_file()
    )
    print(f"    - templates/ ({backup_tpl_count} files)")
    for edir in EXTRA_DIRS:
        if (backup_target / edir).is_dir():
            print(f"    - {edir}/")

    # Save custom templates to temp.
    custom_template_list = list(config.get("CUSTOM_TEMPLATES") or [])
    custom_saved: dict[str, Path] = {}
    for ct in custom_template_list:
        local_ct = templates_dir / ct
        if local_ct.is_file():
            stash = temp_dir / f"custom_{ct}"
            shutil.copy2(local_ct, stash)
            custom_saved[ct] = stash

    # Replace templates with upstream (rm + cp -r).
    if templates_dir.exists():
        shutil.rmtree(templates_dir)
    shutil.copytree(upstream_dir / "templates", templates_dir)
    new_tpl_count = sum(
        1 for _ in templates_dir.rglob("*") if _.is_file()
    )
    print()
    print(
        f"  Replaced templates/ with upstream ({new_tpl_count} files)"
    )

    # Restore custom templates.
    for ct, stash in custom_saved.items():
        shutil.copy2(stash, templates_dir / ct)
        print(f"  Preserved custom: templates/{ct}")

    # Patch _pod.tpl when applicable.
    pod_tpl = templates_dir / "_pod.tpl"
    if not custom_pod_patch or not pod_tpl.is_file():
        print(
            "  _pod.tpl: skipped (this chart does not use _pod.tpl patching)"
        )
    elif "persistentVolumeClaims.enabled" not in pod_tpl.read_text():
        _patch_pod_tpl(pod_tpl, custom_pod_patch)
    else:
        print("  _pod.tpl: PVC support already in upstream, no patch needed")

    # Sync extra dirs (ci/, dashboards/).
    for edir in EXTRA_DIRS:
        upstream_edir = upstream_dir / edir
        if upstream_edir.is_dir():
            target_edir = chart_dir / edir
            if target_edir.exists():
                shutil.rmtree(target_edir)
            shutil.copytree(upstream_edir, target_edir)
            print(f"  Updated {edir}/")

    # Update Chart.yaml and values.yaml.
    shutil.copy2(upstream_chart_yaml, chart_dir / "Chart.yaml")
    print()
    print(
        f"  Updated Chart.yaml ({current_version} -> {latest_version} "
        f"/ App: {latest_app_version})"
    )

    shutil.copy2(upstream_values, chart_dir / "values.yaml")
    print("  Updated values.yaml")

    upstream_schema = upstream_dir / "values.schema.json"
    if upstream_schema.is_file():
        shutil.copy2(upstream_schema, chart_dir / "values.schema.json")
        print("  Updated values.schema.json")

    # Helmfile pin rewrite.
    if helmfile_path is not None and helmfile_path.is_file():
        pins = _update_helmfile_pins(
            helmfile_path, current_version, latest_version
        )
        print(
            f"  Updated {helmfile_name} ({pins} pin(s): "
            f"{current_version} -> {latest_version})"
        )

    _auto_prune_backups(backup_dir, keep_backups)

    print()
    print(DOUBLE_SEP)
    print(f" Upgrade complete! ({current_version} -> {latest_version})")
    print()
    print(f" Changelog: {config['CHANGELOG_URL']}")
    print()
    print(" Custom templates preserved:")
    for ct in custom_template_list:
        print(f"   - templates/{ct}")
    print("   - templates/_pod.tpl (PVC patch)")
    print()
    print(" Next steps:")
    print("   1. Review values/ files for any needed changes")
    print("   2. Run: helmfile diff")
    print("   3. Run: helmfile apply")
    print()
    print(" To rollback:")
    print("   ./upgrade.py --rollback")
    print(DOUBLE_SEP)
    return 0
