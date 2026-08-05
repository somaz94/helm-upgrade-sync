"""Status-table rendering for ``check-versions.py``.

Public API:

- :class:`Row` / :class:`ChartRow` — input dataclasses produced by the
  orchestrator's ``parse_managed_files`` walker.
- :class:`RowResult` — per-row classification (status / current /
  latest / err).
- :func:`resolve_row` / :func:`resolve_chart_row` — query the upstream
  via :mod:`upgrade_sync.fetchers` and classify the row.
- :func:`print_main_table` / :func:`print_chart_table` — pretty-print
  the resolved rows; return per-status counts.

Constants ``ROW_FMT``, ``HEADER_ROW``, ``EMPTY_SENTINEL`` and friends
are byte-for-byte parity with the retired bash ``check-versions.sh``
output — CI orchestrators that scrape the check-versions phase key off
this layout.

Stdlib only.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .fetchers import (
    fetch_latest_chart_version_gh,
    fetch_latest_git_tags,
    fetch_latest_helm_repo,
    fetch_latest_version_source,
    find_latest_available_source,
    verify_image_exists,
)


# =============================================================
# Table format strings + sentinels
# =============================================================

# Status table column widths. The bash version used ``printf '%-7s
# %-24s %-15s %-15s %s'``; the Python f-string equivalent below preserves
# the exact spacing (two-space gutter between columns).
ROW_FMT = "  {status:<7}  {template:<24}  {current:<15}  {latest:<15}  {path}"
CHART_ROW_FMT = (
    "  {status:<7}  {name:<20}  {current:<10}  {latest:<10}  {path}"
)

# ``printf '%-7s'`` pads a string longer than 7 chars verbatim (no
# truncation), so we emit fixed-width sentinel strings rather than
# relying on str.ljust().
HEADER_ROW = ROW_FMT.format(
    status="STATUS", template="TEMPLATE",
    current="CURRENT", latest="LATEST", path="PATH",
)
HEADER_RULE = ROW_FMT.format(
    status="-------", template="------------------------",
    current="---------------", latest="---------------", path="----",
)
CHART_HEADER_ROW = CHART_ROW_FMT.format(
    status="STATUS", name="CHART",
    current="CURRENT", latest="LATEST", path="PATH",
)
CHART_HEADER_RULE = CHART_ROW_FMT.format(
    status="-------", name="--------------------",
    current="----------", latest="----------", path="----",
)

# Em-dash sentinel used when a value is unknown (matches bash
# ``${var:-—}``).
EMPTY_SENTINEL = "—"


# =============================================================
# Row dataclasses
# =============================================================

@dataclass
class Row:
    """One row of the main status table (Phase 3)."""

    rel: str
    template: str
    label: str
    current: str
    fetcher: str
    fetcher_arg: str
    extra_arg: str
    container_image: str
    version_source_arg: str
    tag_prefix: str


@dataclass
class ChartRow:
    """One row of the OCI chart-pin status table (Phase 4)."""

    rel: str
    name: str
    current: str
    source_type: str
    source_repo: str


@dataclass
class RowResult:
    status: str
    current: str
    latest: str
    err: str = ""


# =============================================================
# Row classification
# =============================================================

def _which(cmd: str) -> bool:
    """``True`` when ``cmd`` resolves on PATH. Thin wrapper around
    :func:`shutil.which` so tests can ``mock.patch.object(table, "_which", ...)``
    without touching the real ``$PATH``.
    """
    return shutil.which(cmd) is not None


def resolve_row(row: Row, helm_installed: bool) -> RowResult:
    """Query the appropriate upstream + classify the row's status."""
    latest = ""
    err = ""
    if row.fetcher == "helm-repo":
        if not helm_installed:
            err = "helm not installed"
        elif not row.fetcher_arg:
            err = "HELM_CHART empty in CONFIG"
        else:
            latest = fetch_latest_helm_repo(row.fetcher_arg)
            if not latest:
                err = f"helm search repo '{row.fetcher_arg}' returned nothing"
    elif row.fetcher == "git-tags":
        if not _which("git"):
            err = "git not installed"
        else:
            latest = fetch_latest_git_tags(row.fetcher_arg)
            if not latest:
                err = (
                    f"git ls-remote --tags '{row.fetcher_arg}' returned no semver"
                )
    elif row.fetcher == "version-source":
        if not row.fetcher_arg:
            err = "VERSION_SOURCE empty in CONFIG"
        else:
            latest = fetch_latest_version_source(
                row.fetcher_arg, row.extra_arg,
                row.version_source_arg, row.tag_prefix,
            )
            if not latest:
                err = (
                    f"version-source '{row.fetcher_arg}' failed or unsupported"
                )
    else:
        err = f"unknown template '{row.template}'"

    current = row.current
    if err:
        return RowResult(status="ERROR", current=current or EMPTY_SENTINEL,
                         latest=latest or EMPTY_SENTINEL, err=err)
    if not current:
        return RowResult(
            status="ERROR", current=EMPTY_SENTINEL, latest=latest or EMPTY_SENTINEL,
            err="could not read current version",
        )
    if current == latest:
        return RowResult(status="OK", current=current, latest=latest)

    # Verify the image is actually published before reporting UPDATE.
    if row.container_image and not verify_image_exists(row.container_image, latest):
        available = find_latest_available_source(
            row.fetcher_arg, row.extra_arg, row.container_image,
            row.version_source_arg, row.tag_prefix,
        )
        if available and available != current:
            return RowResult(
                status="NO_IMG", current=current,
                latest=f"{latest} (→{available})",
                err=(
                    f"{latest} image missing; latest available: {available} "
                    f"(use --version {available})"
                ),
            )
        return RowResult(
            status="NO_IMG", current=current, latest=latest,
            err=(
                f"image {row.container_image}:{latest} not found; "
                f"no older published image found"
            ),
        )
    return RowResult(status="UPDATE", current=current, latest=latest)


def resolve_chart_row(row: ChartRow) -> RowResult:
    latest = ""
    err = ""
    if row.source_type == "github-releases":
        if not row.source_repo or not row.name:
            err = "CHART_SOURCE_REPO or CHART_NAME empty"
        else:
            latest = fetch_latest_chart_version_gh(row.source_repo, row.name)
            if not latest:
                err = (
                    f"no matching '{row.name}-X.Y.Z' release in {row.source_repo}"
                )
    else:
        err = f"unsupported CHART_SOURCE_TYPE '{row.source_type}'"

    current = row.current
    if err:
        return RowResult(status="ERROR", current=current or EMPTY_SENTINEL,
                         latest=latest or EMPTY_SENTINEL, err=err)
    if not current:
        return RowResult(
            status="ERROR", current=EMPTY_SENTINEL, latest=latest or EMPTY_SENTINEL,
            err="could not read chart pin from helmfile (yaml or gotmpl)",
        )
    if current == latest:
        return RowResult(status="OK", current=current, latest=latest)
    return RowResult(status="UPDATE", current=current, latest=latest)


# =============================================================
# Pretty printing
# =============================================================

def print_main_table(
    rows: list[Row], helm_installed: bool, updates_only: bool,
) -> tuple[int, int, int, int]:
    """Print the Phase 3 status table. Returns ``(ok, update, error, no_image)``."""
    print("")
    print(HEADER_ROW)
    print(HEADER_RULE)
    ok_count = 0
    update_count = 0
    error_count = 0
    no_image_count = 0
    for row in rows:
        result = resolve_row(row, helm_installed)
        if result.status == "OK":
            ok_count += 1
        elif result.status == "UPDATE":
            update_count += 1
        elif result.status == "NO_IMG":
            no_image_count += 1
        else:
            error_count += 1
        if updates_only and result.status == "OK":
            continue
        print(ROW_FMT.format(
            status=result.status,
            template=row.template,
            current=result.current or EMPTY_SENTINEL,
            latest=result.latest or EMPTY_SENTINEL,
            path=row.rel,
        ))
        if result.err:
            print(f"           -> {result.err}")
    return ok_count, update_count, error_count, no_image_count


def print_chart_table(
    chart_rows: list[ChartRow], updates_only: bool,
) -> tuple[int, int, int]:
    """Print the Phase 4 OCI chart-pin table. Returns ``(ok, update, error)``."""
    print("")
    print("OCI chart pin status (external-oci-cr-version consumers):")
    print("")
    print(CHART_HEADER_ROW)
    print(CHART_HEADER_RULE)
    ok = 0
    update = 0
    error = 0
    for row in chart_rows:
        result = resolve_chart_row(row)
        if result.status == "OK":
            ok += 1
        elif result.status == "UPDATE":
            update += 1
        else:
            error += 1
        if updates_only and result.status == "OK":
            continue
        print(CHART_ROW_FMT.format(
            status=result.status,
            name=row.name or EMPTY_SENTINEL,
            current=result.current or EMPTY_SENTINEL,
            latest=result.latest or EMPTY_SENTINEL,
            path=row.rel,
        ))
        if result.err:
            print(f"           -> {result.err}")
    return ok, update, error
