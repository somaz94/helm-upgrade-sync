"""External OCI Helm chart with Harbor-mirror upgrade runner.

Thin extension of :mod:`external_oci` for charts whose upstream images
must be mirrored to a private registry (Harbor) before the chart upgrade
is applied. The 8-step flow reuses K9's hooks (``fetch_latest_hook``,
``chart_write_hook``, ``helmfile_pin_hook``) and adds two K10-only hooks
introduced in :mod:`external_standard`:

  - ``pre_apply_hook`` runs as ``[Step 7/8]`` and drives the mirror
    stage. Non-zero return aborts the upgrade (no files modified).
    Skipped in dry-run with a SKIPPED message.
  - ``values_summary_hook`` runs at the tail of Step 1 and surfaces
    per-values-file ``image.tag`` overrides. When the consumer omits the
    hook the K10 default (yq-based ``.image.tag`` per file) takes over.

A ``mirror_image`` helper is exposed for consumer ``upgrade.py`` files
so the per-chart ``do_mirror`` function can call ``crane copy`` in the
same shape as the bash template's ``mirror_image`` shell helper.

Migrated from the canonical bash template at
``scripts/upgrade-sync/templates/external-oci-with-mirror.sh``.

Public entry-point: ``run(config, argv, script_path)``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .external_oci import (
    _fetch_latest_via_github,
    _helmfile_pin_default_or_scoped,
    _write_chart_wrapper_aware,
)
from .external_standard import PinWriteHook, run as _run_external_standard


# -----------------------------------------------
# crane-based mirror helper (used by consumer `do_mirror`)
# -----------------------------------------------

def mirror_image(
    upstream_ref: str,
    harbor_ref: str,
    *,
    insecure: bool = False,
) -> int:
    """Mirror an upstream image reference to a private registry via crane.

    Idempotent: if the destination digest already matches upstream, the
    copy is skipped. Verifies digest match after the copy. Aborts
    (non-zero return) on failure.

    Mirrors the bash ``mirror_image`` helper byte-for-byte:

      - Missing ``crane`` binary → ERROR + return 2.
      - Empty upstream digest → ERROR + return 1.
      - Matching digests → SKIP message + return 0.
      - Copy failure → ERROR + return 1.
      - Post-copy digest mismatch → ERROR + return 1.
      - Success → OK message + return 0.

    ``insecure=True`` passes ``--insecure`` to every crane invocation
    (needed for registries with non-standards-compliant certs).
    """
    flags = ["--insecure"] if insecure else []

    if shutil.which("crane") is None:
        print(
            "  ERROR: 'crane' is required for the mirror stage.",
            file=sys.stderr,
        )
        print(
            "         Install: brew install crane (macOS) or",
            file=sys.stderr,
        )
        print(
            "         go install github.com/google/go-containerregistry/cmd/crane@latest",
            file=sys.stderr,
        )
        return 2

    upstream_digest = _crane_digest(upstream_ref, flags)
    harbor_digest = _crane_digest(harbor_ref, flags)

    if not upstream_digest:
        print(
            f"  ERROR: cannot resolve upstream digest for {upstream_ref}",
            file=sys.stderr,
        )
        return 1

    if harbor_digest and upstream_digest == harbor_digest:
        print(
            f"  SKIP   {upstream_ref} -> {harbor_ref} "
            f"(already mirrored, digest={harbor_digest})"
        )
        return 0

    print(f"  COPY   {upstream_ref} -> {harbor_ref}")
    copy_rc = subprocess.run(
        ["crane", "copy", *flags, upstream_ref, harbor_ref],
        check=False,
    ).returncode
    if copy_rc != 0:
        print(
            f"  ERROR: crane copy failed "
            f"({upstream_ref} -> {harbor_ref})",
            file=sys.stderr,
        )
        return 1

    verify_digest = _crane_digest(harbor_ref, flags)
    if verify_digest != upstream_digest:
        print(
            f"  ERROR: digest mismatch after copy "
            f"({upstream_digest} vs {verify_digest})",
            file=sys.stderr,
        )
        return 1

    print(f"  OK     {harbor_ref} (digest={verify_digest})")
    return 0


def _crane_digest(ref: str, flags: list[str]) -> str:
    """Return ``crane digest`` stdout (stripped) or empty on failure."""
    result = subprocess.run(
        ["crane", "digest", *flags, ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "").strip()


# -----------------------------------------------
# Entry-point
# -----------------------------------------------

def run(
    config: dict,
    argv: list[str],
    script_path: str | os.PathLike,
    *,
    pin_write_hook: PinWriteHook | None = None,
) -> int:
    """Entry-point invoked by each consumer ``upgrade.py``.

    Delegates to :func:`external_standard.run` with ``total_steps=8``
    plus K9 (external-oci) hooks for OCI fetch / wrapper-mode chart
    write / tracked-chart helmfile pin scope, and K10 hooks for the
    mirror stage (``pre_apply_hook``) and Step 1 values summary
    (``values_summary_hook``).

    Per-chart ``do_mirror`` and ``print_values_summary`` callables are
    read from ``config`` when present. ``do_mirror`` must accept the
    keyword arguments documented on
    :data:`external_standard.PreApplyHook` and return 0 to continue, or
    non-zero to abort. ``print_values_summary`` accepts ``values_dir``.

    CONFIG keys consumed:
      - ``GITHUB_REPO`` / ``GITHUB_TAG_PREFIX`` (K9 inherit).
      - ``WRAPPER_CHART_YAML`` / ``HELMFILE_TRACKED_CHART`` (K9 inherit).
      - ``do_mirror`` (callable; optional). Missing = silent skip.
      - ``print_values_summary`` (callable; optional). Missing = K10
        default (yq-based ``.image.tag`` per ``values/*.yaml``).
    """
    github_repo = config["GITHUB_REPO"]
    tag_prefix = config.get("GITHUB_TAG_PREFIX", "v")
    wrapper_mode = bool(config.get("WRAPPER_CHART_YAML", False))
    tracked_chart = config.get("HELMFILE_TRACKED_CHART", "") or ""
    do_mirror = config.get("do_mirror")
    print_values_summary = config.get("print_values_summary")

    def fetch_hook(*, config: dict) -> tuple[str, str]:
        latest_version_found, latest_tag = _fetch_latest_via_github(
            github_repo, tag_prefix
        )
        if not latest_version_found:
            print(
                f"  (GitHub Releases API: {github_repo} — "
                f"no tag matched prefix '{tag_prefix}')"
            )
            return "", ""
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

    pre_apply = _make_pre_apply_hook(do_mirror) if do_mirror is not None else None
    values_summary = _make_values_summary_hook(print_values_summary)

    return _run_external_standard(
        config,
        argv,
        script_path,
        total_steps=8,
        fetch_latest_hook=fetch_hook,
        chart_write_hook=chart_write,
        helmfile_pin_hook=helmfile_pin,
        values_summary_hook=values_summary,
        pre_apply_hook=pre_apply,
        pin_write_hook=pin_write_hook,
    )


def _make_pre_apply_hook(do_mirror):
    """Wrap the per-chart ``do_mirror`` callable in the PreApplyHook signature.

    ``do_mirror`` receives the same kwargs as ``PreApplyHook`` plus
    ``mirror_image`` so per-chart bodies stay terse. Non-zero return =
    abort the upgrade (the caller prints the ERROR line and propagates
    the rc).
    """
    def pre_apply(
        *,
        chart_dir: Path,
        temp_dir: Path,
        values_dir: Path,
        latest_version: str,
        latest_app_version: str,
    ) -> int:
        return do_mirror(
            chart_dir=chart_dir,
            temp_dir=temp_dir,
            values_dir=values_dir,
            latest_version=latest_version,
            latest_app_version=latest_app_version,
            mirror_image=mirror_image,
        )
    return pre_apply


def _make_values_summary_hook(print_values_summary):
    """Wrap the per-chart ``print_values_summary`` callable, or return
    ``None`` so the external_standard default kicks in.

    Returning ``None`` here is critical: when the consumer omits the
    override, the K10 ``_default_values_summary`` baseline (yq-based
    ``.image.tag`` per ``values/*.yaml``) must run. Building a closure
    that calls the default would double-print.
    """
    if print_values_summary is None:
        return None

    def values_summary(*, values_dir: Path) -> None:
        print_values_summary(values_dir=values_dir)
    return values_summary
