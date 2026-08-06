"""External OCI Helm chart upgrade runner (external-oci template).

Thin extension of :mod:`external_standard` for charts distributed via
OCI registries (ghcr.io, Docker Hub OCI, ECR). The 7-step main flow is
reused via three hook injection points:

  - ``fetch_latest_hook`` — replaces Step 2 with a GitHub Releases API
    lookup. Supports both single-chart repos (``GITHUB_TAG_PREFIX="v"``,
    hit ``/releases/latest``) and multi-chart repos
    (e.g. ``somaz94/helm-charts`` with ``keycloak-cr-``, scan
    ``/releases?per_page=30`` for the first matching prefix).
  - ``chart_write_hook`` — when ``WRAPPER_CHART_YAML=true``, patches only
    the ``version:`` line of ``Chart.yaml`` and skips ``values.yaml`` +
    ``values.schema.json`` (the component owns its own ``values/<env>.yaml``).
    Otherwise = ``external_standard`` baseline.
  - ``helmfile_pin_hook`` — when ``HELMFILE_TRACKED_CHART`` is set,
    scopes the helmfile pin rewrite to the release block whose
    ``chart:`` line contains that substring (multi-release helmfile
    where sibling releases at the same version must stay untouched).
    Otherwise = ``external_standard`` baseline via :func:`_common_helmfile.update_helmfile_pins`.

Migrated from the canonical bash template at
``templates/external-oci.sh``.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._common import fetch_latest_release_tag
from ._common_helmfile import update_helmfile_pins
from .external_standard import (
    _default_chart_write,
    run as _run_external_standard,
)


# -----------------------------------------------
# Step 2 — GitHub Releases lookup (replaces helm search)
# -----------------------------------------------

def _fetch_latest_via_github(
    github_repo: str, tag_prefix: str
) -> tuple[str, str]:
    """Return ``(latest_version_found, latest_tag)``.

    ``latest_version_found`` is the chart version after stripping
    ``tag_prefix``. The second tuple member returns the original tag
    (e.g. ``v2.5.1``) so the caller can log it. Both empty on failure.
    """
    latest_tag = fetch_latest_release_tag(github_repo, tag_prefix)
    if not latest_tag:
        return "", ""
    # Strip prefix (default "v"). Empty prefix leaves the tag untouched.
    if tag_prefix and latest_tag.startswith(tag_prefix):
        latest_version_found = latest_tag[len(tag_prefix):]
    else:
        latest_version_found = latest_tag
    return latest_version_found, latest_tag


# -----------------------------------------------
# Step 7 — wrapper-aware Chart.yaml + values write
# -----------------------------------------------

# Match a top-level `version:` line, capturing the prefix (whitespace
# between the colon and the value is preserved).
_VERSION_LINE_RE = re.compile(r"^(version:[ \t]+)(.*)$")


def _patch_wrapper_chart_version(
    chart_yaml: Path, current_version: str, latest_version: str
) -> None:
    """Patch only the `version:` line of a wrapper Chart.yaml.

    Mirrors the bash awk block — preserves quoting style: ``"X.Y.Z"``
    stays quoted, bare ``X.Y.Z`` stays bare. Lines that don't match the
    current_version are left alone (no fallthrough replacement).
    """
    lines: list[str] = []
    patched = False
    cv_quoted = f'"{current_version}"'
    with chart_yaml.open() as f:
        for line in f:
            stripped_eol = line.rstrip("\n")
            ending = line[len(stripped_eol):]
            m = _VERSION_LINE_RE.match(stripped_eol)
            if m is None or patched:
                lines.append(line)
                continue
            prefix = m.group(1)
            rest = m.group(2).rstrip()
            if rest == cv_quoted:
                lines.append(f'{prefix}"{latest_version}"{ending}')
                patched = True
            elif rest == current_version:
                lines.append(f"{prefix}{latest_version}{ending}")
                patched = True
            else:
                lines.append(line)
    chart_yaml.write_text("".join(lines))


def _write_chart_wrapper_aware(
    *,
    chart_dir: Path,
    temp_dir: Path,
    current_version: str,
    latest_version: str,
    latest_app_version: str,
    wrapper_mode: bool,
) -> None:
    """Step 7 chart write override.

    ``wrapper_mode=True`` — local Chart.yaml is component metadata, NOT
    a mirror of the upstream chart. Patch only the ``version:`` line;
    preserve name / description / appVersion / sources. Skip values.yaml
    and values.schema.json (component uses values/<env>.yaml only).

    ``wrapper_mode=False`` — ``external_standard`` baseline: cp Chart.yaml + values.yaml
    + (optional) values.schema.json.
    """
    if wrapper_mode:
        chart_yaml = chart_dir / "Chart.yaml"
        _patch_wrapper_chart_version(chart_yaml, current_version, latest_version)
        print()
        print(
            f"  Updated Chart.yaml (wrapper: version {current_version} "
            f"-> {latest_version}; appVersion preserved)"
        )
        return

    # Non-wrapper path = ``external_standard`` baseline. Reuse the existing helper instead
    # of duplicating the cp + print block.
    _default_chart_write(
        chart_dir=chart_dir,
        temp_dir=temp_dir,
        current_version=current_version,
        latest_version=latest_version,
        latest_app_version=latest_app_version,
    )


# -----------------------------------------------
# Step 7 — tracked-chart scoped helmfile pin rewrite
# -----------------------------------------------

# Patterns for the awk state machine that scopes the rewrite to a
# specific release block in a multi-release helmfile.
_BLOCK_NAME_RE = re.compile(r"^\s*-\s+name:\s+")
_BLOCK_CHART_RE = re.compile(r"^\s*chart:\s+(.+)$")
_BLOCK_VERSION_RE = re.compile(r"^(\s*version:[ \t]+)(.*)$")


def _update_helmfile_pins_tracked_scope(
    helmfile_path: Path,
    current_version: str,
    latest_version: str,
    tracked_chart: str,
) -> int:
    """Rewrite the version pin only inside the release block whose
    ``chart:`` line contains ``tracked_chart`` (substring match).

    Mirrors the bash awk in external-oci.sh — preserves quoting (quoted
    stays quoted, bare stays bare) and only flips lines whose value
    matches ``current_version``.

    Returns the number of pins actually rewritten.
    """
    in_block = False
    rewritten = 0
    cv_quoted = f'"{current_version}"'
    lines: list[str] = []
    with helmfile_path.open() as f:
        for line in f:
            stripped_eol = line.rstrip("\n")
            ending = line[len(stripped_eol):]

            if _BLOCK_NAME_RE.match(stripped_eol):
                in_block = False

            chart_m = _BLOCK_CHART_RE.match(stripped_eol)
            if chart_m is not None:
                chart_value = chart_m.group(1).strip()
                in_block = tracked_chart in chart_value

            if in_block:
                ver_m = _BLOCK_VERSION_RE.match(stripped_eol)
                if ver_m is not None:
                    prefix = ver_m.group(1)
                    rest = ver_m.group(2).rstrip()
                    if rest == cv_quoted:
                        lines.append(f'{prefix}"{latest_version}"{ending}')
                        rewritten += 1
                        continue
                    if rest == current_version:
                        lines.append(f"{prefix}{latest_version}{ending}")
                        rewritten += 1
                        continue
            lines.append(line)

    helmfile_path.write_text("".join(lines))
    return rewritten


def _helmfile_pin_default_or_scoped(
    *,
    helmfile_path: Path,
    helmfile_name: str,
    current_version: str,
    latest_version: str,
    tracked_chart: str,
) -> int:
    """Step 7 helmfile pin override.

    ``tracked_chart`` empty → fall back to the ``external_standard`` baseline (4 sed
    expressions via :func:`_common_helmfile.update_helmfile_pins`).
    Otherwise scope via :func:`_update_helmfile_pins_tracked_scope`.
    """
    if not tracked_chart:
        return update_helmfile_pins(helmfile_path, current_version, latest_version)
    return _update_helmfile_pins_tracked_scope(
        helmfile_path, current_version, latest_version, tracked_chart
    )


# -----------------------------------------------
# Entry-point
# -----------------------------------------------

def run(config: dict, argv: list[str], script_path: str | os.PathLike) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Delegates to :func:`external_standard.run` with three closures that
    capture per-chart CONFIG keys (``GITHUB_REPO`` / ``GITHUB_TAG_PREFIX``
    / ``WRAPPER_CHART_YAML`` / ``HELMFILE_TRACKED_CHART``) and inject
    the OCI-specific Step 2 / Step 7 behavior.
    """
    github_repo = config["GITHUB_REPO"]
    tag_prefix = config.get("GITHUB_TAG_PREFIX", "v")
    wrapper_mode = bool(config.get("WRAPPER_CHART_YAML", False))
    tracked_chart = config.get("HELMFILE_TRACKED_CHART", "") or ""

    def fetch_hook(*, config: dict) -> tuple[str, str]:
        latest_version_found, latest_tag = _fetch_latest_via_github(
            github_repo, tag_prefix
        )
        if not latest_version_found:
            # Match the bash diagnostic for empty/failed fetch — the
            # caller already prints a generic "Failed to fetch" line.
            print(
                f"  (GitHub Releases API: {github_repo} — "
                f"no tag matched prefix '{tag_prefix}')"
            )
            return "", ""
        # This template places the original tag in the "app version" slot purely
        # for the operator log line; the real appVersion is read from
        # the freshly fetched Chart.yaml in Step 3.
        return latest_version_found, latest_tag

    def chart_write(
        *,
        chart_dir: Path,
        temp_dir: Path,
        current_version: str,
        latest_version: str,
        latest_app_version: str,
    ) -> None:
        _write_chart_wrapper_aware(
            chart_dir=chart_dir,
            temp_dir=temp_dir,
            current_version=current_version,
            latest_version=latest_version,
            latest_app_version=latest_app_version,
            wrapper_mode=wrapper_mode,
        )

    def helmfile_pin(
        *,
        helmfile_path: Path,
        helmfile_name: str,
        current_version: str,
        latest_version: str,
    ) -> int:
        return _helmfile_pin_default_or_scoped(
            helmfile_path=helmfile_path,
            helmfile_name=helmfile_name,
            current_version=current_version,
            latest_version=latest_version,
            tracked_chart=tracked_chart,
        )

    return _run_external_standard(
        config,
        argv,
        script_path,
        fetch_latest_hook=fetch_hook,
        chart_write_hook=chart_write,
        helmfile_pin_hook=helmfile_pin,
    )
