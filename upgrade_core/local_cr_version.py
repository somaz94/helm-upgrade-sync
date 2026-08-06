"""Local CR-version upgrade runner (local-cr-version template).

Migrated from ``templates/local-cr-version.sh`` during the shell -> python
migration. **Sister template** of :mod:`external_oci_cr_version`; the two
share ~90% of their helpers via :mod:`._common_cr`.

Used by LOCAL Helm charts that wrap a Custom Resource and have **no
upstream Helm chart** to sync from. Typical shape:

  - ``Chart.yaml`` (local metadata, appVersion tracks component version)
  - ``helmfile.yaml`` (``chart: .``)
  - ``values/<env>.yaml`` (holds ``<VERSION_KEY>`` — e.g. ``version``)
  - ``templates/*.yaml`` (owned by us, not synced from upstream)

What this script does:
  1. Reads the current version from ``<CHART_DIR>/<VALUES_FILE>``.
  2. Queries the component's version feed for the latest GA version
     (one of ``elastic-artifacts`` / ``github-releases`` /
     ``docker-hub-tags`` — same 3 backends as ``external_oci_cr_version``).
  3. Verifies the container image exists in the registry before applying.
  4. Diffs and, on apply, updates both ``<VALUES_FILE>.<VERSION_KEY>``
     and ``Chart.yaml.appVersion``. When ``MIRROR_CHART_VERSION`` is
     truthy, also mirrors into ``Chart.yaml.version`` (useful for
     single-CR wrapper charts where chart version == app version).

Difference vs ``external-oci-cr-version``:

  - **This template owns** Chart.yaml (local metadata mirror).
  - **This template only**: ``MIRROR_CHART_VERSION`` option.
  - **This template only**: backup contains Chart.yaml + values file
    (``external_oci_cr_version`` backups are values-only).
  - **This template does NOT** have the OCI chart-pin sub-flow
    (``--check-chart`` / ``--upgrade-chart``) — this is a local chart so
    there's no upstream OCI pin to track.

**0 consumer** currently (orphan template, scaffolding for future
operators per ``README.md`` — CNPG / Strimzi /
Redis Operator extension path). Kept around because the helper set is
already exercised by ``external_oci_cr_version``'s 2 consumers (elasticsearch + kibana) and the
``_common_cr.py`` shared layer makes maintenance free.

Public entry-point: ``run(config, argv, script_path=__file__)``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._common import (
    DATA_BACKUP_WARNING,
    auto_prune_backups,
    backup_file_names,
    cleanup_backups,
    now_timestamp,
    print_backup_list,
    prompt_major_bump,
    prompt_select_backup,
    read_keep_backups_env,
    sorted_backups,
)
from ._common_cr import (
    check_cluster_health,
    check_dependency_version,
    fetch_latest_version,
    get_live_cr_version,
    handle_downgrade_rollback,
    read_yaml_value,
    semver_compare,
    update_yaml_value,
    verify_image_with_fallback,
)
from ._common_helmfile import detect_helmfile


# =============================================================
# Template-specific helpers
# =============================================================

def _read_chart_field(chart_yaml: Path, field: str) -> str:
    """Return the value of a top-level Chart.yaml field, empty on miss.

    Bash counterpart: ``grep '^<field>:' Chart.yaml | awk '{print $2}'
    | tr -d '"'``. Used for ``appVersion`` and ``version``.
    """
    if not chart_yaml.is_file():
        return ""
    pat = re.compile(rf"^{re.escape(field)}:[ \t]+(.+)$")
    for raw in chart_yaml.read_text().splitlines():
        m = pat.match(raw)
        if not m:
            continue
        val = m.group(1).strip()
        val = re.sub(r"\s+#.*$", "", val).strip()
        return val.strip("\"'")
    return ""


def _list_backups(backup_dir: Path, values_file: str) -> None:
    """Print available backups (Chart.yaml + values file).

    Backups always include the values file and (when present)
    Chart.yaml — no chart-pin classifier needed (``external_oci_cr_version`` sister).
    """

    def describe(d: Path) -> str:
        chart_yaml = d / "Chart.yaml"
        chart_ver = _read_chart_field(chart_yaml, "appVersion") if chart_yaml.is_file() else ""
        # Fall back to chart-level `version:` when appVersion is empty.
        if not chart_ver and chart_yaml.is_file():
            chart_ver = _read_chart_field(chart_yaml, "version")
        files = backup_file_names(d, files_only=True)
        return f"(appVersion: {chart_ver or 'unknown'}) — {files}"

    print_backup_list(backup_dir, describe)


def _do_rollback(
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    helmfile_path: Path | None,
) -> int:
    """Restore Chart.yaml + values file from the selected backup.

    Stack-only path (no chart-pin branch — this template has no OCI chart pin).
    Detects downgrade vs live CR. On downgrade, defers to
    :func:`._common_cr.handle_downgrade_rollback` (auto-webhook flow or
    manual 7-step instructions).
    """
    backups = sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        return 1

    _list_backups(backup_dir, config["VALUES_FILE"])
    selected = prompt_select_backup(backups)

    values_basename = Path(config["VALUES_FILE"]).name
    backup_values = selected / values_basename
    backup_chart = selected / "Chart.yaml"

    backup_ver = ""
    if backup_values.is_file():
        backup_ver = read_yaml_value(backup_values, config["VERSION_KEY"])
    live_ver = get_live_cr_version(config["COMPONENT_LABEL"], helmfile_path)
    is_downgrade = False
    if live_ver and backup_ver and live_ver != backup_ver:
        if semver_compare(backup_ver, live_ver) == -1:
            is_downgrade = True

    print()
    print(f"Restoring from backup/{selected.name}...")

    if backup_chart.is_file():
        (chart_dir / "Chart.yaml").write_text(backup_chart.read_text())
        print("  Restored Chart.yaml")
    if backup_values.is_file():
        (chart_dir / config["VALUES_FILE"]).write_text(backup_values.read_text())
        print(f"  Restored {config['VALUES_FILE']}")
    else:
        print(f"  WARN: backup does not contain {values_basename}; nothing to restore.")
        return 1

    if is_downgrade:
        handle_downgrade_rollback(
            config, chart_dir, helmfile_path, live_ver, backup_ver,
        )
        return 0

    print()
    print("Rollback complete! Run 'helmfile diff' to verify, then 'helmfile apply'.")
    return 0


# =============================================================
# Main stack-version flow (Steps 1-7)
# =============================================================

def _stack_upgrade(
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    helmfile_path: Path | None,
    dry_run: bool,
    target_version: str,
    keep_backups: int,
) -> int:
    """7-step main flow.

    Differs from :func:`external_oci_cr_version._stack_upgrade`:
      - Step 1 also reads Chart.yaml.appVersion (``external_oci_cr_version`` reads OCI chart pin).
      - Step 3 "Already up to date" check is wider — both VALUES_FILE
        version AND Chart.yaml appVersion must match upstream.
      - Step 6 backs up Chart.yaml + values file (``external_oci_cr_version``: values only).
      - Step 7 updates VALUES_FILE.VERSION_KEY + Chart.yaml.appVersion
        + (when ``MIRROR_CHART_VERSION`` truthy) Chart.yaml.version.
    """
    component_label = config["COMPONENT_LABEL"]
    values_file = config["VALUES_FILE"]
    version_key = config["VERSION_KEY"]
    version_source = config["VERSION_SOURCE"]
    version_source_arg = config.get("VERSION_SOURCE_ARG", "")
    major_pin = config.get("MAJOR_PIN", "")
    changelog_url = config.get("CHANGELOG_URL", "")
    container_image = config.get("CONTAINER_IMAGE", "")
    dep_kind = config.get("DEPENDENCY_CR_KIND", "")
    dep_name = config.get("DEPENDENCY_CR_NAME", "")
    mirror_chart_version = bool(config.get("MIRROR_CHART_VERSION", False))
    script_name = config.get("SCRIPT_NAME", "Upgrade Script")

    print("================================================")
    print(f" {script_name}")
    if dry_run:
        print(" Mode: DRY-RUN (no files will be changed)")
    if target_version:
        print(f" Target: v{target_version}")
    if major_pin:
        print(f" Major pin: {major_pin}.x")
    print("================================================")

    # Step 1: Read current version (+ Chart.yaml.appVersion).
    print()
    print(f"[Step 1/7] Reading current version from {values_file}...")
    values_path = chart_dir / values_file
    if not values_path.is_file():
        print(f"  ERROR: values file not found: {values_path}")
        return 1
    current_version = read_yaml_value(values_path, version_key)
    if not current_version:
        print(f"  ERROR: could not read '{version_key}' from {values_file}")
        return 1
    print(f"  Current {component_label} version: {current_version}")

    chart_yaml = chart_dir / "Chart.yaml"
    current_app_version = ""
    if chart_yaml.is_file():
        current_app_version = _read_chart_field(chart_yaml, "appVersion")
        print(f"  Chart.yaml appVersion:       {current_app_version}")

    # Step 2: Pre-flight cluster health.
    print()
    print("[Step 2/7] Pre-flight cluster health check...")
    if not check_cluster_health(component_label, helmfile_path):
        print()
        force = input("  Proceed anyway? [y/N]: ").strip()
        if not force.lower().startswith("y"):
            print("Aborted.")
            return 1

    # Step 3: Fetch latest version.
    print()
    print(f"[Step 3/7] Checking latest upstream version (source: {version_source})...")
    if target_version:
        latest_version = target_version
        print(f"  Using explicit target: {target_version}")
    else:
        latest_version = fetch_latest_version(version_source, version_source_arg, major_pin)
        if not latest_version:
            print(f"  ERROR: failed to fetch latest version from '{version_source}'.")
            print("  Verify network access and the source endpoint.")
            return 1
        print(f"  Latest available:      {latest_version}")

    # "Already up to date" — this template widens the check: VALUES_FILE.version
    # must match AND (Chart.yaml.appVersion absent OR matches).
    up_to_date = current_version == latest_version and (
        not current_app_version or current_app_version == latest_version
    )
    if up_to_date:
        print()
        print("  Already up to date! Nothing to do.")
        return 0

    print()
    print(f"  Upgrade: {current_version} -> {latest_version}")
    print(f"  Changelog: {changelog_url}")

    # Step 4: Verify container image (with fallback search if missing).
    verify_outcome = verify_image_with_fallback(
        container_image=container_image,
        latest_version=latest_version,
        current_version=current_version,
        version_source=version_source,
        version_source_arg=version_source_arg,
        major_pin=major_pin,
        target_version=target_version,
        dry_run=dry_run,
    )
    if not verify_outcome.proceed:
        return verify_outcome.exit_code
    latest_version = verify_outcome.effective_version

    # Step 5: Compatibility + dependency CR + major bump warning.
    print()
    print("[Step 5/7] Compatibility checks")
    print(
        f"  * Verify the currently installed operator supports {component_label} {latest_version}."
    )
    print("  * For Stack major bumps (e.g. 8.x -> 9.x) review breaking changes before applying.")
    print(
        f"  * Keep this component on the same Stack version as its operator dependency."
    )

    if dep_kind and dep_name:
        print()
        print("  Checking dependency CR version constraint...")
        if not check_dependency_version(
            latest_version, dep_kind, dep_name, helmfile_path, component_label
        ):
            return 1

    if not prompt_major_bump(
        current_version=current_version,
        latest_version=latest_version,
        changelog_url=changelog_url,
        dry_run=dry_run,
        extra_lines=DATA_BACKUP_WARNING,
    ):
        return 1

    # Step 6: Dry-run exit / backup (Chart.yaml + values).
    print()
    if dry_run:
        print("[Step 6/7] DRY-RUN complete. No files were changed.")
        print()
        print("  To apply: ./upgrade.py")
        if target_version:
            print(f"  To apply: ./upgrade.py --version {target_version}")
        return 0

    print("[Step 6/7] Backing up current files...")
    timestamp = now_timestamp()
    bdir = backup_dir / timestamp
    bdir.mkdir(parents=True, exist_ok=True)
    values_basename = Path(values_file).name
    (bdir / values_basename).write_text(values_path.read_text())
    if chart_yaml.is_file():
        (bdir / "Chart.yaml").write_text(chart_yaml.read_text())
    print(f"  Backed up to: backup/{timestamp}/")
    for f in sorted(bdir.iterdir()):
        print(f"    - {f.name}")

    # Step 7: Apply (values + Chart.yaml.appVersion + optional mirror).
    print()
    print("[Step 7/7] Applying version update...")
    update_yaml_value(values_path, version_key, latest_version)
    print(
        f"  Updated {values_file} ({version_key}: {current_version} -> {latest_version})"
    )

    if chart_yaml.is_file():
        update_yaml_value(chart_yaml, "appVersion", latest_version)
        print(
            f"  Updated Chart.yaml (appVersion: {current_app_version or 'unset'} -> {latest_version})"
        )

        if mirror_chart_version:
            current_chart_version = _read_chart_field(chart_yaml, "version")
            if current_chart_version != latest_version:
                update_yaml_value(chart_yaml, "version", latest_version)
                print(
                    f"  Updated Chart.yaml (version: {current_chart_version or 'unset'} -> "
                    f"{latest_version}) [mirrored]"
                )

    auto_prune_backups(backup_dir, keep_backups)

    print()
    print("================================================")
    print(f" Upgrade complete! ({current_version} -> {latest_version})")
    print()
    print(f" Changelog: {changelog_url}")
    print()
    print(" Next steps:")
    print(f"   1. Verify the operator supports {component_label} {latest_version}.")
    print("   2. Run: helmfile diff")
    print("   3. Run: helmfile apply")
    print(f"   4. Watch CR: kubectl -n <ns> get {component_label} -w")
    print()
    print(" To rollback:")
    print("   ./upgrade.py --rollback")
    print("================================================")
    return 0


# =============================================================
# Argument parsing + entry point
# =============================================================

def _usage(config: dict, keep_backups: int) -> int:
    """Print the help text mirroring the bash ``usage`` heredoc."""
    script_name = config.get("SCRIPT_NAME", "Upgrade Script")
    print("Usage: upgrade.py [COMMAND] [OPTIONS]")
    print()
    print(script_name)
    print(
        "Tracks the upstream version of a Custom Resource's component and bumps the"
    )
    print(
        f"version field inside {config.get('VALUES_FILE', 'values/dev.yaml')} (plus Chart.yaml appVersion)."
    )
    print()
    print("Commands:")
    print("  (default)           Check latest version and upgrade")
    print("  --version <VER>     Upgrade to a specific version (skips upstream query)")
    print("  --dry-run           Preview changes only (no files will be modified)")
    print("  --rollback          Restore from a previous backup")
    print("  --list-backups      List available backups")
    print(f"  --cleanup-backups   Keep only the last {keep_backups} backups, remove older ones")
    print("  -h, --help          Show this help message")
    return 0


def _parse_argv(
    argv: list[str], config: dict, keep_backups: int
) -> tuple[str, str, bool, int]:
    """Return ``(mode, target_version, dry_run, early_exit_code)``."""
    mode = "stack"
    target_version = ""
    dry_run = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            _usage(config, keep_backups)
            return mode, target_version, dry_run, 0
        if arg == "--list-backups":
            mode = "list-backups"
            i += 1
            continue
        if arg == "--rollback":
            mode = "rollback"
            i += 1
            continue
        if arg == "--cleanup-backups":
            mode = "cleanup-backups"
            i += 1
            continue
        if arg == "--dry-run":
            dry_run = True
            i += 1
            continue
        if arg == "--version":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print("ERROR: --version requires a version number")
                return mode, target_version, dry_run, 1
            target_version = argv[i + 1]
            i += 2
            continue
        print(f"Unknown option: {arg}")
        print()
        _usage(config, keep_backups)
        return mode, target_version, dry_run, 1

    return mode, target_version, dry_run, -1


def run(
    config: dict,
    argv: list[str],
    *,
    script_path: str | os.PathLike,
) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``."""
    chart_dir = Path(script_path).resolve().parent
    backup_dir = chart_dir / "backup"
    keep_backups = read_keep_backups_env()
    helmfile_path, _ = detect_helmfile(chart_dir)

    mode, target_version, dry_run, early_rc = _parse_argv(argv, config, keep_backups)
    if early_rc != -1:
        return early_rc

    if mode == "list-backups":
        _list_backups(backup_dir, config["VALUES_FILE"])
        return 0
    if mode == "rollback":
        return _do_rollback(config, chart_dir, backup_dir, helmfile_path)
    if mode == "cleanup-backups":
        cleanup_backups(backup_dir, keep_backups)
        return 0
    return _stack_upgrade(
        config,
        chart_dir,
        backup_dir,
        helmfile_path,
        dry_run,
        target_version,
        keep_backups,
    )
