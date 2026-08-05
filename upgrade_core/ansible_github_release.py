"""Ansible-deployed component upgrade runner (ansible-github-release template).

Drives the upgrade flow for components deployed via Ansible (NOT Helm) that
track their version from a GitHub Releases feed and store the current version
in a single YAML key (e.g. `node_exporter_version` in `group_vars/all.yml`).

Migrated from the canonical bash template at
``scripts/upgrade-sync/templates/ansible-github-release.sh`` as part of the
the shell -> python migration shell -> python migration.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ._common import (
    DOUBLE_SEP,
    SEPARATOR,
    auto_prune_backups as _auto_prune_backups,
    cleanup_backups as _cleanup_backups,
    fetch_github_ga_versions as _fetch_github_ga_versions,
    now_timestamp,
    prompt_major_bump as _prompt_major_bump,
    prompt_select_backup as _prompt_select_backup,
    read_keep_backups_env,
    read_yaml_value as _read_yaml_value,
    sorted_backups as _sorted_backups,
    update_yaml_value as _update_yaml_value,
)


def run(config: dict, argv: list[str], script_path: str | os.PathLike) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Returns the process exit code (0 success, non-zero failure).
    """
    script = Path(script_path).resolve()
    chart_dir = script.parent
    backup_dir = chart_dir / "backup"
    timestamp = now_timestamp()
    keep_backups = read_keep_backups_env()
    prog = script.name

    args = _parse_args(
        argv, prog, keep_backups, backup_dir, chart_dir,
        config["VERSION_FILE"], config["VERSION_KEY"],
        config["ANSIBLE_DIR"], config["ANSIBLE_INVENTORY"],
        config["ANSIBLE_UPGRADE_PLAYBOOK"],
    )
    if args is None:
        return 0

    return _main_flow(
        config=config,
        chart_dir=chart_dir,
        backup_dir=backup_dir,
        timestamp=timestamp,
        keep_backups=keep_backups,
        dry_run=args["dry_run"],
        target_version=args["target_version"],
    )


# -----------------------------------------------
# Argument parsing
# -----------------------------------------------

def _parse_args(
    argv: list[str],
    prog: str,
    keep_backups: int,
    backup_dir: Path,
    chart_dir: Path,
    version_file: str,
    version_key: str,
    ansible_dir: str,
    ansible_inventory: str,
    ansible_upgrade_playbook: str,
) -> dict | None:
    dry_run = False
    target_version = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            _usage(prog, keep_backups)
            sys.exit(0)
        elif arg == "--list-backups":
            _list_backups(backup_dir, version_file, version_key)
            sys.exit(0)
        elif arg == "--rollback":
            _do_rollback(
                backup_dir, chart_dir, version_file, version_key,
                ansible_dir, ansible_inventory, ansible_upgrade_playbook,
            )
            sys.exit(0)
        elif arg == "--cleanup-backups":
            _cleanup_backups(backup_dir, keep_backups)
            sys.exit(0)
        elif arg == "--dry-run":
            dry_run = True
            i += 1
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

    return {"dry_run": dry_run, "target_version": target_version}


def _usage(prog: str, keep_backups: int) -> None:
    print(f"""Usage: {prog} [COMMAND] [OPTIONS]

Tracks the upstream version of an Ansible-deployed component and bumps the
version field in the Ansible variables file.

Commands:
  (default)           Check latest version and upgrade
  --version <VER>     Upgrade to a specific version (skips upstream query)
  --dry-run           Preview changes only (no files will be modified)
  --rollback          Restore from a previous backup
  --list-backups      List available backups
  --cleanup-backups   Keep only the last {keep_backups} backups, remove older ones
  -h, --help          Show this help message

Examples:
  {prog}                                # Upgrade to latest GA
  {prog} --dry-run                      # Preview upgrade without changes
  {prog} --version 1.12.0               # Pin to a specific version
  {prog} --rollback                     # Restore from backup""")


# -----------------------------------------------
# YAML helpers (top-level string value read + quote-preserving update)
# come from ``_common`` (moved in Phase 3 from the K12/K13 ``_common_cr``
# module so non-CR templates can use them without a CR-domain
# dependency). Module-level aliases preserve the original ``ag.``
# names for the test suite.
# -----------------------------------------------


# -----------------------------------------------
# Backup helpers (list / rollback) — shared sorted/cleanup/prune helpers
# come from `_common` (ansible-flavored list + rollback stay here).
# -----------------------------------------------

def _list_backups(backup_dir: Path, version_file: str, version_key: str) -> None:
    print("Available backups:")
    print()
    backups = _sorted_backups(backup_dir)
    if not backups:
        print("  No backups found.")
        return
    vfile_base = Path(version_file).name
    for idx, d in enumerate(backups, start=1):
        ver = "unknown"
        snapshot = d / vfile_base
        if snapshot.is_file():
            value = _read_yaml_value(snapshot, version_key)
            if value:
                ver = value
        names = sorted(p.name for p in d.iterdir())
        files = ", ".join(names)
        print(f"  [{idx}] {d.name} (version: {ver}) — {files}")
    print()


def _do_rollback(
    backup_dir: Path,
    chart_dir: Path,
    version_file: str,
    version_key: str,
    ansible_dir: str,
    ansible_inventory: str,
    ansible_upgrade_playbook: str,
) -> None:
    backups = _sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        sys.exit(1)

    _list_backups(backup_dir, version_file, version_key)

    selected = _prompt_select_backup(backups)
    print()
    print(f"Restoring from backup/{selected.name}...")

    vfile_base = Path(version_file).name
    snapshot = selected / vfile_base
    if snapshot.is_file():
        target = chart_dir / version_file
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot, target)
        print(f"  Restored {version_file}")
    else:
        print(f"  ERROR: backup does not contain {vfile_base}")
        sys.exit(1)

    print()
    print("Rollback complete! Next steps to apply on hosts:")
    print(
        f"   cd {ansible_dir} && ansible-playbook -i {ansible_inventory} "
        f"{ansible_upgrade_playbook}"
    )


# GitHub Releases helpers come from `_common` (the shell -> python migration / the migration
# extraction). `_fetch_latest_version` is a thin convenience wrapper.

def _fetch_latest_version(github_repo: str, major_pin: str) -> str:
    versions = _fetch_github_ga_versions(github_repo, major_pin)
    return versions[0] if versions else ""


# -----------------------------------------------
# Main 5-step flow
# -----------------------------------------------

def _main_flow(
    *,
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    timestamp: str,
    keep_backups: int,
    dry_run: bool,
    target_version: str,
) -> int:
    print(DOUBLE_SEP)
    print(f" {config['SCRIPT_NAME']}")
    if dry_run:
        print(" Mode: DRY-RUN (no files will be changed)")
    if target_version:
        print(f" Target: v{target_version}")
    if config["MAJOR_PIN"]:
        print(f" Major pin: {config['MAJOR_PIN']}.x")
    print(DOUBLE_SEP)

    # Step 1
    print()
    print(f"[Step 1/5] Reading current version from {config['VERSION_FILE']}...")
    version_path = chart_dir / config["VERSION_FILE"]
    if not version_path.is_file():
        print(f"  ERROR: version file not found: {version_path}")
        return 1
    current_version = _read_yaml_value(version_path, config["VERSION_KEY"])
    if not current_version:
        print(
            f"  ERROR: could not read '{config['VERSION_KEY']}' from "
            f"{config['VERSION_FILE']}"
        )
        return 1
    print(f"  Current {config['COMPONENT_NAME']} version: {current_version}")

    # Step 2
    print()
    print(
        f"[Step 2/5] Checking latest upstream version "
        f"(GitHub: {config['GITHUB_REPO']})..."
    )

    if target_version:
        latest_version = target_version
        print(f"  Using explicit target: {target_version}")
    else:
        latest_version = _fetch_latest_version(
            config["GITHUB_REPO"], config["MAJOR_PIN"],
        )
        if not latest_version:
            print("  ERROR: failed to fetch latest version from GitHub.")
            print(
                f"  Verify network access and GITHUB_REPO='{config['GITHUB_REPO']}'."
            )
            return 1
        print(f"  Latest available:      {latest_version}")

    if current_version == latest_version:
        print()
        print("  Already up to date! Nothing to do.")
        return 0

    print()
    print(f"  Upgrade: {current_version} -> {latest_version}")
    print(f"  Changelog: {config['CHANGELOG_URL']}")

    # Step 3
    print()
    print(f"[Step 3/5] Diff preview for {config['VERSION_FILE']}...")
    print(SEPARATOR)
    print(f"- {config['VERSION_KEY']}: \"{current_version}\"")
    print(f"+ {config['VERSION_KEY']}: \"{latest_version}\"")
    print(SEPARATOR)

    if not _prompt_major_bump(
        current_version=current_version,
        latest_version=latest_version,
        changelog_url=config["CHANGELOG_URL"],
        dry_run=dry_run,
    ):
        return 1

    # Step 4
    print()
    if dry_run:
        print("[Step 4/5] DRY-RUN complete. No files were changed.")
        print()
        print("  To apply: ./upgrade.py")
        if target_version:
            print(f"  To apply: ./upgrade.py --version {target_version}")
        return 0

    print(f"[Step 4/5] Backing up {config['VERSION_FILE']}...")
    backup_target = backup_dir / timestamp
    backup_target.mkdir(parents=True, exist_ok=True)
    vfile_base = Path(config["VERSION_FILE"]).name
    shutil.copy2(version_path, backup_target / vfile_base)
    print(f"  Backed up to: backup/{timestamp}/{vfile_base}")

    # Step 5
    print()
    print("[Step 5/5] Applying version update...")
    _update_yaml_value(version_path, config["VERSION_KEY"], latest_version)
    print(
        f"  Updated {config['VERSION_FILE']} "
        f"({config['VERSION_KEY']}: {current_version} -> {latest_version})"
    )

    _auto_prune_backups(backup_dir, keep_backups)

    print()
    print(DOUBLE_SEP)
    print(f" Upgrade complete! ({current_version} -> {latest_version})")
    print()
    print(f" Changelog: {config['CHANGELOG_URL']}")
    print()
    print(" Next steps:")
    print(f"   1. Review the change: git diff {config['VERSION_FILE']}")
    print(
        f"   2. Apply to hosts:    cd {config['ANSIBLE_DIR']} && ansible-playbook "
        f"-i {config['ANSIBLE_INVENTORY']} {config['ANSIBLE_UPGRADE_PLAYBOOK']}"
    )
    print("   3. Verify on a host:  curl http://<host>:<port>/metrics | head")
    print()
    print(" To rollback (source file only, then re-run ansible-playbook):")
    print("   ./upgrade.py --rollback")
    print(DOUBLE_SEP)
    return 0
