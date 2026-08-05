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
    tracked releases are listed; explicitly pinned/old releases are omitted
    so they are never auto-bumped (e.g. gitlab-runner old-build-deploy-image).

For ``BASE="standard"`` the base reads ``HELM_REPO_NAME`` / ``HELM_REPO_URL``
/ ``HELM_CHART`` / ``CHANGELOG_URL`` / ``CHART_TYPE``; for ``BASE="oci"`` it
reads ``GITHUB_REPO`` / ``GITHUB_TAG_PREFIX`` / ``HELM_CHART`` plus the
optional ``do_mirror`` / ``print_values_summary`` callables — identical to
the wrapped base templates.

The local component ``Chart.yaml`` continues to be refreshed by the base
flow (it mirrors the same version), so ``check-versions.py`` and the base
Step 1 stay consistent with the ArgoCD pin.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import _common_argocd
from .external_oci_with_mirror import run as _run_oci_with_mirror
from .external_standard import run as _run_external_standard


def _make_pin_write_hook(argocd_pin_files: list[str]):
    """Build the ``pin_write_hook`` closure for :func:`external_standard.run`.

    Resolves each ``ARGOCD_PIN_FILES`` entry against ``chart_dir`` (the
    component directory) and flips ``chart.version`` across all of them.
    Returns the count of files actually rewritten (for the operator log).
    """
    def pin_write(
        *,
        chart_dir: Path,
        current_version: str,
        latest_version: str,
    ) -> int:
        files = [chart_dir / rel for rel in argocd_pin_files]
        return _common_argocd.update_argocd_pins(
            files, current_version, latest_version
        )

    return pin_write


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

    if base == "oci":
        return _run_oci_with_mirror(
            config, argv, script_path, pin_write_hook=pin_write_hook
        )
    if base == "standard":
        return _run_external_standard(
            config, argv, script_path, pin_write_hook=pin_write_hook
        )

    print(
        f"  ERROR: argocd-pin template: unknown BASE '{base}' "
        "(expected 'standard' or 'oci').",
        file=sys.stderr,
    )
    return 1
