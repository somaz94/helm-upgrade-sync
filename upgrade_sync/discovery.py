"""Managed-file + chart discovery for the sync system.

Mirrors the bash ``find_managed_files`` / ``find_unmanaged_charts`` /
``read_template_header`` helpers from the retired ``sync.sh``.

Three call sites share these helpers:

- ``sync.py`` (this package's CLI entry point)
- ``check-versions.py`` (version-probe orchestrator)
- CI upgrade orchestrators

The bash original walked ``find -type f -name upgrade.{sh,py}`` with five
path exclusions; this module ports the same exclusions over ``Path.rglob``.
"""

from __future__ import annotations

from pathlib import Path


# Sub-paths that the bash discovery skipped. Match anywhere in the
# repo-relative path string (mirrors bash ``-not -path '*/backup/*'`` etc.).
_EXCLUDED_SUBSTRINGS: tuple[str, ...] = (
    "/backup/",
    "/_deprecated/",
    "/_optional/",
    "/scripts/upgrade-sync/",
    "/tests/python/fixtures/",
)

# Filenames that the discovery treats as "managed upgrade scripts".
# The bash original walked ``-name 'upgrade.sh' -o -name 'upgrade.py'``;
# The shell -> python migration moved every canonical template + every consumer to
# ``.py``. The ``.sh`` glob is kept here for forward compatibility (e.g.
# an experimental fixture chart that hasn't been migrated yet).
_UPGRADE_FILENAMES: tuple[str, ...] = ("upgrade.sh", "upgrade.py")

# Line-2 header read by ``parse_template_header``.
_HEADER_PREFIX = "# upgrade-template: "


def find_managed_files(repo_root: Path) -> list[Path]:
    """Return every managed ``upgrade.{sh,py}`` under ``repo_root``, sorted.

    Skips ``backup/``, ``_deprecated/``, ``_optional/``, the sync tool
    itself (``scripts/upgrade-sync/``), and the fixtures directory.
    """
    matches: list[Path] = []
    for pattern in _UPGRADE_FILENAMES:
        for path in repo_root.rglob(pattern):
            if not path.is_file():
                continue
            rel = str(path.relative_to(repo_root))
            if any(s.lstrip("/") in rel for s in _EXCLUDED_SUBSTRINGS):
                continue
            matches.append(path)
    # Sort by string path so the order matches bash ``find | sort``. The
    # default ``sorted(Path[...])`` walks the parts tuple, which puts
    # ``security/keycloak`` before ``security/keycloak-operator`` whereas
    # byte-wise sort puts ``-`` (0x2d) before ``/`` (0x2f).
    return sorted(matches, key=str)


def find_unmanaged_charts(repo_root: Path) -> list[str]:
    """Return repo-relative paths of chart dirs missing an upgrade.{sh,py}.

    A "chart directory" is any directory containing ``Chart.yaml`` that is
    not inside a backup / deprecated / optional / templates dir or the
    test fixtures. Used only by ``--status`` for operator awareness.
    """
    chart_excludes = _EXCLUDED_SUBSTRINGS + ("/templates/",)
    results: set[str] = set()
    for chart in repo_root.rglob("Chart.yaml"):
        if not chart.is_file():
            continue
        rel = str(chart.relative_to(repo_root))
        if any(s.lstrip("/") in rel for s in chart_excludes):
            continue
        component_dir = chart.parent
        has_managed = any((component_dir / name).is_file() for name in _UPGRADE_FILENAMES)
        if not has_managed:
            results.add(str(component_dir.relative_to(repo_root)))
    return sorted(results)


def parse_template_header(upgrade_script: Path) -> str:
    """Return the ``# upgrade-template: <name>`` value on line 2.

    Mirrors the bash ``sed -n '2s/^# upgrade-template: //p'``. Returns
    an empty string when the header is absent (legacy / unmanaged file).
    """
    try:
        with upgrade_script.open("r", encoding="utf-8") as fh:
            next(fh)  # skip shebang
            line = next(fh, "")
    except OSError:
        return ""
    line = line.rstrip("\n")
    if line.startswith(_HEADER_PREFIX):
        return line[len(_HEADER_PREFIX):]
    return ""
