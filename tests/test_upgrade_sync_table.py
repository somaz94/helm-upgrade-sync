"""Unit tests for upgrade_sync/table.py.

Row classification + status-table rendering — extracted from
``check-versions.py``. ``resolve_row`` / ``resolve_chart_row`` are
covered with synthetic ``Row`` / ``ChartRow`` instances and mocked
fetchers so the suite never hits the network.

Stdlib unittest only.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Make ``upgrade_sync`` importable when the test is launched directly.
_pkg_root = REPO_ROOT / "scripts" / "python"
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from upgrade_sync import table  # noqa: E402


class TestResolveRow(unittest.TestCase):

    def _row(self, **overrides: object) -> table.Row:
        defaults = dict(
            rel="cicd/argo-cd/upgrade.sh",
            template="external-standard",
            label="argo",
            current="9.5.14",
            fetcher="helm-repo",
            fetcher_arg="argo/argo-cd",
            extra_arg="",
            container_image="",
            version_source_arg="",
            tag_prefix="",
        )
        defaults.update(overrides)
        return table.Row(**defaults)  # type: ignore[arg-type]

    def test_helm_repo_ok_status(self) -> None:
        row = self._row()
        with mock.patch.object(table, "fetch_latest_helm_repo", return_value="9.5.14"):
            result = table.resolve_row(row, helm_installed=True)
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.current, "9.5.14")
        self.assertEqual(result.latest, "9.5.14")

    def test_helm_repo_update_status(self) -> None:
        row = self._row()
        with mock.patch.object(table, "fetch_latest_helm_repo", return_value="9.6.0"):
            result = table.resolve_row(row, helm_installed=True)
        self.assertEqual(result.status, "UPDATE")
        self.assertEqual(result.latest, "9.6.0")

    def test_helm_not_installed_error(self) -> None:
        row = self._row()
        result = table.resolve_row(row, helm_installed=False)
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.err, "helm not installed")

    def test_unknown_template_error(self) -> None:
        row = self._row(template="bogus", fetcher="unknown")
        result = table.resolve_row(row, helm_installed=True)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("unknown template", result.err)

    def test_empty_current_error(self) -> None:
        row = self._row(current="")
        with mock.patch.object(table, "fetch_latest_helm_repo", return_value="9.5.14"):
            result = table.resolve_row(row, helm_installed=True)
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.err, "could not read current version")

    def test_git_tags_no_git_installed_error(self) -> None:
        row = self._row(
            template="local-with-templates",
            fetcher="git-tags",
            fetcher_arg="https://example/foo.git",
        )
        with mock.patch.object(table, "_which", return_value=False):
            result = table.resolve_row(row, helm_installed=True)
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.err, "git not installed")

    def test_no_img_when_container_image_missing(self) -> None:
        row = self._row(
            template="local-cr-version",
            fetcher="version-source",
            fetcher_arg="elastic-artifacts",
            extra_arg="9",
            container_image="docker.elastic.co/foo",
        )
        with mock.patch.object(table, "fetch_latest_version_source", return_value="9.1.0"), \
             mock.patch.object(table, "verify_image_exists", return_value=False), \
             mock.patch.object(table, "find_latest_available_source", return_value="9.0.5"):
            result = table.resolve_row(row, helm_installed=True)
        self.assertEqual(result.status, "NO_IMG")
        self.assertIn("→", result.latest)


class TestResolveChartRow(unittest.TestCase):

    def test_ok_status(self) -> None:
        row = table.ChartRow(
            rel="observability/logging/elasticsearch/upgrade.sh",
            name="elasticsearch-eck",
            current="0.1.9",
            source_type="github-releases",
            source_repo="somaz94/helm-charts",
        )
        with mock.patch.object(table, "fetch_latest_chart_version_gh", return_value="0.1.9"):
            result = table.resolve_chart_row(row)
        self.assertEqual(result.status, "OK")

    def test_update_status(self) -> None:
        row = table.ChartRow(
            rel="observability/logging/elasticsearch/upgrade.sh",
            name="elasticsearch-eck",
            current="0.1.8",
            source_type="github-releases",
            source_repo="somaz94/helm-charts",
        )
        with mock.patch.object(table, "fetch_latest_chart_version_gh", return_value="0.1.9"):
            result = table.resolve_chart_row(row)
        self.assertEqual(result.status, "UPDATE")
        self.assertEqual(result.latest, "0.1.9")

    def test_unsupported_source_type_error(self) -> None:
        row = table.ChartRow(
            rel="x", name="y", current="0.1.0",
            source_type="bogus", source_repo="z",
        )
        result = table.resolve_chart_row(row)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("unsupported CHART_SOURCE_TYPE", result.err)

    def test_empty_current_error(self) -> None:
        """Missing helmfile chart pin → ERROR with the bash-equivalent text."""
        row = table.ChartRow(
            rel="observability/logging/elasticsearch/upgrade.sh",
            name="elasticsearch-eck",
            current="",
            source_type="github-releases",
            source_repo="somaz94/helm-charts",
        )
        with mock.patch.object(table, "fetch_latest_chart_version_gh", return_value="0.1.9"):
            result = table.resolve_chart_row(row)
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(
            result.err, "could not read chart pin from helmfile (yaml or gotmpl)"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
