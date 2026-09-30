"""External Helm chart upgrade runner (external-standard template).

Drives the upgrade flow shared by external Helm charts that use a helm repo
+ helmfile layout. Migrated from the canonical bash template at
``templates/external-standard.sh`` as part of the
the shell -> python migration.

The helmfile-flavored helpers (Chart.yaml /
helmfile.yaml parsing, subprocess wrappers, helmfile pin rewrite,
chart-flavored list / rollback) were extracted to
:mod:`_common_helmfile`, and three extension points were added to
:func:`run` so OCI / wrapper / tracked-chart variants can override
specific steps without forking the body:

  - ``fetch_latest_hook`` replaces Step 2's "helm search repo + parse"
    (used by ``external_oci`` external-oci which queries GitHub Releases instead).
  - ``chart_write_hook`` replaces Step 7's "cp Chart.yaml + values.yaml
    + values.schema.json" (used by ``external_oci`` wrapper-mode which patches only
    the ``version:`` line).
  - ``helmfile_pin_hook`` replaces the default
    :func:`_common_helmfile.update_helmfile_pins` call (used by ``external_oci``
    tracked-chart scope which limits the rewrite to one release block).
  - ``post_pin_hook`` still fires after the pin
    rewrite for image-tag-style follow-up steps.

Two more hooks plus ``total_steps`` were
added so the external-oci-with-mirror variant fits the same body:

  - ``values_summary_hook`` runs after the Step 1 helmfile releases
    print and surfaces per-values-file image.tag overrides. None falls
    back to the ``external_oci_with_mirror`` default (yq-based ``.image.tag`` per file).
  - ``pre_apply_hook`` runs as Step 7 (numbered ``[Step 7/total]``) and
    is reserved for pre-apply side effects like mirroring upstream
    images to a private registry. Non-zero return aborts the upgrade.
    Skipped in dry-run with a SKIPPED message.
  - ``total_steps`` defaults to 7; ``external_oci_with_mirror`` passes 8 so the "Apply" step
    moves to ``[Step 8/8]`` and the diagnostic header prints match
    byte-for-byte with the legacy bash template.

Public entry-point: ``run(config, argv, script_path, *, hooks...)``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from ._common import (
    SEPARATOR,
    auto_prune_backups as _auto_prune_backups,
    cleanup_backups as _cleanup_backups,
    is_excluded as _is_excluded,
    now_timestamp,
    parse_upgrade_argv as _parse_upgrade_argv,
    print_run_banner as _print_run_banner,
    print_upgrade_footer as _print_upgrade_footer,
    read_keep_backups_env,
    sorted_backups as _sorted_backups,
)
from ._common_helmfile import (
    detect_helmfile as _detect_helmfile,
    diff as _diff,
    do_rollback as _do_rollback,
    extract_top_keys as _extract_top_keys,
    helm as _helm,
    kept_helmfile_literal_pin as _kept_helmfile_literal_pin,
    list_backups as _list_backups,
    print_helmfile_releases as _print_helmfile_releases,
    read_yaml_field as _read_yaml_field,
    run_subprocess as _run,
    update_helmfile_pins as _update_helmfile_pins,
    used_top_level_keys as _used_top_level_keys,
)


# Hook signatures for templates that extend the base flow without
# forking the whole body. All hooks accept keyword-only arguments and
# default to ``None`` so the baseline and ``external_with_image_tag``
# behavior is preserved when no override is supplied.

# Step 2 — fetch latest version. Default = helm search repo + parse JSON.
# Returns ``(latest_version_found, latest_app_version_from_helm_search)``.
# ``latest_app_version`` from this hook is only used for display
# purposes during the "Latest available" / "Latest" header lines; the
# canonical appVersion read happens later from the freshly fetched
# Chart.yaml in Step 3.
FetchLatestHook = Callable[..., tuple[str, str]]

# Step 7 — write Chart.yaml + values.yaml + values.schema.json. Default
# behavior is a straight `cp` of the temp tree onto the chart dir.
# Hook returns the formatted print lines for the operator log (one per
# line of stdout). Hook MUST print its own messages — the default also
# prints, so the caller doesn't double-print.
ChartWriteHook = Callable[..., None]

# Step 7 — rewrite helmfile pin(s). Default = update_helmfile_pins (4
# sed expressions). Hook returns the count of updated pins (used in the
# log line).
HelmfilePinHook = Callable[..., int]

# Step 7 — post-pin extension (e.g. image-tag rewrite). Fires after the
# helmfile pin rewrite and before auto-prune.
PostPinHook = Callable[..., None]

# Step 1 — surface per-values-file overrides after the helmfile releases
# block. Default = ``external_oci_with_mirror``'s yq-based ``.image.tag`` per ``values/*.yaml``.
ValuesSummaryHook = Callable[..., None]

# Step 7 (``external_oci_with_mirror``) — pre-apply hook (e.g. mirror upstream images to a private
# registry). Returns 0 to continue with the Apply step, non-zero to abort
# the upgrade. Skipped in dry-run by the caller (hook does not see
# ``dry_run`` — the SKIPPED message is the caller's responsibility).
PreApplyHook = Callable[..., int]

# Apply step — version pin rewrite that REPLACES the helmfile pin step and
# fires regardless of helmfile presence. Used by the ``argocd-pin`` template
# to write ``chart.version`` into
# ``<component>/argocd/<release>.yaml`` — the migrated components no longer
# ship a helmfile, so the default ``helmfile_path is not None`` pin path
# never runs. Receives ``(chart_dir, current_version, latest_version,
# backup_target)`` and returns the count of pins/files rewritten. Default
# ``None`` preserves the helmfile pin path byte-for-byte for every
# non-migrated component.
PinWriteHook = Callable[..., int]

# ``--rollback`` — replaces the chart-flavored rollback. Used by the
# ``argocd-pin`` template, whose version pin lives in files the default
# rollback never restores. Receives ``(backup_dir, chart_dir, values_dir)``.
RollbackHook = Callable[..., None]

# ``--list-backups`` — replaces the chart-flavored listing, whose
# ``(Chart: <version>)`` column reads ``unknown`` for a component that keeps
# no Chart.yaml mirror. Receives ``(backup_dir)``.
ListBackupsHook = Callable[..., None]

# Step 1 — current-version probe fallback. Fires ONLY when the local
# Chart.yaml is absent or carries no ``version:`` field. Used by the
# ``argocd-pin`` template, whose version SSOT is the ArgoCD metadata file
# ``<component>/argocd[-aws]/<release>.yaml`` (chart.version), not the local
# Chart.yaml — which is a derived mirror some components deliberately do not
# ship. Without this fallback the probe returns "" and every downstream step
# degrades silently: the values diff compares latest against latest (helm
# reads ``--version ""`` as "no constraint"), so the Step 6 breaking-change
# scan reports no removed keys, and the pin rewrite matches 0 files while
# still reporting success. Receives ``(chart_dir)`` and returns the current
# version ("" when undeterminable). Default ``None`` keeps the Chart.yaml-only
# probe byte-for-byte for every other consumer.
CurrentVersionHook = Callable[..., str]


def run(
    config: dict,
    argv: list[str],
    script_path: str | os.PathLike,
    *,
    total_steps: int = 7,
    fetch_latest_hook: FetchLatestHook | None = None,
    chart_write_hook: ChartWriteHook | None = None,
    helmfile_pin_hook: HelmfilePinHook | None = None,
    post_pin_hook: PostPinHook | None = None,
    values_summary_hook: ValuesSummaryHook | None = None,
    pre_apply_hook: PreApplyHook | None = None,
    pin_write_hook: PinWriteHook | None = None,
    current_version_hook: CurrentVersionHook | None = None,
    rollback_hook: RollbackHook | None = None,
    list_backups_hook: ListBackupsHook | None = None,
    skip_missing_chart_mirror: bool = False,
) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Returns the process exit code (0 success, non-zero failure).

    Hook semantics — all default to ``None`` (use baseline behavior):
      - ``fetch_latest_hook`` — replaces Step 2.
      - ``chart_write_hook`` — replaces Step 7 (or Step 8 when
        ``total_steps=8``) chart write block.
      - ``helmfile_pin_hook`` — replaces helmfile pin rewrite.
      - ``post_pin_hook`` — runs after the pin rewrite.
      - ``values_summary_hook`` — runs after the Step 1 helmfile releases
        list. ``None`` falls back to a default that
        prints ``image.tag`` per ``values/*.yaml`` via yq.
      - ``pre_apply_hook`` — runs as a numbered step between the breaking-
        changes scan and the Apply step. Used only when
        ``total_steps=8``; ignored for ``total_steps=7``.
      - ``pin_write_hook`` — replaces the version pin write (argocd-pin
        pattern). When supplied it fires on apply regardless of helmfile
        presence and the helmfile pin path is skipped; ``None`` keeps the
        helmfile pin behavior unchanged.
      - ``current_version_hook`` — Step 1 fallback consulted only when the
        local Chart.yaml yields no ``version`` (argocd-pin pattern, where the
        ArgoCD metadata file is the version SSOT). ``None`` keeps the
        Chart.yaml-only probe.
      - ``rollback_hook`` — replaces ``--rollback`` (argocd-pin pattern).
        ``None`` keeps the chart-flavored rollback.
      - ``list_backups_hook`` — replaces ``--list-backups`` (argocd-pin
        pattern). ``None`` keeps the chart-flavored listing.

    ``skip_missing_chart_mirror`` (argocd-pin pattern) suppresses the local
    Chart.yaml / values.yaml / values.schema.json mirror write for components
    that ship no such mirror to begin with. Writing one would fabricate
    untracked files whose contents are already authoritative in the ArgoCD
    metadata pin. Only takes effect when the local Chart.yaml is absent, so a
    component that does keep a mirror still has it refreshed.

    ``total_steps`` defaults to 7 to preserve the four baseline templates byte-for-byte
    output. ``external_oci_with_mirror`` passes 8 so the Apply step renumbers to ``[Step 8/8]``.
    """

    script = Path(script_path).resolve()
    chart_dir = script.parent
    backup_dir = chart_dir / "backup"
    values_dir = chart_dir / "values"
    timestamp = now_timestamp()
    keep_backups = read_keep_backups_env()

    helmfile_path, helmfile_name = _detect_helmfile(chart_dir)
    prog = script.name

    args = _parse_args(
        argv, prog, keep_backups, backup_dir, chart_dir, values_dir,
        rollback_hook, list_backups_hook,
    )
    if args is None:
        return 0

    return _main_flow(
        config=config,
        chart_dir=chart_dir,
        backup_dir=backup_dir,
        values_dir=values_dir,
        timestamp=timestamp,
        keep_backups=keep_backups,
        helmfile_path=helmfile_path,
        helmfile_name=helmfile_name,
        dry_run=args["dry_run"],
        target_version=args["target_version"],
        exclude_patterns=args["exclude_patterns"],
        total_steps=total_steps,
        fetch_latest_hook=fetch_latest_hook,
        chart_write_hook=chart_write_hook,
        helmfile_pin_hook=helmfile_pin_hook,
        post_pin_hook=post_pin_hook,
        values_summary_hook=values_summary_hook,
        pre_apply_hook=pre_apply_hook,
        pin_write_hook=pin_write_hook,
        current_version_hook=current_version_hook,
        skip_missing_chart_mirror=skip_missing_chart_mirror,
    )


# -----------------------------------------------
# Argument parsing (manual — preserves bash byte-for-byte messages)
# -----------------------------------------------

def _parse_args(
    argv: list[str],
    prog: str,
    keep_backups: int,
    backup_dir: Path,
    chart_dir: Path,
    values_dir: Path,
    rollback_hook: RollbackHook | None = None,
    list_backups_hook: ListBackupsHook | None = None,
) -> dict | None:
    """Parse CLI args. Returns dict for the main flow, or None for sub-commands."""

    def rollback() -> None:
        if rollback_hook is None:
            _do_rollback(backup_dir, chart_dir, values_dir)
        else:
            rollback_hook(
                backup_dir=backup_dir, chart_dir=chart_dir, values_dir=values_dir
            )

    return _parse_upgrade_argv(
        argv,
        usage=lambda: _usage(prog, keep_backups),
        list_backups=lambda: (
            _list_backups(backup_dir)
            if list_backups_hook is None
            else list_backups_hook(backup_dir=backup_dir)
        ),
        rollback=rollback,
        cleanup_backups=lambda: _cleanup_backups(backup_dir, keep_backups),
    )


def _usage(prog: str, keep_backups: int) -> None:
    print(f"""Usage: {prog} [COMMAND] [OPTIONS]

Checks for new versions, backs up current files, and applies the upgrade.

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
  {prog} --version 1.0.0                # Upgrade to specific version
  {prog} --exclude old-release,test     # Skip files with 'old-release' or 'test' in name
  {prog} --dry-run --version 1.0.0      # Combine flags
  {prog} --rollback                     # Restore from backup
  {prog} --list-backups                 # Show available backups
  {prog} --cleanup-backups              # Remove old backups (keep last {keep_backups})""")


# -----------------------------------------------
# Default Step 2 — helm search repo + parse JSON
# -----------------------------------------------

def _default_fetch_latest(*, config: dict) -> tuple[str, str]:
    """Baseline Step 2 for external-standard / external-with-image-tag.

    Returns (latest_version_found, latest_app_version). Empty strings
    when the search returns nothing or fails — caller emits the bash
    error message.
    """
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
        pass
    return "", ""


# -----------------------------------------------
# Main 7-step flow
# -----------------------------------------------

def _main_flow(
    *,
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    values_dir: Path,
    timestamp: str,
    keep_backups: int,
    helmfile_path: Path | None,
    helmfile_name: str,
    dry_run: bool,
    target_version: str,
    exclude_patterns: str,
    total_steps: int = 7,
    fetch_latest_hook: FetchLatestHook | None = None,
    chart_write_hook: ChartWriteHook | None = None,
    helmfile_pin_hook: HelmfilePinHook | None = None,
    post_pin_hook: PostPinHook | None = None,
    values_summary_hook: ValuesSummaryHook | None = None,
    pre_apply_hook: PreApplyHook | None = None,
    pin_write_hook: PinWriteHook | None = None,
    current_version_hook: CurrentVersionHook | None = None,
    skip_missing_chart_mirror: bool = False,
) -> int:
    _print_run_banner(
        config["SCRIPT_NAME"],
        dry_run=dry_run,
        target_version=target_version,
        extra_lines=[f" Exclude: {exclude_patterns}"] if exclude_patterns else [],
    )

    # Step 1
    print()
    print(f"[Step 1/{total_steps}] Checking current version...")
    chart_yaml = chart_dir / "Chart.yaml"
    current_version = _read_yaml_field(chart_yaml, "version")
    current_app_version = _read_yaml_field(chart_yaml, "appVersion")
    # Pin-only components ship no local Chart.yaml mirror — their version SSOT
    # is the ArgoCD metadata file. Resolve from there instead of proceeding
    # with an empty current version, which silently disables the values diff
    # and the breaking-change scan further down.
    if not current_version and current_version_hook is not None:
        current_version = current_version_hook(chart_dir=chart_dir)
        if current_version:
            current_app_version = "(n/a - pin-only component)"
            print("  (no local Chart.yaml - current version read from the version pin)")
    print(f"  Installed - Chart: {current_version} / App: {current_app_version}")

    # Only templates that declare a pin SSOT (argocd-pin, via
    # ``current_version_hook``) treat an unresolvable current version as fatal.
    # The baseline path must stay permissive: onboarding a freshly-scaffolded
    # component legitimately starts with no Chart.yaml, and the apply step is
    # what materializes it.
    if not current_version and current_version_hook is not None:
        print(
            "  ERROR: could not determine the current chart version - neither "
            "the local Chart.yaml nor the version pin yielded one. Check "
            "ARGOCD_PIN_FILES and the chart.version field. Aborting before any "
            "file is written.",
            file=sys.stderr,
        )
        return 1

    if helmfile_path is not None:
        print()
        print(f"  Helmfile releases ({helmfile_name}):")
        _print_helmfile_releases(helmfile_path)

    # Step 1 hook — values summary (``external_oci_with_mirror``: surface image.tag overrides per
    # values/*.yaml). Default = ``external_oci_with_mirror``'s yq-based per-file dump. Templates
    # that do not expose Step 1 overrides (the four baseline templates) leave this None
    # and the block is skipped entirely.
    if values_summary_hook is not None or total_steps >= 8:
        print()
        print("  Values image overrides:")
        if values_summary_hook is not None:
            values_summary_hook(values_dir=values_dir)
        else:
            _default_values_summary(values_dir=values_dir)

    # Step 2
    print()
    print(f"[Step 2/{total_steps}] Checking latest version...")
    if fetch_latest_hook is not None:
        latest_version_found, latest_app_version = fetch_latest_hook(config=config)
    else:
        latest_version_found, latest_app_version = _default_fetch_latest(config=config)

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
            f"  Latest    - Chart: {latest_version} / App: {latest_app_version}"
        )

    if current_version == latest_version:
        print()
        print("  Already up to date! Nothing to do.")
        return 0

    print()
    print(f"  Upgrade: {current_version} -> {latest_version}")
    print(f"  Changelog: {config['CHANGELOG_URL']}")

    # Steps 3 through the final Apply share a tempdir for fetched chart files.
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir = Path(tmp)
        return _apply_upgrade(
            config=config,
            chart_dir=chart_dir,
            backup_dir=backup_dir,
            values_dir=values_dir,
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
            total_steps=total_steps,
            chart_write_hook=chart_write_hook,
            helmfile_pin_hook=helmfile_pin_hook,
            post_pin_hook=post_pin_hook,
            pre_apply_hook=pre_apply_hook,
            pin_write_hook=pin_write_hook,
            skip_missing_chart_mirror=skip_missing_chart_mirror,
        )


# -----------------------------------------------
# Default Step 1 — values summary (``external_oci_with_mirror`` baseline)
# -----------------------------------------------

def _default_values_summary(*, values_dir: Path) -> None:
    """``external_oci_with_mirror`` baseline: print ``.image.tag`` for each ``values/*.yaml`` via yq.

    yq missing → graceful install hint. No ``values/*.yaml`` → graceful
    message. Mirrors the ``external_oci_with_mirror`` bash default byte-for-byte so consumers can
    omit the hook when the per-file ``image.tag`` view is enough.
    """
    if not values_dir.is_dir():
        print("    (no values/*.yaml found)")
        return
    yaml_files = sorted(values_dir.glob("*.yaml"))
    if not yaml_files:
        print("    (no values/*.yaml found)")
        return
    if shutil.which("yq") is None:
        print("    (yq not installed — install with: brew install yq)")
        return
    for yaml_file in yaml_files:
        if not yaml_file.is_file():
            continue
        result = subprocess.run(
            ["yq", '.image.tag // "(unset)"', str(yaml_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        tag = (result.stdout or "").strip().strip('"') if result.returncode == 0 else "(error)"
        if not tag:
            tag = "(unset)"
        print(f"    {yaml_file.name}: image.tag={tag}")


# -----------------------------------------------
# Default Step 7 — chart + values + schema write
# -----------------------------------------------

def _default_chart_write(
    *,
    chart_dir: Path,
    temp_dir: Path,
    current_version: str,
    latest_version: str,
    latest_app_version: str,
) -> None:
    """Baseline write: copy Chart.yaml + values.yaml + (optional) schema."""
    new_chart = temp_dir / "Chart.yaml"
    shutil.copy2(new_chart, chart_dir / "Chart.yaml")
    print()
    print(
        f"  Updated Chart.yaml ({current_version} -> {latest_version} "
        f"/ App: {latest_app_version})"
    )

    values_new = temp_dir / "values-new.yaml"
    shutil.copy2(values_new, chart_dir / "values.yaml")
    print("  Updated values.yaml")

    pulled_schema = temp_dir / "values.schema.json"
    if pulled_schema.is_file():
        shutil.copy2(pulled_schema, chart_dir / "values.schema.json")
        print("  Updated values.schema.json")


def _apply_upgrade(
    *,
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    values_dir: Path,
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
    total_steps: int = 7,
    chart_write_hook: ChartWriteHook | None = None,
    helmfile_pin_hook: HelmfilePinHook | None = None,
    post_pin_hook: PostPinHook | None = None,
    pre_apply_hook: PreApplyHook | None = None,
    pin_write_hook: PinWriteHook | None = None,
    skip_missing_chart_mirror: bool = False,
) -> int:
    # Step 3
    print()
    print(f"[Step 3/{total_steps}] Fetching Chart.yaml and values.yaml for version {latest_version}...")

    chart_result = _helm(
        "show", "chart", config["HELM_CHART"], "--version", latest_version
    )
    (temp_dir / "Chart.yaml").write_text(chart_result.stdout)

    values_new = temp_dir / "values-new.yaml"
    values_result = _helm(
        "show", "values", config["HELM_CHART"], "--version", latest_version
    )
    values_new.write_text(values_result.stdout)

    pulled = temp_dir / "pulled"
    pulled.mkdir(exist_ok=True)
    _helm(
        "pull",
        config["HELM_CHART"],
        "--version", latest_version,
        "--untar",
        "--untardir", str(pulled),
    )
    schema_src = None
    if pulled.is_dir():
        for entry in sorted(pulled.iterdir()):
            if entry.is_dir():
                candidate = entry / "values.schema.json"
                if candidate.is_file():
                    schema_src = candidate
                break
    if schema_src is not None:
        shutil.copy2(schema_src, temp_dir / "values.schema.json")

    values_old = temp_dir / "values-old.yaml"
    if config["CHART_TYPE"] == "local":
        local_values = chart_dir / "values.yaml"
        if local_values.is_file():
            shutil.copy2(local_values, values_old)
    else:
        old_result = _helm(
            "show", "values", config["HELM_CHART"], "--version", current_version
        )
        # bash redirected stderr to /dev/null and tolerated failure; we mirror
        # by writing whatever stdout came back (possibly empty).
        values_old.write_text(old_result.stdout)

    new_chart = temp_dir / "Chart.yaml"
    if (
        not new_chart.is_file()
        or new_chart.stat().st_size == 0
        or not values_new.is_file()
        or values_new.stat().st_size == 0
    ):
        print(f"  ERROR: Failed to fetch chart for version {latest_version}")
        return 1

    latest_app_version = _read_yaml_field(new_chart, "appVersion")
    print(f"  Downloaded successfully (App: {latest_app_version})")

    # Step 4
    print()
    print(f"[Step 4/{total_steps}] Chart.yaml diff (current vs target)...")
    print(SEPARATOR)
    sys.stdout.write(_diff(chart_dir / "Chart.yaml", new_chart))
    print(SEPARATOR)

    # Step 5
    print()
    print(f"[Step 5/{total_steps}] values.yaml diff (current vs target)...")
    if values_old.is_file() and values_old.stat().st_size > 0:
        diff_text = _diff(values_old, values_new)
        diff_lines = diff_text.count("\n")
        print(f"  Total diff lines: {diff_lines} (showing first 80)")
        print(SEPARATOR)
        for line in diff_text.splitlines()[:80]:
            print(line)
        print(SEPARATOR)
    else:
        print("  Could not fetch old version values for comparison")

    # Step 6
    print()
    print(f"[Step 6/{total_steps}] Checking custom values for breaking changes...")
    if exclude_patterns:
        print(f"  Excluding patterns: {exclude_patterns}")

    old_keys = _extract_top_keys(values_old)
    new_keys = _extract_top_keys(values_new)
    removed_keys = sorted(old_keys - new_keys)
    added_keys = sorted(new_keys - old_keys)
    old_present = values_old.is_file() and values_old.stat().st_size > 0

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

        if old_present:
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
        else:
            print("  SKIP  Could not compare (old version values unavailable)")

    # Blank before the next step (matches the bash "echo \"\"" that
    # precedes the Step 7 / dry-run branch in every template).
    print()

    # ``external_oci_with_mirror`` mirror stage = Step 7 of 8. Skipped entirely when
    # total_steps==7 (the four baseline templates baseline).
    if total_steps >= 8:
        if dry_run:
            print(f"[Step 7/{total_steps}] Mirror stage SKIPPED in dry-run.")
            print()
        else:
            # ``external_oci_with_mirror`` bash inserts another blank line before the mirror header
            # regardless of whether do_mirror is defined.
            print()
            if pre_apply_hook is not None:
                print(
                    f"[Step 7/{total_steps}] Mirroring upstream images to "
                    f"private registry..."
                )
                rc = pre_apply_hook(
                    chart_dir=chart_dir,
                    temp_dir=temp_dir,
                    values_dir=values_dir,
                    latest_version=latest_version,
                    latest_app_version=latest_app_version,
                )
                if rc != 0:
                    print()
                    print(
                        "  ERROR: mirror stage failed. Aborting upgrade "
                        "(no files modified).",
                        file=sys.stderr,
                    )
                    return rc
            else:
                print(
                    f"[Step 7/{total_steps}] Mirror stage skipped "
                    f"(do_mirror not defined in CONFIG)."
                )

    # Final step = Apply (or DRY-RUN exit). ``apply_step`` is the final
    # step number (7 for the four baseline templates, 8 for ``external_oci_with_mirror``).
    apply_step = total_steps
    if dry_run:
        print(
            f"[Step {apply_step}/{total_steps}] DRY-RUN complete. "
            f"No files were changed."
        )
        print()
        print("  To apply: ./upgrade.py")
        if target_version:
            print(f"  To apply: ./upgrade.py --version {target_version}")
        return 0

    print(f"[Step {apply_step}/{total_steps}] Applying upgrade...")

    backup_target = backup_dir / timestamp
    backup_target.mkdir(parents=True, exist_ok=True)

    local_chart_yaml = chart_dir / "Chart.yaml"
    if local_chart_yaml.is_file():
        shutil.copy2(local_chart_yaml, backup_target / "Chart.yaml")
    if helmfile_path is not None and helmfile_path.is_file():
        shutil.copy2(helmfile_path, backup_target / helmfile_name)

    local_values_yaml = chart_dir / "values.yaml"
    if local_values_yaml.is_file():
        shutil.copy2(local_values_yaml, backup_target / "values.yaml")

    local_schema = chart_dir / "values.schema.json"
    if local_schema.is_file():
        shutil.copy2(local_schema, backup_target / "values.schema.json")

    if values_dir.is_dir():
        for values_file in sorted(values_dir.glob("*.yaml")):
            if not values_file.is_file():
                continue
            if _is_excluded(values_file.name, exclude_patterns):
                continue
            shutil.copy2(values_file, backup_target / values_file.name)

    print(f"  Backed up to: backup/{timestamp}/")
    for entry in sorted(backup_target.iterdir()):
        print(f"    - {entry.name}")

    # Chart + values + schema write (overridable for ``external_oci`` wrapper-mode).
    # Pin-only components (argocd-pin with no local Chart.yaml) skip this
    # entirely: the mirror they would gain is a derived copy of a version the
    # ArgoCD metadata pin already owns, and materializing it here would add
    # thousands of lines of generated files that then need tracking in git.
    if skip_missing_chart_mirror and not (chart_dir / "Chart.yaml").is_file():
        print()
        print(
            "  Skipped local chart mirror write (pin-only component - "
            "the ArgoCD metadata file is the version SSOT)"
        )
    elif chart_write_hook is not None:
        chart_write_hook(
            chart_dir=chart_dir,
            temp_dir=temp_dir,
            current_version=current_version,
            latest_version=latest_version,
            latest_app_version=latest_app_version,
        )
    else:
        _default_chart_write(
            chart_dir=chart_dir,
            temp_dir=temp_dir,
            current_version=current_version,
            latest_version=latest_version,
            latest_app_version=latest_app_version,
        )

    # Version pin rewrite. The default target is the helmfile (with the ``external_oci``
    # tracked-chart scope override); the argocd-pin template passes
    # ``pin_write_hook`` to redirect the pin into the ArgoCD metadata
    # file(s) instead. That hook fires regardless of helmfile presence,
    # since the migrated components no longer ship a helmfile.
    if pin_write_hook is not None:
        pins = pin_write_hook(
            chart_dir=chart_dir,
            current_version=current_version,
            latest_version=latest_version,
            backup_target=backup_target,
        )
        # A 0-file rewrite means the pin SSOT was not at ``current_version``,
        # so nothing was bumped and the deploy would be a no-op. Reporting
        # success here is what let this failure mode reach a green pipeline.
        if pins == 0:
            print(
                f"  ERROR: version pin rewrite matched 0 file(s) - the pin "
                f"SSOT is not at '{current_version}'. Inspect `git diff` "
                f"before retrying.",
                file=sys.stderr,
            )
            return 1
        print(
            f"  Updated version pin ({pins} file(s): "
            f"{current_version} -> {latest_version})"
        )
    elif helmfile_path is not None and helmfile_path.is_file():
        if helmfile_pin_hook is not None:
            pins = helmfile_pin_hook(
                helmfile_path=helmfile_path,
                helmfile_name=helmfile_name,
                current_version=current_version,
                latest_version=latest_version,
            )
        else:
            pins = _update_helmfile_pins(
                helmfile_path, current_version, latest_version
            )
        print(
            f"  Updated {helmfile_name} ({pins} pin(s): "
            f"{current_version} -> {latest_version})"
        )

    # Template-specific extension point (e.g. external-with-image-tag rewrites
    # `tag: vX.Y.Z` in values files). No-op when the consumer does not pass one.
    if post_pin_hook is not None:
        post_pin_hook(
            values_dir=values_dir,
            exclude_patterns=exclude_patterns,
            latest_app_version=latest_app_version,
        )

    _auto_prune_backups(backup_dir, keep_backups)

    _print_upgrade_footer(
        config,
        current_version,
        latest_version,
        next_steps=(
            _argocd_next_steps(chart_dir, latest_version)
            if pin_write_hook is not None
            else [
                "   1. Review values/ files for any needed changes",
                "   2. Run: helmfile diff",
                "   3. Run: helmfile apply",
            ]
        ),
    )
    return 0


def _argocd_next_steps(chart_dir: Path, latest_version: str) -> list[str]:
    steps = ["Review values/ files for any needed changes"]
    kept = _kept_helmfile_literal_pin(chart_dir)
    if kept:
        steps.append(
            f"Set the chart pin in {kept} to {latest_version} by hand — a hand-synced "
            f"bootstrap copy the upgrade does not touch (see its header)"
        )
    steps.append(
        "Review `git diff`, then commit and push — ArgoCD applies chart.version from the "
        "pin file(s) (auto-sync, or Sync in the UI when autoSync is off)"
    )
    return [f"   {i}. {s}" for i, s in enumerate(steps, start=1)]
