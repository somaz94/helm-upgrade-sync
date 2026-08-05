"""External Helm chart upgrade runner (external-with-image-tag template).

This template is a thin extension of :mod:`external_standard`: it reuses
the 7-step main flow byte-for-byte and adds a single post-step that
rewrites ``tag: vX.Y.Z`` occurrences in ``values/*.yaml`` files so the
image tag tracks the new ``appVersion``.

The extension is wired via the ``post_pin_hook`` keyword of
:func:`external_standard.run`, which fires after the helmfile pin rewrite
and before the silent backup prune. The hook receives the values
directory, the active ``--exclude`` patterns, and the freshly resolved
``latest_app_version``.

Migrated from the canonical bash template at
``templates/external-with-image-tag.sh`` during the shell -> python
migration.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._common import is_excluded as _is_excluded
from .external_standard import run as _run_external_standard


# `tag: vX.Y.Z` — the bash template's `grep -oE 'tag: v[0-9]+\.[0-9]+\.[0-9]+'`.
# We capture the bare semver (no leading 'v') to match the bash awk + sed flow:
#   grep -oE ... | head -1 | awk '{print $2}' | sed 's/^v//'
_TAG_RE = re.compile(r"tag: v(\d+\.\d+\.\d+)")


def _rewrite_image_tags(
    *,
    values_dir: Path,
    exclude_patterns: str,
    latest_app_version: str,
) -> None:
    """Rewrite ``tag: vX.Y.Z`` occurrences across each values file.

    Per the bash template:
      - Skip the whole block if ``latest_app_version`` is empty.
      - For each ``values/*.yaml`` (excluding patterns matched by
        ``--exclude``), detect the FIRST ``tag: vX.Y.Z`` match (bash
        ``head -1``) and read the captured semver as ``VALUES_TAG``.
      - When ``VALUES_TAG`` differs from ``latest_app_version``, do a
        literal substring replace of ``tag: v$VALUES_TAG`` →
        ``tag: v$LATEST_APP_VERSION`` across the file and report the
        count.

    The literal substring replace mirrors bash ``sed "s/tag: vX/tag: vY/g"``
    1:1 — yaml structure is not parsed, preserving byte parity.
    """
    if not latest_app_version:
        return
    if not values_dir.is_dir():
        return
    for values_file in sorted(values_dir.glob("*.yaml")):
        if not values_file.is_file():
            continue
        if _is_excluded(values_file.name, exclude_patterns):
            continue
        content = values_file.read_text()
        match = _TAG_RE.search(content)
        if not match:
            continue
        values_tag = match.group(1)
        if values_tag == latest_app_version:
            continue
        old_pat = f"tag: v{values_tag}"
        new_pat = f"tag: v{latest_app_version}"
        tag_count = content.count(old_pat)
        content = content.replace(old_pat, new_pat)
        values_file.write_text(content)
        print(
            f"  Updated values/{values_file.name} "
            f"({tag_count} image tag(s): v{values_tag} -> v{latest_app_version})"
        )


def run(config: dict, argv: list[str], script_path: str | os.PathLike) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Delegates to :func:`external_standard.run` with the image-tag hook
    injected after the helmfile pin rewrite.
    """
    return _run_external_standard(
        config, argv, script_path, post_pin_hook=_rewrite_image_tags
    )
