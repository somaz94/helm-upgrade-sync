"""Shared ArgoCD-metadata helpers for the ``argocd-pin`` upgrade template.

Counterpart to :mod:`_common_helmfile`. Where the helmfile-flavored
templates pin a chart version in ``helmfile.yaml``, the ArgoCD-managed
components migrated to the app-of-apps store the version SSOT in a
per-release ArgoCD metadata file: ``<component>/argocd/<release>.yaml``,
under a nested ``chart:`` block::

    chart:
      repoURL: ghcr.io/somaz94/charts   # registered OCI repo
      name: ghost
      version: "0.1.6"

The infra-applicationset git-files generator (argocd-applicationset repo)
reads that ``chart.version`` and ArgoCD auto-sync applies it, so bumping
this field IS the cluster upgrade — there is no helmfile to rewrite.

``_common.read_yaml_value`` / ``update_yaml_value`` only match column-0
top-level keys, so they cannot reach the indented ``version:`` inside the
``chart:`` block. These helpers add that nested scope while preserving the
same quote-style + inline-comment fidelity (the regex approach is borrowed
from ``external_oci._patch_wrapper_chart_version``, scoped to the block).

Exported helpers — all stdlib-only:

- :func:`read_argocd_chart_version` — value of ``chart.version``.
- :func:`read_argocd_chart_url` — ``oci://<repoURL>/<name>`` for ``helm pull``.
- :func:`read_argocd_release_name` — top-level ``releaseName`` value.
- :func:`update_argocd_chart_version` — in-place flip of one file's pin.
- :func:`update_argocd_pins` — flip a list of files (multi-release).
- :func:`has_argocd_marker` — whether ArgoCD delivers the component at all.
"""

from __future__ import annotations

import re
from pathlib import Path


# A `version:` line, capturing the leading indent + key (so the original
# spacing is preserved) and the trailing value. Only matches indented
# lines (at least one leading space) — a column-0 `version:` is never the
# chart pin.
_CHART_VERSION_RE = re.compile(r"^([ \t]+version:[ \t]+)(.*)$")

# An indented `<field>: <value>` line inside the chart block (used to read
# repoURL / name for the OCI chart URL).
_CHART_FIELD_RE = re.compile(r"^[ \t]+(\w+):[ \t]+(.*)$")

# A column-0 `releaseName: <value>` line (top-level, not in the chart block).
_RELEASE_NAME_RE = re.compile(r"^releaseName:[ \t]+(.*)$")


def _strip_value(val: str) -> str:
    """Strip a trailing inline ``# comment`` and matching surrounding quotes."""
    val = re.sub(r"\s+#.*$", "", val).rstrip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1]
    return val


def _iter_chart_block_lines(text: str):
    """Yield ``(index, line)`` for every line inside the top-level
    ``chart:`` block.

    The block opens on a column-0 ``chart:`` line and runs until the next
    column-0 (non-indented, non-blank) line. Mirrors the YAML block-scoping
    the generator relies on without pulling in a YAML parser (stdlib-only,
    same discipline as :mod:`_common_helmfile`).
    """
    lines = text.splitlines(keepends=True)
    in_block = False
    for idx, line in enumerate(lines):
        stripped_eol = line.rstrip("\n")
        if not in_block:
            if stripped_eol == "chart:" or stripped_eol.startswith("chart:"):
                # Only a bare top-level `chart:` mapping opens the block.
                if re.match(r"^chart:[ \t]*$", stripped_eol):
                    in_block = True
            continue
        # Inside the block: a blank line stays in the block; a column-0
        # non-space character closes it.
        if stripped_eol and not stripped_eol[0].isspace():
            in_block = False
            continue
        yield idx, line


def read_argocd_chart_version(argocd_file: Path) -> str:
    """Return the ``chart.version`` value, or empty string.

    Strips matching surrounding quotes and any trailing ``# comment``.
    Missing file / missing key returns empty (bash parity — caller treats
    empty as "could not determine current version").
    """
    if not argocd_file.is_file():
        return ""
    text = argocd_file.read_text()
    for _idx, line in _iter_chart_block_lines(text):
        m = _CHART_VERSION_RE.match(line.rstrip("\n"))
        if not m:
            continue
        val = m.group(2)
        val = re.sub(r"\s+#.*$", "", val).rstrip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        return val
    return ""


def _read_chart_block_field(text: str, field: str) -> str:
    """Return the first indented ``<field>:`` value inside the chart block."""
    for _idx, line in _iter_chart_block_lines(text):
        m = _CHART_FIELD_RE.match(line.rstrip("\n"))
        if m and m.group(1) == field:
            return _strip_value(m.group(2))
    return ""


def read_argocd_chart_url(argocd_file: Path) -> str:
    """Return the OCI chart URL ``oci://<repoURL>/<name>``, or empty string.

    Built from the chart block's ``repoURL`` + ``name``. The ArgoCD metadata
    stores ``repoURL`` without a scheme (ArgoCD appends the chart name at pull
    time); this prepends ``oci://`` so the URL is directly usable by
    ``helm pull``. A ``repoURL`` that already carries a scheme is left as-is.
    """
    if not argocd_file.is_file():
        return ""
    text = argocd_file.read_text()
    repo = _read_chart_block_field(text, "repoURL")
    name = _read_chart_block_field(text, "name")
    if not repo or not name:
        return ""
    if "://" not in repo:
        repo = f"oci://{repo}"
    return f"{repo}/{name}"


def read_argocd_release_name(argocd_file: Path) -> str:
    """Return the top-level ``releaseName`` value, or empty string."""
    if not argocd_file.is_file():
        return ""
    for raw in argocd_file.read_text().splitlines():
        m = _RELEASE_NAME_RE.match(raw)
        if m:
            return _strip_value(m.group(1))
    return ""


def update_argocd_chart_version(
    argocd_file: Path, current_version: str, latest_version: str
) -> int:
    """Flip ``chart.version`` from ``current_version`` to ``latest_version``.

    Returns 1 if a pin was rewritten, 0 otherwise (missing file, missing
    key, or the value did not match ``current_version`` — never a blind
    fallthrough replacement). Preserves quote style (``"x"`` stays double,
    ``'x'`` stays single, bare stays bare) and any trailing inline comment.
    """
    if not argocd_file.is_file():
        return 0
    text = argocd_file.read_text()
    lines = text.splitlines(keepends=True)
    cv_dq = f'"{current_version}"'
    cv_sq = f"'{current_version}'"
    rewritten = 0

    for idx, line in _iter_chart_block_lines(text):
        if rewritten:
            break
        stripped_eol = line.rstrip("\n")
        ending = line[len(stripped_eol):]
        m = _CHART_VERSION_RE.match(stripped_eol)
        if not m:
            continue
        prefix = m.group(1)
        rest = m.group(2)
        # Split any trailing inline comment so we only touch the value.
        comment = ""
        cmatch = re.search(r"(\s+#.*)$", rest)
        if cmatch:
            comment = cmatch.group(1)
            value = rest[: cmatch.start()]
        else:
            value = rest
        value = value.rstrip()
        if value == cv_dq:
            new_value = f'"{latest_version}"'
        elif value == cv_sq:
            new_value = f"'{latest_version}'"
        elif value == current_version:
            new_value = latest_version
        else:
            # Pin is not at the expected current version — leave untouched.
            continue
        lines[idx] = f"{prefix}{new_value}{comment}{ending}"
        rewritten = 1

    if rewritten:
        argocd_file.write_text("".join(lines))
    return rewritten


def update_argocd_pins(
    argocd_files: list[Path], current_version: str, latest_version: str
) -> int:
    """Flip ``chart.version`` across several release files (multi-release).

    Returns the total number of files rewritten. Used by components whose
    single ``upgrade.py`` tracks more than one ArgoCD release at the same
    version (e.g. gitlab-runner build-image + deploy-image, valkey dev + qa).
    Files already off ``current_version`` (or missing) contribute 0.
    """
    total = 0
    for f in argocd_files:
        total += update_argocd_chart_version(f, current_version, latest_version)
    return total


def has_argocd_marker(chart_dir: Path) -> bool:
    """True when any ``argocd*/`` marker dir exists — ArgoCD delivers the component.

    Such a component's helmfile, if one is still on disk, is a render reference or a
    new-cluster bootstrap recipe, so a rollback must never write one back. A marker
    parked under ``_pending/`` does not count: nothing delivers it yet. The marker only
    stands in for "the helmfile is not the deploy path", which a self-managed ArgoCD
    (bootstrapped from its own helmfile) breaks, so only templates whose consumers are
    all ArgoCD-delivered use it.
    """
    return any(p.is_dir() for p in chart_dir.glob("argocd*"))
