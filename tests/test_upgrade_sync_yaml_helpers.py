"""Unit tests for scripts/python/upgrade_sync/yaml_helpers.py.

Top-level scalar YAML reader + helmfile chart-pin reader, extracted
from ``check-versions.py``. Fixtures live under ``tests/python/fixtures/``
and are re-used in MR validation for byte-for-byte parity with the
retired bash readers.

Stdlib unittest only.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Make ``upgrade_sync`` importable when launched directly.
_pkg_root = REPO_ROOT / "scripts" / "python"
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from upgrade_sync.yaml_helpers import (  # noqa: E402
    read_argocd_chart_version,
    read_helmfile_chart_pin,
    read_yaml_value,
)


class TestReadYamlValue(unittest.TestCase):

    def test_double_quoted_value(self) -> None:
        path = FIXTURES_DIR / "auto-upgrade-component-cr/values/dev.yaml"
        self.assertEqual(read_yaml_value(path, "version"), "8.15.0")

    def test_unquoted_chart_yaml_version(self) -> None:
        path = FIXTURES_DIR / "auto-upgrade-chart.yaml"
        self.assertEqual(read_yaml_value(path, "version"), "0.42.1")

    def test_returns_empty_when_key_missing(self) -> None:
        path = FIXTURES_DIR / "auto-upgrade-chart.yaml"
        self.assertEqual(read_yaml_value(path, "nonexistent"), "")


class TestReadHelmfileChartPin(unittest.TestCase):

    def test_reads_release_level_version(self) -> None:
        # The CR fixture uses helmfile.yaml (not gotmpl) with an
        # indented ``  version: 0.16.0`` line.
        comp = FIXTURES_DIR / "auto-upgrade-component-cr"
        self.assertEqual(read_helmfile_chart_pin(comp), "0.16.0")

    def test_returns_empty_when_no_helmfile(self) -> None:
        comp = FIXTURES_DIR / "auto-upgrade-component-non-cr"
        self.assertEqual(read_helmfile_chart_pin(comp), "")


class TestReadArgocdChartVersion(unittest.TestCase):
    """argocd-pin version SSOT — chart.version under <component>/argocd/*.yaml."""

    def _component(self, files: dict[str, str], marker: str = "argocd") -> Path:
        comp = Path(tempfile.mkdtemp())
        argocd = comp / marker
        argocd.mkdir()
        for name, body in files.items():
            (argocd / name).write_text(body)
        return comp

    def test_reads_single_release(self) -> None:
        comp = self._component(
            {"release.yaml": "chart:\n  name: x\n  version: \"0.1.6\"\n"}
        )
        self.assertEqual(read_argocd_chart_version(comp), "0.1.6")

    def test_multi_release_picks_first_sorted(self) -> None:
        # build-image (tracked, 0.90.0) sorts before old-build-deploy-image
        # (pinned, 0.70.3) -> the tracked version is reported.
        comp = self._component({
            "build-image.yaml": "chart:\n  version: 0.90.0\n",
            "deploy-image.yaml": "chart:\n  version: 0.90.0\n",
            "old-build-deploy-image.yaml": "chart:\n  version: 0.70.3\n",
        })
        self.assertEqual(read_argocd_chart_version(comp), "0.90.0")

    def test_no_argocd_dir_returns_empty(self) -> None:
        comp = Path(tempfile.mkdtemp())
        self.assertEqual(read_argocd_chart_version(comp), "")

    def test_ignores_top_level_version(self) -> None:
        comp = self._component(
            {"release.yaml": "version: 9.9.9\nchart:\n  version: 1.0.0\n"}
        )
        self.assertEqual(read_argocd_chart_version(comp), "1.0.0")

    def test_reads_argocd_aws_marker_dir(self) -> None:
        # AWS components pin under argocd-aws/ (not argocd/); the reader
        # must honor the disjoint marker dir. Regression for the -aws
        # "could not read current version" preflight failure.
        comp = self._component(
            {"build-image.yaml": "chart:\n  name: gitlab-runner\n  version: \"0.90.1\"\n"},
            marker="argocd-aws",
        )
        self.assertEqual(read_argocd_chart_version(comp), "0.90.1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
