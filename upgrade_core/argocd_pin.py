"""ArgoCD-pin upgrade runner (argocd-pin template).

For infra components migrated to the ArgoCD app-of-apps, the chart-version
SSOT moved out of ``helmfile.yaml`` (now retired to ``backup/``) and into a
per-release ArgoCD metadata file ``<component>/argocd/<release>.yaml`` under
the nested ``chart.version`` field. The infra-applicationset git-files
generator reads that field and ArgoCD auto-sync applies it, so bumping it IS
the cluster upgrade.

This module is a thin dispatcher: it reuses the existing fetch / diff /
breaking-change machinery from :mod:`external_standard` (helm repo charts)
or :mod:`external_oci_with_mirror` (OCI charts + Harbor image mirror) and
swaps only the version-pin WRITE target — via the ``pin_write_hook``
extension point added to :func:`external_standard.run` — so it writes
``chart.version`` into the ArgoCD metadata file(s) instead of a helmfile.

CONFIG keys (in addition to the chosen base template's keys):

  - ``BASE`` — ``"standard"`` (helm repo, like external-standard) or
    ``"oci"`` (OCI + optional Harbor mirror, like external-oci-with-mirror).
  - ``ARGOCD_PIN_FILES`` — list of ArgoCD metadata files to bump, each a
    path RELATIVE to the component directory (``upgrade.py``'s dir), e.g.
    ``["argocd/build-image.yaml", "argocd/deploy-image.yaml"]``. Only the
    tracked releases are listed; a release held at a hand-picked version is
    left out so it is never auto-bumped.

For ``BASE="standard"`` the base reads ``HELM_REPO_NAME`` / ``HELM_REPO_URL``
/ ``HELM_CHART`` / ``CHANGELOG_URL`` / ``CHART_TYPE``; for ``BASE="oci"`` it
reads ``GITHUB_REPO`` / ``GITHUB_TAG_PREFIX`` / ``HELM_CHART`` plus the
optional ``do_mirror`` / ``print_values_summary`` callables — identical to
the wrapped base templates.

The local component ``Chart.yaml`` is an OPTIONAL derived mirror, not the
SSOT. Components that ship one have it refreshed by the base flow; components
that ship none keep none, because ``skip_missing_chart_mirror`` suppresses the
write. Either way the base Step 1 resolves the current version through
``current_version_hook`` below, so it agrees with ``check-versions.py``, which
reads the same ArgoCD metadata file.

``--rollback`` is replaced too: the chart-flavored rollback restores the
backed-up Chart.yaml / values but never touches ``ARGOCD_PIN_FILES``, so it
used to report success while the cluster kept reading the new pin. The
argocd-pin rollback also sets ``chart.version`` back, from the pre-upgrade
version each apply now records in its backup (``PIN_VERSION_FILE``), falling
back to the backed-up Chart.yaml mirror, and fails loudly when neither exists.
It never writes a helmfile back, and ``--list-backups`` shows the pin each
backup would restore.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import _common_argocd, _common_helmfile
from ._common import (
    backup_file_names,
    print_backup_list,
    prompt_select_backup,
    sorted_backups,
)
from .external_oci_with_mirror import run as _run_oci_with_mirror
from .external_standard import run as _run_external_standard

# Not *.yaml: a rollback copies every top-level *.yaml of a backup into values/.
PIN_VERSION_FILE = "argocd-pin-version"


def _make_pin_write_hook(argocd_pin_files: list[str]):
    """Build the ``pin_write_hook`` closure for :func:`external_standard.run`.

    Resolves each ``ARGOCD_PIN_FILES`` entry against ``chart_dir`` (the
    component directory) and flips ``chart.version`` across all of them.
    Returns the count of files actually rewritten (for the operator log).
    When the caller passes this run's ``backup_target`` and a pin moved, the
    pre-upgrade version is recorded there for ``--rollback``.
    """
    def pin_write(
        *,
        chart_dir: Path,
        current_version: str,
        latest_version: str,
        backup_target: Path | None = None,
    ) -> int:
        files = [chart_dir / rel for rel in argocd_pin_files]
        rewritten = _common_argocd.update_argocd_pins(
            files, current_version, latest_version
        )
        # A 0-match run aborts, and its pins were never at current_version.
        if backup_target is not None and rewritten:
            (backup_target / PIN_VERSION_FILE).write_text(f"{current_version}\n")
            print(
                f"  Recorded pre-upgrade pin {current_version} in "
                f"backup/{backup_target.name}/{PIN_VERSION_FILE}"
            )
        return rewritten

    return pin_write


def _backup_pin_version(backup: Path) -> tuple[str, str]:
    """Return ``(version, source file)`` a backup records, or ``("", "")``.

    Backups taken before ``PIN_VERSION_FILE`` existed still carry the
    pre-upgrade Chart.yaml mirror, whose ``version`` is the same chart pin.
    """
    recorded = backup / PIN_VERSION_FILE
    if recorded.is_file():
        version = recorded.read_text().strip()
        if version:
            return version, PIN_VERSION_FILE
    chart_yaml = backup / "Chart.yaml"
    if chart_yaml.is_file():
        version = _common_helmfile.read_yaml_field(chart_yaml, "version").strip("\"'")
        if version:
            return version, "Chart.yaml"
    return "", ""


def _describe_backup(backup: Path) -> str:
    """Backup-list row: the pin a rollback to this backup would restore."""
    version, source = _backup_pin_version(backup)
    pin = f"pin: {version} from {source}" if version else "pin: not recorded"
    return f"({pin}) — {backup_file_names(backup)}"


def _list_backups(*, backup_dir: Path) -> None:
    print_backup_list(backup_dir, _describe_backup)


def _make_rollback_hook(argocd_pin_files: list[str]):
    """Build the ``rollback_hook`` closure: restore a backup AND its chart pin.

    Every check runs before the first file is copied, so a refused rollback
    leaves the tree untouched.
    """
    def rollback(*, backup_dir: Path, chart_dir: Path, values_dir: Path) -> None:
        backups = sorted_backups(backup_dir)
        if not backups:
            print("No backups found.")
            sys.exit(1)

        _list_backups(backup_dir=backup_dir)
        selected = prompt_select_backup(backups)
        print()

        # A listed file may not exist yet (a marker parked under _pending/);
        # the upgrade skips those too.
        existing = [rel for rel in argocd_pin_files if (chart_dir / rel).is_file()]
        target, source = _backup_pin_version(selected)
        pins = {
            rel: _common_argocd.read_argocd_chart_version(chart_dir / rel)
            for rel in existing
        }
        if not existing:
            problem = f"none of ARGOCD_PIN_FILES exists ({', '.join(argocd_pin_files)})"
        elif not target:
            problem = (
                f"backup/{selected.name} records no chart version (no "
                f"{PIN_VERSION_FILE}, no Chart.yaml)"
            )
        elif not all(pins.values()):
            unreadable = [rel for rel, have in pins.items() if not have]
            problem = f"no chart.version to rewrite in {', '.join(unreadable)}"
        else:
            problem = ""
        if problem:
            print(
                f"  ERROR: {problem}. Nothing was restored — set chart.version "
                f"by hand, or `git revert` the upgrade commit.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Restoring from backup/{selected.name}...")
        _common_helmfile.restore_backup_files(
            selected, chart_dir, values_dir, restore_helmfile=False
        )
        for rel, have in pins.items():
            if have == target:
                continue
            if not _common_argocd.update_argocd_chart_version(chart_dir / rel, have, target):
                print(
                    f"  ERROR: could not set chart.version to {target} in {rel}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"  Restored chart.version {have} -> {target} in {rel}")
        # A bootstrap helmfile left on disk hand-syncs its literal pin to chart.version.
        _common_helmfile.align_kept_helmfile_pin(chart_dir, next(iter(pins.values())), target)

        print()
        print(
            f"Rollback complete! chart.version is {target} "
            f"(from the backup's {source})."
        )
        print(
            "  Review `git diff`, then commit and push — the ArgoCD Application "
            "reads chart.version from the pin file(s)."
        )

    return rollback


def _make_current_version_hook(argocd_pin_files: list[str]):
    """Build the Step 1 ``current_version_hook`` closure.

    The ArgoCD metadata file IS the chart-version SSOT, so a component that
    keeps no local ``Chart.yaml`` mirror still has an authoritative current
    version. Returns the first non-empty ``chart.version`` across the tracked
    pin files — they are held at the same version by construction, since
    :func:`_make_pin_write_hook` bumps them together.
    """
    def current_version(*, chart_dir: Path) -> str:
        for rel in argocd_pin_files:
            found = _common_argocd.read_argocd_chart_version(chart_dir / rel)
            if found:
                return found
        return ""

    return current_version


def run(config: dict, argv: list[str], script_path: str | os.PathLike) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Dispatches on ``config["BASE"]`` and injects the ArgoCD pin writer.
    """
    base = config.get("BASE", "standard")
    argocd_pin_files = config.get("ARGOCD_PIN_FILES")
    if not argocd_pin_files:
        print(
            "  ERROR: argocd-pin template requires a non-empty "
            "ARGOCD_PIN_FILES list in CONFIG.",
            file=sys.stderr,
        )
        return 1

    pin_write_hook = _make_pin_write_hook(list(argocd_pin_files))
    current_version_hook = _make_current_version_hook(list(argocd_pin_files))
    rollback_hook = _make_rollback_hook(list(argocd_pin_files))

    if base == "oci":
        return _run_oci_with_mirror(
            config,
            argv,
            script_path,
            pin_write_hook=pin_write_hook,
            current_version_hook=current_version_hook,
            rollback_hook=rollback_hook,
            list_backups_hook=_list_backups,
            skip_missing_chart_mirror=True,
        )
    if base == "standard":
        return _run_external_standard(
            config,
            argv,
            script_path,
            pin_write_hook=pin_write_hook,
            current_version_hook=current_version_hook,
            rollback_hook=rollback_hook,
            list_backups_hook=_list_backups,
            skip_missing_chart_mirror=True,
        )

    print(
        f"  ERROR: argocd-pin template: unknown BASE '{base}' "
        "(expected 'standard' or 'oci').",
        file=sys.stderr,
    )
    return 1
