"""Lightweight YAML scalar readers for the sync orchestrator.

Extracted from ``check-versions.py`` so the
parser surface used to seed the status table can be unit-tested and
shared with sister tools.

Two helpers, both regex-based (no PyYAML dep — keeps the sync
orchestrator's runtime to stdlib + the helmfile-tools image):

- :func:`read_yaml_value` — top-level scalar field reader. Handles
  optional surrounding single/double quotes and a trailing
  ``# inline comment``.
- :func:`read_helmfile_chart_pin` — pulls the OCI chart pin out of
  ``helmfile.yaml.gotmpl`` (preferred — ``$chartVersion := "X"`` hoist)
  or ``helmfile.yaml`` (fallback — indented release-level ``version:``
  line, skipping templated values).

Note: there is a sibling :func:`upgrade_core._common.read_yaml_value`
with a similar shape but slightly different comment-handling regex,
used inside per-component ``upgrade.py`` scripts. The two cannot be
collapsed without verifying the comment-handling parity contract
across all consumers — out of scope for the current sync orchestrator
split.

Stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path


def read_yaml_value(yaml_file: Path, key: str) -> str:
    """Top-level scalar read: ``<key>: value`` (with optional surrounding
    quotes + trailing comment). Mirrors the awk-based helper in bash.
    """
    pattern = re.compile(
        r'^' + re.escape(key) + r':[ \t]*(.*?)(?:[ \t]+#.*)?$'
    )
    try:
        with yaml_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                m = pattern.match(line.rstrip("\n"))
                if m:
                    val = m.group(1).strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        val = val[1:-1]
                    return val
    except OSError:
        pass
    return ""


# Helmfile chart-pin hoist (in .gotmpl templates):
#   {{- $chartVersion := "X.Y.Z" -}}
_HELMFILE_HOIST_PIN = re.compile(r'\$chartVersion[ \t]*:=[ \t]+"([^"]+)"')

# Helmfile release-level ``  version: X.Y.Z`` (indented). Mirrors the
# awk filter that skips lines containing ``{{`` (templated values).
_HELMFILE_RELEASE_PIN = re.compile(r'^[ \t]+version:[ \t]+(.+)$')


# Indented ``  version: X`` line inside an ArgoCD metadata ``chart:`` block.
_ARGOCD_CHART_VERSION = re.compile(r'^[ \t]+version:[ \t]+(.+)$')

# ArgoCD metadata marker dirs, in priority order: ``argocd/`` (on-prem)
# and ``argocd-aws/`` (AWS). A multi-track component carries several, all
# pinned to the SAME version (ARGOCD_PIN_FILES flips them together), so
# first-match is safe here. A missing dir is a no-op; dropping a marker makes
# the probe return "" SILENTLY.
_ARGOCD_MARKER_DIRS = ("argocd", "argocd-aws")


def read_argocd_chart_version(component_dir: Path) -> str:
    """Read ``chart.version`` from the component's ArgoCD metadata.

    Used by the ``argocd-pin`` template, whose version SSOT lives in
    ``<component>/<marker>/<release>.yaml`` (nested under a top-level
    ``chart:`` block), not in a helmfile. Two marker dirs exist by
    convention: ``argocd/`` for on-prem components and ``argocd-aws/``
    for AWS components (a disjoint marker dir avoids a cross-cluster
    ``infra-<releaseName>`` app-name collision — the on-prem and AWS
    infra-applicationsets glob the SAME repo). A component carries one
    marker per track it is delivered to; when it carries both they pin
    the same chart version, so the first match is authoritative either
    way. Scans the marker dir's ``*.yaml`` in sorted order and returns
    the first ``chart.version`` found.

    Multi-release components list several files (e.g. gitlab-runner
    build-image / deploy-image / old-build-deploy-image); the tracked
    primary must sort first so this probe reports the tracked version
    (build-image precedes old-build-deploy-image; an explicitly pinned
    "old-" release sorts last). Returns empty when no file has a pin.
    """
    for marker in _ARGOCD_MARKER_DIRS:
        argocd_dir = component_dir / marker
        if not argocd_dir.is_dir():
            continue
        for argocd_file in sorted(argocd_dir.glob("*.yaml")):
            in_chart_block = False
            try:
                with argocd_file.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        line = raw.rstrip("\n")
                        if not in_chart_block:
                            if re.match(r'^chart:[ \t]*$', line):
                                in_chart_block = True
                            continue
                        if line and not line[0].isspace():
                            in_chart_block = False
                            continue
                        m = _ARGOCD_CHART_VERSION.match(line)
                        if m:
                            val = m.group(1).strip()
                            if " #" in val:
                                val = val[: val.index(" #")].strip()
                            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                                val = val[1:-1]
                            return val
            except OSError:
                continue
    return ""


def read_helmfile_chart_pin(component_dir: Path) -> str:
    """Read the OCI chart pin from ``helmfile.yaml.gotmpl`` preferred,
    else ``helmfile.yaml``.

    For ``.gotmpl`` files the ``$chartVersion := "X"`` hoist is preferred
    — falls back to the first indented release-level ``version:`` line
    (skipping templated values that still contain ``{{``).
    """
    gotmpl = component_dir / "helmfile.yaml.gotmpl"
    yaml = component_dir / "helmfile.yaml"
    if gotmpl.is_file():
        target = gotmpl
    elif yaml.is_file():
        target = yaml
    else:
        return ""
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                m = _HELMFILE_HOIST_PIN.search(line)
                if m:
                    return m.group(1)
                m = _HELMFILE_RELEASE_PIN.match(line)
                if m:
                    val = m.group(1).strip().strip('"').strip("'")
                    # Skip trailing comment after the value.
                    if " #" in val:
                        val = val[: val.index(" #")].strip()
                    if "{{" in val:
                        continue
                    return val
    except OSError:
        pass
    return ""
