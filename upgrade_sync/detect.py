"""Content-based template detection.

Ported from the bash ``detect_template`` helper. Used only by callers that
need to identify a template by looking at file contents (e.g. when the
``# upgrade-template:`` header is missing). All current 25 consumers carry
the header; this helper is kept available for forward-compat tooling.
"""

from __future__ import annotations

import re
from pathlib import Path

# Python consumers name their template by the upgrade_core module they import.
_PY_ENTRYPOINT_RE = re.compile(r"^from upgrade_core\.([a-z_]+) import run\b", re.MULTILINE)
_KNOWN_TEMPLATES = frozenset({
    "ansible-github-release",
    "argocd-pin",
    "external-oci",
    "external-oci-cr-version",
    "external-oci-with-mirror",
    "external-standard",
    "external-with-image-tag",
    "local-cr-version",
    "local-with-templates",
})

# Legacy ``.sh`` probes. The order matters — every cascade in detect_template
# falls through to the next on a miss.
_OCI_CHART_RE = re.compile(r'^HELM_CHART=("|\')?oci://', re.MULTILINE)
_DO_MIRROR_RE = re.compile(r"^do_mirror\(\)", re.MULTILINE)
_GITHUB_REPO_RE = re.compile(r"^GITHUB_REPO=", re.MULTILINE)
_VERSION_SOURCE_RE = re.compile(r"^VERSION_SOURCE=", re.MULTILINE)
_MIRROR_CHART_VERSION_RE = re.compile(r"^MIRROR_CHART_VERSION=", re.MULTILINE)
_CUSTOM_TEMPLATES_RE = re.compile(r"^CUSTOM_TEMPLATES=", re.MULTILINE)
_IMAGE_TAG_RE = re.compile(r"Update image tags in values files")


def detect_template(upgrade_script: Path) -> str:
    """Return the template name implied by the file's body.

    A Python consumer is identified by its ``from upgrade_core.<module> import
    run`` line (``<module>`` with ``_`` → ``-`` is the template name). Legacy
    ``.sh`` bodies fall through to the content cascade:

    1. ``HELM_CHART="oci://..."`` + ``do_mirror()`` → ``external-oci-with-mirror``
    2. ``HELM_CHART="oci://..."`` alone           → ``external-oci``
    3. ``GITHUB_REPO=``                           → ``ansible-github-release``
    4. ``VERSION_SOURCE=`` + ``MIRROR_CHART_VERSION=`` → ``local-cr-version``
    5. ``VERSION_SOURCE=`` alone                  → ``external-oci-cr-version``
    6. ``CUSTOM_TEMPLATES=``                      → ``local-with-templates``
    7. ``Update image tags in values files``      → ``external-with-image-tag``
    8. fallback                                   → ``external-standard``
    """
    body = upgrade_script.read_text(encoding="utf-8")
    m = _PY_ENTRYPOINT_RE.search(body)
    if m and m.group(1).replace("_", "-") in _KNOWN_TEMPLATES:
        return m.group(1).replace("_", "-")
    if _OCI_CHART_RE.search(body):
        if _DO_MIRROR_RE.search(body):
            return "external-oci-with-mirror"
        return "external-oci"
    if _GITHUB_REPO_RE.search(body):
        return "ansible-github-release"
    if _VERSION_SOURCE_RE.search(body):
        if _MIRROR_CHART_VERSION_RE.search(body):
            return "local-cr-version"
        return "external-oci-cr-version"
    if _CUSTOM_TEMPLATES_RE.search(body):
        return "local-with-templates"
    if _IMAGE_TAG_RE.search(body):
        return "external-with-image-tag"
    return "external-standard"
