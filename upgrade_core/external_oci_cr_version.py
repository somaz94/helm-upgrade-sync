"""External OCI CR-version upgrade runner (external-oci-cr-version template).

Migrated from ``templates/external-oci-cr-version.sh`` during the
shell -> python migration, then refactored to share CR-version helpers with
the local sister template (``local_cr_version``) via :mod:`._common_cr`.

Independent module following the ``local_with_templates`` pattern
— does **not** extend :mod:`external_standard` via hooks because the
CR-version flow is structurally different:

  - **Stack/component version** track (default) — reads ``<VALUES_FILE>``
    field ``<VERSION_KEY>``, queries an upstream version feed (one of
    ``elastic-artifacts`` / ``github-releases`` / ``docker-hub-tags``),
    verifies the container image, performs pre-flight CR health checks,
    optional dependency-CR version constraint, then writes back to
    ``<VALUES_FILE>`` only (NO Chart.yaml here — chart metadata lives in
    the upstream OCI publisher).
  - **OCI chart pin** track (``--check-chart`` / ``--upgrade-chart``) —
    reads the chart pin from ``helmfile.yaml`` (or its ``.gotmpl``
    sibling), queries the publisher's GitHub Releases for the latest
    ``<CHART_NAME>-<semver>`` tag, ``helm pull`` both versions, renders
    each with the active values file, shows a unified diff, then bumps
    the helmfile pin on apply.
  - **Rollback** — auto-detects whether the selected backup is a Stack
    bump (``<TIMESTAMP>`` dir with values file) or a chart-pin bump
    (``<TIMESTAMP>-chart`` dir with helmfile). For a component ArgoCD
    delivers, a chart-pin backup predates the move and is refused, and a
    Stack rollback restores the values file with a downgrade warning (live
    CR when readable, else the working-tree values file) and, on a
    downgrade, the manual ArgoCD steps (operator marker ``autoSync: false``
    first — no auto-webhook flow on this path). Otherwise Stack
    rollback detects a version downgrade (vs live CR) and defers to
    :func:`._common_cr.handle_downgrade_rollback` (auto-webhook flow or
    7-step manual instructions).

Public entry-point: ``run(config, argv, script_path=__file__)``. Two
production consumers — ``observability/logging/elasticsearch`` and
``observability/logging/kibana``. Kibana additionally sets
``DEPENDENCY_CR_KIND="elasticsearch"`` so its target version stays
``<= elasticsearch.spec.version``.

Template-specific helpers live here. CR-version helpers shared with
``local_cr_version`` live in :mod:`._common_cr`, extracted on the same
two-consumer threshold that produced ``_common_helmfile.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    SEMVER_RE,
    check_cluster_health,
    check_dependency_version,
    fetch_latest_version,
    get_live_cr_version,
    handle_downgrade_rollback,
    http_get,
    kube_context,
    read_helmfile_namespace,
    read_yaml_value,
    semver_compare,
    update_yaml_value,
    verify_image_with_fallback,
)
from ._common_argocd import (
    has_argocd_marker,
    read_argocd_chart_url,
    read_argocd_chart_version,
    read_argocd_release_name,
    update_argocd_chart_version,
)
from ._common_helmfile import detect_helmfile


# =============================================================
# Template-specific constants
# =============================================================

# Chart-pin backup dirs append ``-chart`` so list/rollback can branch
# without inspecting contents.
_CHART_BACKUP_SUFFIX = "-chart"

# Bash awk for the ``$chartVersion := "X.Y.Z"`` gotmpl hoist.
_GOTMPL_HOIST_RE = re.compile(
    r'\$chartVersion[ \t]*:=[ \t]+"([^"]+)"'
)

# Bash awk for ``  version: <value>`` indented release-level line.
# Skip templated values (``{{ ... }}``) — those are gotmpl references,
# not literal pins.
_INDENTED_VERSION_RE = re.compile(r"^[ \t]+version:[ \t]+(\S+)\s*$")


# =============================================================
# Backup classifier + version reader (template-specific)
# =============================================================

def _classify_backup(directory: Path, values_file: str = "") -> str:
    """Return ``"chart"`` | ``"stack"`` | ``"unknown"``.

    Chart-pin backups end in ``-chart`` OR contain a helmfile (yaml or
    gotmpl). Stack backups contain ``basename(values_file)`` (or any
    ``*.yaml`` when ``values_file`` is empty — the looser bash heuristic).
    Unknown = neither marker present.

    The ``values_file`` parameter (N15) tightens the stack heuristic: a
    caller that passes ``"values/dev.yaml"`` checks specifically for
    ``directory / "dev.yaml"`` instead of any ``*.yaml`` child.
    """
    name = directory.name
    if name.endswith(_CHART_BACKUP_SUFFIX):
        return "chart"
    if (directory / "helmfile.yaml").is_file() or (
        directory / "helmfile.yaml.gotmpl"
    ).is_file():
        return "chart"
    if values_file:
        target = directory / Path(values_file).name
        if target.is_file():
            return "stack"
        return "unknown"
    # Looser fallback (bash parity): any *.yaml child = stack.
    yaml_children = [p for p in directory.iterdir() if p.suffix == ".yaml"]
    if yaml_children:
        return "stack"
    return "unknown"


def _read_backup_version(
    directory: Path, values_file: str, version_key: str
) -> str:
    """Read the tracked version from a backup directory.

    ``stack`` backup: read ``<VALUES_FILE>``.``<VERSION_KEY>``.
    ``chart`` backup: read the helmfile chart pin (gotmpl
    ``$chartVersion`` hoist preferred, otherwise the first indented
    ``version:`` line).
    """
    kind = _classify_backup(directory, values_file)
    if kind == "stack":
        f = directory / Path(values_file).name
        return read_yaml_value(f, version_key) if f.is_file() else ""
    if kind == "chart":
        helmfile = directory / "helmfile.yaml.gotmpl"
        if not helmfile.is_file():
            helmfile = directory / "helmfile.yaml"
        if not helmfile.is_file():
            return ""
        return _read_helmfile_chart_pin(helmfile)
    return ""


# =============================================================
# Backup listing + rollback (chart vs stack branch)
# =============================================================

def _list_backups(
    backup_dir: Path, values_file: str, version_key: str
) -> None:
    """Print available backups with type label and tracked version."""
    argocd_delivered = has_argocd_marker(backup_dir.parent)

    def describe(d: Path) -> str:
        kind = _classify_backup(d, values_file)
        ver = _read_backup_version(d, values_file, version_key)
        if kind == "chart" and argocd_delivered:
            label = "chart, pre-ArgoCD, not restorable"
        elif kind == "chart":
            label = "chart"
        elif kind == "stack":
            label = version_key
        else:
            label = "backup"
        files = backup_file_names(d, files_only=True)
        return f"({label}: {ver or 'unknown'}) — {files}"

    print_backup_list(backup_dir, describe)


def _do_rollback(
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    helmfile_path: Path | None,
) -> int:
    """Branch on backup type and restore accordingly."""
    backups = sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        return 1

    _list_backups(backup_dir, config["VALUES_FILE"], config["VERSION_KEY"])
    selected = prompt_select_backup(backups)
    kind = _classify_backup(selected, config["VALUES_FILE"])
    argocd_delivered = has_argocd_marker(chart_dir)

    if kind == "chart" and argocd_delivered:
        # The ArgoCD-era chart bump writes no backup (git is the record), so every
        # chart backup predates the move and holds the retired helmfile.
        pins = ", ".join(
            f"{f.parent.name}/{f.name}" for f in _detect_argocd_pin_files(chart_dir)
        ) or "the argocd*/ markers"
        print()
        old_pin = _read_backup_version(selected, config["VALUES_FILE"], config["VERSION_KEY"])
        print(
            f"  ERROR: backup/{selected.name} (chart pin {old_pin or 'unknown'}) is a "
            f"helmfile chart pin from before the ArgoCD move; restoring it would bring "
            f"the retired helmfile back. Nothing was restored — set chart.version in "
            f"{pins} by hand, or `git revert` the bump commit.",
            file=sys.stderr,
        )
        return 1

    if kind == "chart":
        print()
        print(f"Restoring chart pin from backup/{selected.name}...")
        gotmpl_src = selected / "helmfile.yaml.gotmpl"
        yaml_src = selected / "helmfile.yaml"
        if gotmpl_src.is_file():
            (chart_dir / "helmfile.yaml.gotmpl").write_text(gotmpl_src.read_text())
            print("  Restored helmfile.yaml.gotmpl")
            print()
            print("Chart pin rollback complete! Run 'helmfile diff', then 'helmfile apply'.")
            return 0
        if yaml_src.is_file():
            (chart_dir / "helmfile.yaml").write_text(yaml_src.read_text())
            print("  Restored helmfile.yaml")
            print()
            print("Chart pin rollback complete! Run 'helmfile diff', then 'helmfile apply'.")
            return 0
        print("  WARN: backup does not contain a helmfile; nothing to restore.")
        return 1

    # Stack rollback path.
    backup_ver = _read_backup_version(
        selected, config["VALUES_FILE"], config["VERSION_KEY"]
    )
    if argocd_delivered:
        return _restore_stack_for_argocd(config, chart_dir, selected, backup_ver)
    live_ver = get_live_cr_version(config["COMPONENT_LABEL"], helmfile_path)
    is_downgrade = False
    if live_ver and backup_ver and live_ver != backup_ver:
        if semver_compare(backup_ver, live_ver) == -1:
            is_downgrade = True

    print()
    print(f"Restoring from backup/{selected.name}...")
    values_basename = Path(config["VALUES_FILE"]).name
    src = selected / values_basename
    if not src.is_file():
        print(f"  WARN: backup does not contain {values_basename}; nothing to restore.")
        return 1
    (chart_dir / config["VALUES_FILE"]).write_text(src.read_text())
    print(f"  Restored {config['VALUES_FILE']}")

    if is_downgrade:
        handle_downgrade_rollback(
            config, chart_dir, helmfile_path, live_ver, backup_ver,
            operator_chart_label="eck-operator-dir",
        )
        return 0

    print()
    print("Rollback complete! Run 'helmfile diff' to verify, then 'helmfile apply'.")
    return 0


def _restore_stack_for_argocd(
    config: dict, chart_dir: Path, selected: Path, backup_ver: str
) -> int:
    """Restore the CR version for a component ArgoCD delivers.

    Nothing is applied until the push. The downgrade check reads the live CR (its
    namespace comes from the marker) and falls back to the working-tree values
    file when the cluster cannot be read — a bump whose sync failed is the case
    where the two differ.
    """
    values_file = chart_dir / config["VALUES_FILE"]
    src = selected / values_file.name
    if not src.is_file():
        print(f"  WARN: backup does not contain {values_file.name}; nothing to restore.")
        return 1
    live_ver = get_live_cr_version(
        config["COMPONENT_LABEL"], _detect_argocd_pin_file(chart_dir)
    )
    current_ver = live_ver or read_yaml_value(values_file, config["VERSION_KEY"])
    is_downgrade = bool(current_ver and backup_ver) and (
        semver_compare(backup_ver, current_ver) == -1
    )

    print()
    print(f"Restoring from backup/{selected.name}...")
    values_file.write_text(src.read_text())
    print(f"  Restored {config['VALUES_FILE']}")

    print()
    if is_downgrade:
        basis = "the live CR" if live_ver else "the values file"
        print(
            f"  WARNING: This is a version downgrade ({current_ver} -> {backup_ver}, "
            f"current version from {basis})."
        )
        print(
            "  The Elastic Stack does not downgrade data a newer version has written; "
            "a snapshot restore may be the real rollback."
        )
        print(
            f"  Nothing is pushed yet: back it out with `git restore "
            f"{config['VALUES_FILE']}`, or take these steps:"
        )
        _print_argocd_downgrade_steps(config, chart_dir, backup_ver)
        return 0
    print(
        "Rollback complete! Review `git diff`, then commit and push — ArgoCD applies "
        "the CR version from the values file."
    )
    return 0


def _print_argocd_downgrade_steps(config: dict, chart_dir: Path, backup_ver: str) -> None:
    """Print the manual CR-downgrade steps for a component ArgoCD delivers.

    The operator's App must stop syncing first, or self-heal recreates the webhook
    and scales the operator back up. Only its marker can stop it: the
    ApplicationSet reverts a hand-patched Application.
    """
    ctx = kube_context() or "<kube-context>"
    pin_file = _detect_argocd_pin_file(chart_dir)
    marker = pin_file.parent.name if pin_file is not None else "argocd*"
    cr_ns = read_helmfile_namespace(pin_file) or "<ns>"
    operator_ns = config.get("CR_OPERATOR_NS") or "<operator-ns>"
    operator_sts = config.get("CR_OPERATOR_STS") or "<operator-sts>"
    operator_marker = _operator_marker_path(
        chart_dir, config.get("CR_OPERATOR_CHART_DIR", ""), marker
    )
    label = config["COMPONENT_LABEL"]
    cr = f"kubectl --context {ctx} -n {cr_ns}"
    print(
        f"    1. Set `autoSync: false` in {operator_marker}, commit and push only that, "
        f"and wait until the operator App has no automated sync policy."
    )
    print(f"    2. kubectl --context {ctx} -n {operator_ns} scale sts {operator_sts} --replicas=0")
    print(
        f"    3. kubectl --context {ctx} delete validatingwebhookconfiguration "
        f"{config.get('CR_WEBHOOK_NAME') or '<webhook>'} --ignore-not-found"
    )
    print(
        f"    4. Commit and push this rollback; Sync the {label} App in the ArgoCD UI "
        f"if it does not auto-sync."
    )
    # The webhook comes back in step 6 and would reject a sync that has not landed yet.
    print(f"    5. Confirm the CR took it: {cr} get {label} {label} -o jsonpath='{{.spec.version}}'  # {backup_ver}")
    print(
        "    6. Set `autoSync: true` again and push: ArgoCD recreates the webhook and "
        "scales the operator back up."
    )
    # Kibana has no .status.phase, and a stale Ready passes before the operator reconciles.
    print(
        f"    7. {cr} wait {label}/{label} "
        f"--for=jsonpath='{{.status.version}}'={backup_ver} --timeout=600s"
    )


def _operator_marker_path(chart_dir: Path, operator_dir: str, marker: str) -> str:
    """Operator marker path relative to the component dir, or a pattern when not found."""
    if operator_dir:
        for base in (chart_dir, *chart_dir.parents):
            candidate = base / operator_dir / marker
            if candidate.is_dir():
                files = sorted(candidate.glob("*.yaml"))
                target = files[0] if len(files) == 1 else candidate / "*.yaml"
                return os.path.relpath(target, chart_dir)
    return f"{operator_dir or '<operator-dir>'}/{marker}/*.yaml"


# =============================================================
# OCI chart-pin helpers (template-specific)
# =============================================================

def _read_helmfile_chart_url(helmfile_path: Path) -> str:
    """Read the first ``chart:`` URL under ``releases:``."""
    if not helmfile_path.is_file():
        return ""
    for raw in helmfile_path.read_text().splitlines():
        m = re.match(r"^\s*chart:[ \t]+(.+)$", raw)
        if m:
            val = m.group(1).strip()
            val = re.sub(r"\s+#.*$", "", val).strip()
            return val.strip("\"'")
    return ""


def _read_helmfile_chart_pin(helmfile_path: Path) -> str:
    """Read the chart pin from helmfile (yaml or gotmpl).

    Preference order:
      1. ``{{- $chartVersion := "X.Y.Z" }}`` hoist (gotmpl only).
      2. First indented ``version: X.Y.Z`` release-level line,
         skipping any ``{{ ... }}`` templated value.

    Returns empty string on no match.
    """
    if not helmfile_path.is_file():
        return ""
    lines = helmfile_path.read_text().splitlines()
    for raw in lines:
        m = _GOTMPL_HOIST_RE.search(raw)
        if m:
            return m.group(1)
    for raw in lines:
        if "{{" in raw:
            continue
        m = _INDENTED_VERSION_RE.match(raw)
        if m:
            return m.group(1).strip("\"'")
    return ""


def _update_helmfile_chart_pin(helmfile_path: Path, new_pin: str) -> None:
    """Replace the first chart pin in helmfile, preserving quote style.

    Preference order:
      1. ``$chartVersion := "X.Y.Z"`` hoist (always double-quoted).
      2. First indented ``version: X.Y.Z`` release-level line (skipping
         ``{{ ... }}`` templated lines). Preserves double, single, or
         bare quoting on the original line.
    """
    text = helmfile_path.read_text()
    lines: list[str] = []
    patched = False
    for line in text.splitlines(keepends=True):
        if patched:
            lines.append(line)
            continue
        stripped_eol = line.rstrip("\n")
        ending = line[len(stripped_eol):]
        if _GOTMPL_HOIST_RE.search(stripped_eol):
            new_line = _GOTMPL_HOIST_RE.sub(
                lambda _m: f'$chartVersion := "{new_pin}"', stripped_eol, count=1
            )
            lines.append(new_line + ending)
            patched = True
            continue
        ver_m = _INDENTED_VERSION_RE.match(stripped_eol)
        if ver_m is not None and "{{" not in stripped_eol:
            prefix_match = re.match(r"^([ \t]+version:[ \t]+)", stripped_eol)
            assert prefix_match is not None
            prefix = prefix_match.group(1)
            rest = stripped_eol[len(prefix):]
            if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
                new_rest = f'"{new_pin}"'
            elif rest.startswith("'") and rest.endswith("'") and len(rest) >= 2:
                new_rest = f"'{new_pin}'"
            else:
                new_rest = new_pin
            lines.append(f"{prefix}{new_rest}{ending}")
            patched = True
            continue
        lines.append(line)
    helmfile_path.write_text("".join(lines))


def _list_chart_versions(
    chart_source_type: str, chart_source_repo: str, chart_name: str
) -> list[str]:
    """Return chart-publisher versions newest-first (semver-filtered).

    Currently only ``github-releases`` is supported. Filters release
    tags by ``<chart_name>-`` prefix (e.g. ``elasticsearch-eck-0.1.2``).
    """
    if chart_source_type != "github-releases":
        return []
    if not chart_source_repo or not chart_name:
        return []
    body = http_get(
        f"https://api.github.com/repos/{chart_source_repo}/releases?per_page=100"
    )
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    prefix = f"{chart_name}-"
    versions: list[str] = []
    for r in data:
        if r.get("prerelease") or r.get("draft"):
            continue
        tag = r.get("tag_name", "")
        if not tag.startswith(prefix):
            continue
        rest = tag[len(prefix):]
        if SEMVER_RE.match(rest):
            versions.append(rest)
    versions.sort(key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
    return versions


def _fetch_latest_chart_version(
    chart_source_type: str, chart_source_repo: str, chart_name: str
) -> str:
    """First entry of :func:`_list_chart_versions` or empty string."""
    versions = _list_chart_versions(chart_source_type, chart_source_repo, chart_name)
    return versions[0] if versions else ""


def _read_helmfile_release_name(helmfile_path: Path) -> str:
    """Read the first ``- name: <release>`` line from helmfile.

    Used for ``helm template <release_name> <chart>`` in
    :func:`_do_upgrade_chart`. Skips templated values (``{{ ... }}``).

    N5 fix: the ``{{`` skip check moved BEFORE extracting the value so
    a templated line isn't matched-then-discarded silently.
    """
    if not helmfile_path.is_file():
        return ""
    for raw in helmfile_path.read_text().splitlines():
        if "{{" in raw:
            continue
        m = re.match(r"^\s*-\s*name:[ \t]+(.+)$", raw)
        if not m:
            continue
        return m.group(1).strip().strip("\"'")
    return ""


# =============================================================
# ArgoCD chart-pin redirect (Phase D — migrated CR components)
#
# elasticsearch / kibana migrated to the ArgoCD app-of-apps: their helmfile
# retired to backup/ and the OCI chart-version SSOT moved to
# <component>/argocd/<release>.yaml (chart.version). detect_helmfile() then
# returns None, so the chart-pin track reads/writes that ArgoCD metadata
# instead of a helmfile. The Stack/CR version track (VALUES_FILE) is
# unaffected. When a helmfile IS present (non-migrated components, e.g. the
# -aws variants) the ArgoCD file is never consulted — byte-for-byte
# unchanged behavior, since run() only resolves the ArgoCD file when
# detect_helmfile() returned None.
# =============================================================


# Remote-OCI marker dirs, on-prem first (``argocd-local*`` never apply here:
# this template's consumers pull a public OCI chart, never a vendored one).
# Every marker of one component pins the SAME version, so first-match is
# safe for READS; writes fan out (see _chart_pin_write). This template ignores
# CONFIG.ARGOCD_PIN_FILES. A missing dir is a no-op, but dropping a marker
# makes this probe return None SILENTLY with no test catching it.
_ARGOCD_MARKER_DIRS = ("argocd", "argocd-aws")


def _detect_argocd_pin_files(chart_dir: Path) -> list[Path]:
    """Return EVERY ArgoCD metadata file of this component carrying a pin.

    Scans each marker directory (``argocd/`` for on-prem, ``argocd-aws/`` for
    the AWS variant) in marker order, then filename order within each.

    A component delivered to one cluster yields one file. A component enrolled
    on two delivery tracks yields two, one per cluster, because each cluster's
    ApplicationSet needs its own ``valueFile``. Both pin the same chart
    version, so a bump must rewrite BOTH; see _chart_pin_write.
    """
    found: list[Path] = []
    for marker in _ARGOCD_MARKER_DIRS:
        argocd_dir = chart_dir / marker
        if not argocd_dir.is_dir():
            continue
        for f in sorted(argocd_dir.glob("*.yaml")):
            if read_argocd_chart_version(f):
                found.append(f)
    return found


def _detect_argocd_pin_file(chart_dir: Path) -> Path | None:
    """Return the component's PRIMARY ArgoCD metadata file, or None.

    First match of _detect_argocd_pin_files. Authoritative for reads (current
    pin, chart URL, release name, operator label): every marker of one
    component pins the same version. NOT sufficient for writes.
    """
    found = _detect_argocd_pin_files(chart_dir)
    return found[0] if found else None


def _chart_pin_label(helmfile_name: str, argocd_pin_file: Path | None) -> str:
    """Source-file label for operator messages.

    Uses the pin file's actual parent directory so the label matches the real
    on-disk path — ``argocd/<name>`` for on-prem, ``argocd-aws/<name>`` for the
    AWS variant. The label feeds actionable ``git diff`` / ``git restore``
    next-step commands, so a hardcoded prefix would misdirect the operator.
    """
    if argocd_pin_file is not None:
        return f"{argocd_pin_file.parent.name}/{argocd_pin_file.name}"
    # Both pin sources absent (genuinely misconfigured component) -> a
    # readable label so the "could not read chart pin from <label>" error
    # doesn't render a bare trailing dot.
    return helmfile_name or "helmfile / argocd metadata"


def _chart_pin_current(
    helmfile_path: Path | None, argocd_pin_file: Path | None
) -> str:
    """Current chart pin from the ArgoCD metadata (preferred) or helmfile."""
    if argocd_pin_file is not None:
        return read_argocd_chart_version(argocd_pin_file)
    if helmfile_path is not None:
        return _read_helmfile_chart_pin(helmfile_path)
    return ""


def _chart_pin_url(
    helmfile_path: Path | None, argocd_pin_file: Path | None
) -> str:
    """OCI chart URL from the ArgoCD metadata (preferred) or helmfile."""
    if argocd_pin_file is not None:
        return read_argocd_chart_url(argocd_pin_file)
    if helmfile_path is not None:
        return _read_helmfile_chart_url(helmfile_path)
    return ""


def _chart_pin_release_name(
    helmfile_path: Path | None, argocd_pin_file: Path | None
) -> str:
    """Helm release name from the ArgoCD metadata (preferred) or helmfile."""
    if argocd_pin_file is not None:
        return read_argocd_release_name(argocd_pin_file)
    if helmfile_path is not None:
        return _read_helmfile_release_name(helmfile_path)
    return ""


def _chart_pin_write(
    helmfile_path: Path | None,
    argocd_pin_file: Path | None,
    current: str,
    new_pin: str,
) -> tuple[list[Path], list[Path]]:
    """Write the new chart pin to the ArgoCD metadata (preferred) or helmfile.

    Returns ``(written, skipped)``.

    Fans out across EVERY marker dir of the component, not just the
    first-match primary. A component enrolled on two delivery tracks carries
    one metadata file per cluster; bumping only the primary leaves the other
    cluster pinned to the old chart and the two silently diverge — silently,
    because the read path is first-match and would keep reporting the bumped
    version. Note this template does NOT read ``CONFIG.ARGOCD_PIN_FILES``
    (that is the ``argocd-pin`` template's explicit-list mechanism), so
    auto-discovery is the only thing standing between a two-track component
    and that drift.

    ``update_argocd_chart_version`` rewrites only when the file's pin equals
    ``current``, so a marker deliberately held at another version is left
    alone and returned in ``skipped`` for the caller to surface rather than
    being force-matched.
    """
    if argocd_pin_file is not None:
        # argocd_pin_file is <chart_dir>/<marker>/<release>.yaml, so its
        # grandparent is the component dir holding every marker. The primary
        # is ALWAYS a target and leads the list: re-discovery is a convenience
        # for finding its siblings, never a precondition for writing the file
        # we were handed (a caller that passes a path outside the marker
        # layout must still get its bump, not a silent no-op).
        targets = [argocd_pin_file]
        for f in _detect_argocd_pin_files(argocd_pin_file.parent.parent):
            if f != argocd_pin_file:
                targets.append(f)
        written: list[Path] = []
        skipped: list[Path] = []
        for f in targets:
            if update_argocd_chart_version(f, current, new_pin):
                written.append(f)
            else:
                skipped.append(f)
        return written, skipped
    assert helmfile_path is not None
    _update_helmfile_chart_pin(helmfile_path, new_pin)
    return [helmfile_path], []


def _require_chart_source_configured(config: dict) -> None:
    """Exit 1 with bash-equivalent diagnostics if chart-pin track is unwired."""
    if not config.get("CHART_SOURCE_TYPE"):
        print("ERROR: chart pin tracking is not configured for this component.")
        print("       Set CHART_SOURCE_TYPE / CHART_SOURCE_REPO / CHART_NAME in the")
        print("       CONFIG block of upgrade.py to enable --check-chart /")
        print("       --upgrade-chart.")
        raise SystemExit(1)
    if not config.get("CHART_SOURCE_REPO") or not config.get("CHART_NAME"):
        cst = config["CHART_SOURCE_TYPE"]
        print("ERROR: CHART_SOURCE_REPO and CHART_NAME must both be set when")
        print(f"       CHART_SOURCE_TYPE='{cst}'.")
        raise SystemExit(1)


def _do_check_chart(
    config: dict,
    helmfile_path: Path | None,
    helmfile_name: str,
    argocd_pin_file: Path | None,
) -> int:
    """Report current chart pin vs. latest publisher release (read-only)."""
    _require_chart_source_configured(config)
    chart_name = config["CHART_NAME"]
    chart_source_type = config["CHART_SOURCE_TYPE"]
    chart_source_repo = config["CHART_SOURCE_REPO"]
    pin_label = _chart_pin_label(helmfile_name, argocd_pin_file)

    print("================================================")
    print(f" Chart pin check — {chart_name}")
    print("================================================")
    print()

    current = _chart_pin_current(helmfile_path, argocd_pin_file)
    if not current:
        print(f"  ERROR: could not read chart pin from {pin_label}.")
        return 1
    print(f"  Current pin ({pin_label}): {current}")

    print(
        f"  Querying {chart_source_type} for {chart_source_repo} "
        f"(prefix '{chart_name}-*')..."
    )
    latest = _fetch_latest_chart_version(
        chart_source_type, chart_source_repo, chart_name
    )
    if not latest:
        print()
        print(f"  ERROR: no matching release found in {chart_source_repo}.")
        print(f"  Verify CHART_NAME='{chart_name}' matches the release tag prefix.")
        return 1
    print(f"  Latest upstream:             {latest}")
    print()

    if current == latest:
        print("  Status: OK — chart pin is up to date.")
    else:
        cmp = semver_compare(current, latest)
        if cmp == 1:
            print(
                f"  Status: AHEAD — local pin ({current}) is newer than the latest"
            )
            print(f"          published release ({latest}). Probably a manual override.")
        else:
            print(f"  Status: UPDATE AVAILABLE — {current} -> {latest}")
            print(
                f"  Release notes: https://github.com/{chart_source_repo}/releases/tag/"
                f"{chart_name}-{latest}"
            )
            print()
            print("  To preview:  ./upgrade.py --upgrade-chart --dry-run")
            print("  To apply:    ./upgrade.py --upgrade-chart")
    print()
    return 0


def _do_upgrade_chart(
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    helmfile_path: Path | None,
    helmfile_name: str,
    argocd_pin_file: Path | None,
    dry_run: bool,
    target_chart_version: str,
    keep_backups: int,
) -> int:
    """Bump the OCI chart pin with render-diff preview + backup.

    5 numbered steps. For a helmfile-backed component the pin file is backed
    up under a ``-chart``-suffixed dir so the rollback flow can distinguish
    it from a stack bump. For an ArgoCD-managed component the pin lives in
    git (``argocd/<release>.yaml``), so the write goes straight there and
    git history is the rollback path — no backup dir is created.
    """
    _require_chart_source_configured(config)
    pin_label = _chart_pin_label(helmfile_name, argocd_pin_file)

    current = _chart_pin_current(helmfile_path, argocd_pin_file)
    if not current:
        print(f"ERROR: could not read chart pin from {pin_label}.")
        return 1

    chart_name = config["CHART_NAME"]
    chart_source_repo = config["CHART_SOURCE_REPO"]
    chart_source_type = config["CHART_SOURCE_TYPE"]
    component_label = config["COMPONENT_LABEL"]
    values_file = config["VALUES_FILE"]

    print("================================================")
    print(f" Chart pin upgrade — {chart_name}")
    if dry_run:
        print(" Mode: DRY-RUN (no files will be changed)")
    print("================================================")
    print()

    print("[Step 1/5] Resolving target chart version...")
    if target_chart_version:
        target = target_chart_version
        print(f"  Using explicit target: {target}")
    else:
        target = _fetch_latest_chart_version(
            chart_source_type, chart_source_repo, chart_name
        )
        if not target:
            print(
                f"  ERROR: no matching release found in {chart_source_repo} "
                f"(prefix '{chart_name}-*')."
            )
            return 1
        print(f"  Current pin:     {current}")
        print(f"  Latest upstream: {target}")

    if current == target:
        print()
        print("  Already up to date. Nothing to do.")
        return 0

    print()
    print("[Step 2/5] Pulling charts to a scratch directory...")
    helm_path = shutil.which("helm")
    if not helm_path:
        print("  ERROR: helm not found on PATH. Install helm to use --upgrade-chart.")
        return 1
    chart_url = _chart_pin_url(helmfile_path, argocd_pin_file)
    if not chart_url:
        print(f"  ERROR: could not read chart URL from {pin_label}.")
        return 1
    print(f"  Chart: {chart_url}")

    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        cur_dir = scratch_path / "current"
        tgt_dir = scratch_path / "target"
        cur_dir.mkdir()
        tgt_dir.mkdir()

        for ver, dest in ((current, cur_dir), (target, tgt_dir)):
            rc = subprocess.run(
                [
                    "helm", "pull", chart_url, "--version", ver,
                    "-d", str(dest), "--untar",
                ],
                capture_output=True,
                check=False,
            )
            if rc.returncode != 0:
                print(f"  ERROR: helm pull failed for {chart_url}@{ver}.")
                print("         Check that the version is published and accessible.")
                return 1
        print(f"  Pulled {current} and {target}.")

        print()
        print(f"[Step 3/5] Rendering both charts with {values_file} and diffing...")
        release_name = (
            _chart_pin_release_name(helmfile_path, argocd_pin_file)
            or component_label
        )

        cur_chart = _find_chart_root(cur_dir)
        tgt_chart = _find_chart_root(tgt_dir)
        if not cur_chart or not tgt_chart:
            print("  ERROR: could not locate unpacked Chart.yaml. helm pull layout unexpected.")
            return 1

        cur_render = scratch_path / "current-render.yaml"
        tgt_render = scratch_path / "target-render.yaml"
        values_path = chart_dir / values_file

        cur_proc = subprocess.run(
            ["helm", "template", release_name, str(cur_chart), "-f", str(values_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if cur_proc.returncode != 0:
            print(f"  ERROR: helm template failed on current chart ({current}).")
            print("  Details:")
            for line in (cur_proc.stderr or "").splitlines():
                print(f"    {line}")
            return 1
        cur_render.write_text(cur_proc.stdout)

        tgt_proc = subprocess.run(
            ["helm", "template", release_name, str(tgt_chart), "-f", str(values_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if tgt_proc.returncode != 0:
            print()
            print(f"  ERROR: helm template failed on target chart ({target}). Possible values")
            print("         schema breakage or a mandatory new field. Details:")
            for line in (tgt_proc.stderr or "").splitlines():
                print(f"    {line}")
            print()
            print("  Review the chart's release notes and values changes:")
            print(
                f"    https://github.com/{chart_source_repo}/releases/tag/"
                f"{chart_name}-{target}"
            )
            return 1
        tgt_render.write_text(tgt_proc.stdout)

        diff_proc = subprocess.run(
            ["diff", "-u", str(cur_render), str(tgt_render)],
            capture_output=True,
            text=True,
            check=False,
        )
        diff_out = diff_proc.stdout
        if not diff_out:
            print(
                "  Rendered manifests are identical (label-only or pure refactor chart bump)."
            )
        else:
            diff_lines = diff_out.count("\n")
            print(f"  Rendered manifest diff ({diff_lines} lines):")
            print("  ---------------------------------------------")
            for line in diff_out.splitlines():
                print(f"  | {line}")
            print("  ---------------------------------------------")

        print()
        print("[Step 4/5] Applying chart pin update...")
        if dry_run:
            print(
                f"  [DRY-RUN] Would bump {pin_label} chart pin: {current} -> {target}"
            )
            if argocd_pin_file is None:
                timestamp = now_timestamp()
                print(
                    f"  [DRY-RUN] Would back up {helmfile_name} to "
                    f"backup/{timestamp}-chart/"
                )
            else:
                print(
                    "  [DRY-RUN] ArgoCD metadata is git-tracked; git history is "
                    "the backup."
                )
            print()
            print("  To apply: ./upgrade.py --upgrade-chart")
            if target_chart_version:
                print(f"  To apply: ./upgrade.py --upgrade-chart --chart-version {target_chart_version}")
            return 0

        print()
        confirm = input(f"  Apply chart pin update {current} -> {target}? [y/N]: ").strip()
        if not confirm.lower().startswith("y"):
            print("  Aborted.")
            return 1

        if argocd_pin_file is None:
            timestamp = now_timestamp()
            bdir = backup_dir / f"{timestamp}-chart"
            bdir.mkdir(parents=True, exist_ok=True)
            assert helmfile_path is not None
            (bdir / helmfile_name).write_text(helmfile_path.read_text())
            print(f"  Backed up {helmfile_name} to: backup/{timestamp}-chart/")

        written, skipped = _chart_pin_write(
            helmfile_path, argocd_pin_file, current, target
        )
        for f in written:
            label = (
                f"{f.parent.name}/{f.name}" if argocd_pin_file is not None
                else helmfile_name
            )
            print(f"  Updated {label} (chart version: {current} -> {target})")
        for f in skipped:
            print(
                f"  WARNING: {f.parent.name}/{f.name} was NOT bumped — its pin "
                f"is not {current}. Reconcile it by hand."
            )

    if argocd_pin_file is None:
        auto_prune_backups(backup_dir, keep_backups)

    print()
    print("[Step 5/5] Chart pin bump complete.")
    print()
    print("================================================")
    print(f" Chart pin bump complete! ({current} -> {target})")
    print()
    print(
        f" Release notes: https://github.com/{chart_source_repo}/releases/tag/"
        f"{chart_name}-{target}"
    )
    print()
    print(" Next steps:")
    if argocd_pin_file is not None:
        print(f"   1. Review: git diff {pin_label}")
        print("   2. Commit + push — ArgoCD auto-sync applies the new chart version.")
        print(f"   3. Watch CR: kubectl --context <ctx> -n <ns> get {component_label} -w")
    else:
        print("   1. Run: helmfile diff")
        print("   2. Run: helmfile apply")
        print(f"   3. Watch CR: kubectl --context <ctx> -n <ns> get {component_label} -w")
    print()
    print(" To rollback the chart pin:")
    if argocd_pin_file is not None:
        print(f"   git restore {pin_label}   # ArgoCD auto-sync reverts the cluster")
    else:
        print("   ./upgrade.py --rollback   # pick the *-chart timestamp")
    print("================================================")
    return 0


def _find_chart_root(parent: Path) -> Path | None:
    """Return the first directory under ``parent`` containing ``Chart.yaml``."""
    for sub in sorted(parent.iterdir()):
        if not sub.is_dir():
            continue
        if (sub / "Chart.yaml").is_file():
            return sub
        for nested in sorted(sub.iterdir()):
            if nested.is_dir() and (nested / "Chart.yaml").is_file():
                return nested
    return None


# =============================================================
# Main stack-version flow (Steps 1-7)
# =============================================================

def _stack_next_steps(
    component_label: str, helmfile_name: str, argocd_pin_file: Path | None
) -> list[str]:
    watch = f"Watch CR: kubectl --context <ctx> -n <ns> get {component_label} -w"
    steps = [
        f"   1. Verify the OCI chart pin in {_chart_pin_label(helmfile_name, argocd_pin_file)} "
        f"supports this version."
    ]
    if argocd_pin_file is not None:
        return steps + [
            "   2. Review `git diff`, then commit and push — ArgoCD applies the CR version.",
            f"   3. {watch}",
        ]
    return steps + ["   2. Run: helmfile diff", "   3. Run: helmfile apply", f"   4. {watch}"]


def _stack_upgrade(
    config: dict,
    chart_dir: Path,
    backup_dir: Path,
    helmfile_path: Path | None,
    helmfile_name: str,
    argocd_pin_file: Path | None,
    dry_run: bool,
    target_version: str,
    keep_backups: int,
) -> int:
    """Default Stack/component version flow — 7 numbered steps."""
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

    # Step 1: Read current version.
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
    chart_pin = _chart_pin_current(helmfile_path, argocd_pin_file)
    if chart_pin:
        pin_label = _chart_pin_label(helmfile_name, argocd_pin_file)
        print(f"  OCI chart pin ({pin_label}): {chart_pin}  (bump manually if needed)")

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

    if current_version == latest_version:
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

    # Step 5: Compatibility checks + dependency CR + major bump warning.
    print()
    print("[Step 5/7] Compatibility checks")
    print(
        f"  * Verify the currently installed operator supports {component_label} {latest_version}."
    )
    print("  * For Stack major bumps (e.g. 8.x -> 9.x) review breaking changes before applying.")
    print(
        f"  * Verify the OCI chart pin in {_chart_pin_label(helmfile_name, argocd_pin_file)} "
        f"supports this component version."
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

    # Step 6: Dry-run exit / backup.
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
    print(f"  Backed up to: backup/{timestamp}/")
    for f in sorted(bdir.iterdir()):
        print(f"    - {f.name}")

    # Step 7: Apply.
    print()
    print("[Step 7/7] Applying version update...")
    update_yaml_value(values_path, version_key, latest_version)
    print(
        f"  Updated {values_file} ({version_key}: {current_version} -> {latest_version})"
    )

    auto_prune_backups(backup_dir, keep_backups)

    print()
    print("================================================")
    print(f" Upgrade complete! ({current_version} -> {latest_version})")
    print()
    print(f" Changelog: {changelog_url}")
    print()
    print(" Next steps:")
    for line in _stack_next_steps(component_label, helmfile_name, argocd_pin_file):
        print(line)
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
        "Tracks the upstream version of a Custom Resource's component (Stack/app version)"
    )
    print(
        f"and bumps the version field inside {config.get('VALUES_FILE', 'values/dev.yaml')}."
        " When CHART_SOURCE_TYPE is set,"
    )
    print(
        "the --check-chart / --upgrade-chart commands also track the OCI chart pin in"
    )
    print("helmfile.yaml (publisher release tag `<CHART_NAME>-<semver>`).")
    print()
    print("Commands (Stack/component version — default track):")
    print("  (default)              Check latest Stack version and upgrade VALUES_FILE")
    print("  --version <VER>        Upgrade Stack to a specific version")
    print("  --dry-run              Preview Stack changes only (no files will be modified)")
    print()
    print("Commands (OCI chart pin — requires CHART_SOURCE_TYPE set):")
    print("  --check-chart          Report current chart pin vs. latest upstream (read-only)")
    print(
        "  --upgrade-chart        Download both chart versions, diff the rendered manifests,"
    )
    print("                         prompt, then bump helmfile.yaml.version")
    print("  --chart-version <VER>  Target a specific chart version with --upgrade-chart")
    print()
    print("Commands (shared):")
    print(
        "  --rollback             Restore from a previous backup (auto-detects stack vs chart)"
    )
    print("  --list-backups         List available backups")
    print(
        f"  --cleanup-backups      Keep only the last {keep_backups} backups, remove older ones"
    )
    print("  -h, --help             Show this help message")
    return 0


def _parse_argv(
    argv: list[str], config: dict, keep_backups: int
) -> tuple[str, str, str, bool, int]:
    """Return ``(mode, target_version, target_chart_version, dry_run, early_exit_code)``."""
    mode = "stack"
    target_version = ""
    target_chart_version = ""
    dry_run = False
    chart_action = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            _usage(config, keep_backups)
            return mode, target_version, target_chart_version, dry_run, 0
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
        if arg == "--check-chart":
            chart_action = "check"
            i += 1
            continue
        if arg == "--upgrade-chart":
            chart_action = "upgrade"
            i += 1
            continue
        if arg == "--chart-version":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print("ERROR: --chart-version requires a version number")
                return mode, target_version, target_chart_version, dry_run, 1
            target_chart_version = argv[i + 1]
            i += 2
            continue
        if arg == "--version":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print("ERROR: --version requires a version number")
                return mode, target_version, target_chart_version, dry_run, 1
            target_version = argv[i + 1]
            i += 2
            continue
        print(f"Unknown option: {arg}")
        print()
        _usage(config, keep_backups)
        return mode, target_version, target_chart_version, dry_run, 1

    if chart_action == "check":
        mode = "check-chart"
    elif chart_action == "upgrade":
        mode = "upgrade-chart"
    return mode, target_version, target_chart_version, dry_run, -1


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
    helmfile_path, helmfile_name = detect_helmfile(chart_dir)
    # Migrated CR components (elasticsearch / kibana) have no helmfile — the
    # chart-pin SSOT moved to argocd/<release>.yaml. Resolve it ONLY when the
    # helmfile is gone, so non-migrated (-aws) components stay byte-identical.
    argocd_pin_file = (
        _detect_argocd_pin_file(chart_dir) if helmfile_path is None else None
    )

    mode, target_version, target_chart_version, dry_run, early_rc = _parse_argv(
        argv, config, keep_backups
    )
    if early_rc != -1:
        return early_rc

    if mode == "list-backups":
        _list_backups(backup_dir, config["VALUES_FILE"], config["VERSION_KEY"])
        return 0
    if mode == "rollback":
        return _do_rollback(config, chart_dir, backup_dir, helmfile_path)
    if mode == "cleanup-backups":
        cleanup_backups(backup_dir, keep_backups)
        return 0
    if mode == "check-chart":
        return _do_check_chart(
            config, helmfile_path, helmfile_name, argocd_pin_file
        )
    if mode == "upgrade-chart":
        return _do_upgrade_chart(
            config,
            chart_dir,
            backup_dir,
            helmfile_path,
            helmfile_name,
            argocd_pin_file,
            dry_run,
            target_chart_version,
            keep_backups,
        )
    return _stack_upgrade(
        config,
        chart_dir,
        backup_dir,
        helmfile_path,
        helmfile_name,
        argocd_pin_file,
        dry_run,
        target_version,
        keep_backups,
    )
