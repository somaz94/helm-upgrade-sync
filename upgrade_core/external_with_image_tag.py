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

# `repository: docker.io/goharbor/valkey-photon` (quotes optional, comment allowed).
_REPOSITORY_RE = re.compile(
    r"^\s*repository:\s*[\"']?(?P<repo>[^\s\"'#]+)[\"']?\s*(?:#.*)?$", re.MULTILINE
)


def _repository_basenames(text: str) -> set[str]:
    """Return the last path segment of every ``repository:`` value in ``text``.

    Comparing basenames rather than full references is deliberate: an override
    legitimately retargets the registry/org (a registry mirror, a custom build), and
    only the image *name* is the thing upstream can rename underneath us.
    """
    return {
        m.group("repo").rstrip("/").rsplit("/", 1)[-1]
        for m in _REPOSITORY_RE.finditer(text)
    }


def _warn_repository_drift(*, values_dir: Path, exclude_patterns: str) -> None:
    """Warn when an override names an image the new upstream values no longer has.

    This template rewrites **tags only** — it never touches ``repository:``.
    When upstream renames an image, the override keeps the old name and takes
    the new tag, producing a reference that does not exist. The Harbor chart
    did exactly this in 1.19.2 (``redis-photon`` -> ``valkey-photon``): the
    rendered ``goharbor/redis-photon:v2.15.2`` would have gone straight to
    ImagePullBackOff, and no resource-removal diff check can see it.

    Advisory only: an override may name an image that upstream values never
    mention (a sidecar, a wholly custom build), so this cannot fail the run
    without producing false alarms. It prints; the human decides.
    """
    upstream = values_dir.parent / "values.yaml"
    if not upstream.is_file():
        return
    upstream_names = _repository_basenames(upstream.read_text())
    if not upstream_names:
        return
    for values_file in sorted(values_dir.glob("*.yaml")):
        if not values_file.is_file() or _is_excluded(values_file.name, exclude_patterns):
            continue
        unknown = sorted(_repository_basenames(values_file.read_text()) - upstream_names)
        if not unknown:
            continue
        print(f"    WARNING: {values_file.name} names image(s) absent from the new "
              f"upstream values.yaml: {', '.join(unknown)}")
        print("      This template rewrites tags only, never `repository:`. If upstream "
              "RENAMED an image, the tag was just bumped onto the OLD name and the "
              "reference no longer exists (ImagePullBackOff at deploy).")
        print("      Confirm each rendered reference resolves before merging, e.g.:")
        print("        helm template <rel> <chart> --version <new> "
              f"-f {values_file.parent.name}/{values_file.name} | grep 'image:'")


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

    # Tags are now bumped; check that each override still names an image the new
    # upstream values actually ship. See _warn_repository_drift for why.
    _warn_repository_drift(values_dir=values_dir, exclude_patterns=exclude_patterns)


def run(config: dict, argv: list[str], script_path: str | os.PathLike) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Delegates to :func:`external_standard.run` with the image-tag hook
    injected after the helmfile pin rewrite.
    """
    return _run_external_standard(
        config, argv, script_path, post_pin_hook=_rewrite_image_tags
    )
