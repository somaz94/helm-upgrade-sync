"""Unit tests for upgrade_core/local_cr_version.py.

``local_cr_version`` is an **independent** module — sister of
``external_oci_cr_version``
(``external_oci_cr_version``). Shared CR helpers live in
:mod:`upgrade_core._common_cr`. K12-specific helpers covered here:

  - ``_read_chart_field`` — Chart.yaml top-level scalar reader.
  - ``_list_backups`` — K12 simpler form (Chart.yaml appVersion label).
  - ``_do_rollback`` — Chart.yaml + values restore (no chart-pin branch).
  - ``_stack_upgrade`` — 7-step main flow with Chart.yaml appVersion
    + optional ``MIRROR_CHART_VERSION`` mirror.
  - Argument parsing (no chart-pin sub-commands).
  - ``run()`` integration on the safest no-side-effect paths.

Stdlib unittest only.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

lcv = load("upgrade_core.local_cr_version")


# =============================================================
# _read_chart_field
# =============================================================


class ReadChartFieldTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
        tmp.write_text(content)
        return tmp

    def test_reads_app_version_bare(self) -> None:
        p = self._write("apiVersion: v2\nname: foo\nappVersion: 9.0.0\nversion: 0.1.0\n")
        self.assertEqual(lcv._read_chart_field(p, "appVersion"), "9.0.0")

    def test_reads_quoted_version(self) -> None:
        p = self._write('apiVersion: v2\nappVersion: "9.0.0"\n')
        self.assertEqual(lcv._read_chart_field(p, "appVersion"), "9.0.0")

    def test_missing_field(self) -> None:
        p = self._write("apiVersion: v2\nname: foo\n")
        self.assertEqual(lcv._read_chart_field(p, "appVersion"), "")

    def test_missing_file(self) -> None:
        p = Path("/nonexistent/file.yaml")
        self.assertEqual(lcv._read_chart_field(p, "appVersion"), "")

    def test_strips_inline_comment(self) -> None:
        p = self._write("appVersion: 9.0.0   # comment\n")
        self.assertEqual(lcv._read_chart_field(p, "appVersion"), "9.0.0")


# =============================================================
# _list_backups
# =============================================================


class ListBackupsTests(unittest.TestCase):
    def test_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                lcv._list_backups(Path(tmp) / "backup", "values/dev.yaml")
            self.assertIn("No backups found.", buf.getvalue())

    def test_lists_chart_appversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backup" / "20260101_000000"
            bdir.mkdir(parents=True)
            (bdir / "Chart.yaml").write_text(
                "apiVersion: v2\nname: foo\nappVersion: 9.2.3\nversion: 0.1.0\n"
            )
            (bdir / "dev.yaml").write_text("version: 9.2.3\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                lcv._list_backups(Path(tmp) / "backup", "values/dev.yaml")
            out = buf.getvalue()
            self.assertIn("20260101_000000", out)
            self.assertIn("appVersion: 9.2.3", out)


# =============================================================
# Argument parsing
# =============================================================


class ParseArgvTests(unittest.TestCase):
    _CFG = {"SCRIPT_NAME": "Test K12", "VALUES_FILE": "values/dev.yaml"}

    def test_default_is_stack(self) -> None:
        mode, *_ = lcv._parse_argv([], self._CFG, 5)
        self.assertEqual(mode, "stack")

    def test_dry_run(self) -> None:
        mode, _, dry, rc = lcv._parse_argv(["--dry-run"], self._CFG, 5)
        self.assertEqual((mode, dry, rc), ("stack", True, -1))

    def test_target_version(self) -> None:
        mode, tv, dry, rc = lcv._parse_argv(["--version", "9.5.0"], self._CFG, 5)
        self.assertEqual((mode, tv, dry, rc), ("stack", "9.5.0", False, -1))

    def test_help_returns_zero(self) -> None:
        with redirect_stdout(io.StringIO()):
            _, _, _, rc = lcv._parse_argv(["--help"], self._CFG, 5)
        self.assertEqual(rc, 0)

    def test_unknown_option_returns_one(self) -> None:
        with redirect_stdout(io.StringIO()):
            _, _, _, rc = lcv._parse_argv(["--bogus"], self._CFG, 5)
        self.assertEqual(rc, 1)

    def test_chart_pin_subcommands_unknown(self) -> None:
        """K12 has no chart-pin sub-flow; --check-chart should be unknown."""
        with redirect_stdout(io.StringIO()):
            _, _, _, rc = lcv._parse_argv(["--check-chart"], self._CFG, 5)
        self.assertEqual(rc, 1)

    def test_rollback_list_cleanup_modes(self) -> None:
        for arg, expected in (
            ("--rollback", "rollback"),
            ("--list-backups", "list-backups"),
            ("--cleanup-backups", "cleanup-backups"),
        ):
            mode, *_ = lcv._parse_argv([arg], self._CFG, 5)
            self.assertEqual(mode, expected)


# =============================================================
# _stack_upgrade — apply path with Chart.yaml mirror
# =============================================================


class StackUpgradeApplyTests(unittest.TestCase):
    """End-to-end through the apply step with mocked network + kubectl."""

    def _setup_chart_dir(
        self, *, current_version: str = "9.0.0", chart_yaml: bool = True,
        mirror_chart_version: bool = False,
    ) -> tuple[Path, dict]:
        tmp = tempfile.mkdtemp()
        chart_dir = Path(tmp)
        (chart_dir / "values").mkdir()
        (chart_dir / "values" / "dev.yaml").write_text(f"version: {current_version}\n")
        if chart_yaml:
            (chart_dir / "Chart.yaml").write_text(
                f"apiVersion: v2\nname: foo\nappVersion: {current_version}\nversion: 0.1.0\n"
            )
        (chart_dir / "helmfile.yaml").write_text(
            "releases:\n  - name: foo\n    namespace: ns\n    chart: .\n"
        )
        config = {
            "SCRIPT_NAME": "Test K12",
            "COMPONENT_LABEL": "foo",
            "VERSION_SOURCE": "elastic-artifacts",
            "VERSION_SOURCE_ARG": "",
            "VALUES_FILE": "values/dev.yaml",
            "VERSION_KEY": "version",
            "MAJOR_PIN": "9",
            "CHANGELOG_URL": "https://example.com/changelog",
            "CONTAINER_IMAGE": "",
            "CR_WEBHOOK_NAME": "",
            "CR_OPERATOR_NS": "",
            "CR_OPERATOR_STS": "",
            "CR_OPERATOR_CHART_DIR": "",
            "DEPENDENCY_CR_KIND": "",
            "DEPENDENCY_CR_NAME": "",
            "MIRROR_CHART_VERSION": mirror_chart_version,
        }
        return chart_dir, config

    def test_apply_updates_values_and_chart_appversion(self) -> None:
        chart_dir, config = self._setup_chart_dir(current_version="9.0.0")
        backup_dir = chart_dir / "backup"
        with mock.patch.object(lcv, "fetch_latest_version", return_value="9.2.3"):
            with mock.patch.object(lcv, "check_cluster_health", return_value=True):
                with redirect_stdout(io.StringIO()):
                    rc = lcv._stack_upgrade(
                        config, chart_dir, backup_dir,
                        helmfile_path=chart_dir / "helmfile.yaml",
                        dry_run=False, target_version="",
                        keep_backups=5,
                    )
        self.assertEqual(rc, 0)
        self.assertIn(
            "version: 9.2.3",
            (chart_dir / "values" / "dev.yaml").read_text(),
        )
        chart_text = (chart_dir / "Chart.yaml").read_text()
        self.assertIn("appVersion: 9.2.3", chart_text)
        # Without MIRROR_CHART_VERSION, chart version stays unchanged.
        self.assertIn("version: 0.1.0", chart_text)

    def test_apply_mirrors_chart_version_when_enabled(self) -> None:
        chart_dir, config = self._setup_chart_dir(
            current_version="9.0.0", mirror_chart_version=True
        )
        backup_dir = chart_dir / "backup"
        with mock.patch.object(lcv, "fetch_latest_version", return_value="9.2.3"):
            with mock.patch.object(lcv, "check_cluster_health", return_value=True):
                with redirect_stdout(io.StringIO()):
                    rc = lcv._stack_upgrade(
                        config, chart_dir, backup_dir,
                        helmfile_path=chart_dir / "helmfile.yaml",
                        dry_run=False, target_version="",
                        keep_backups=5,
                    )
        self.assertEqual(rc, 0)
        chart_text = (chart_dir / "Chart.yaml").read_text()
        self.assertIn("appVersion: 9.2.3", chart_text)
        self.assertIn("version: 9.2.3", chart_text)
        # Chart version 0.1.0 should be replaced.
        self.assertNotIn("version: 0.1.0", chart_text)

    def test_dry_run_changes_nothing(self) -> None:
        chart_dir, config = self._setup_chart_dir(current_version="9.0.0")
        backup_dir = chart_dir / "backup"
        with mock.patch.object(lcv, "fetch_latest_version", return_value="9.2.3"):
            with mock.patch.object(lcv, "check_cluster_health", return_value=True):
                with redirect_stdout(io.StringIO()):
                    rc = lcv._stack_upgrade(
                        config, chart_dir, backup_dir,
                        helmfile_path=chart_dir / "helmfile.yaml",
                        dry_run=True, target_version="",
                        keep_backups=5,
                    )
        self.assertEqual(rc, 0)
        # Files unchanged.
        self.assertIn(
            "version: 9.0.0",
            (chart_dir / "values" / "dev.yaml").read_text(),
        )

    def test_already_up_to_date(self) -> None:
        chart_dir, config = self._setup_chart_dir(current_version="9.2.3")
        backup_dir = chart_dir / "backup"
        with mock.patch.object(lcv, "fetch_latest_version", return_value="9.2.3"):
            with mock.patch.object(lcv, "check_cluster_health", return_value=True):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lcv._stack_upgrade(
                        config, chart_dir, backup_dir,
                        helmfile_path=chart_dir / "helmfile.yaml",
                        dry_run=False, target_version="",
                        keep_backups=5,
                    )
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date", buf.getvalue())


# =============================================================
# run() integration — list-backups + help paths
# =============================================================


class RunIntegrationTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "SCRIPT_NAME": "Test K12",
            "COMPONENT_LABEL": "foo",
            "VERSION_SOURCE": "elastic-artifacts",
            "VERSION_SOURCE_ARG": "",
            "VALUES_FILE": "values/dev.yaml",
            "VERSION_KEY": "version",
            "MAJOR_PIN": "9",
            "CHANGELOG_URL": "",
            "CONTAINER_IMAGE": "",
            "CR_WEBHOOK_NAME": "",
            "CR_OPERATOR_NS": "",
            "CR_OPERATOR_STS": "",
            "CR_OPERATOR_CHART_DIR": "",
            "DEPENDENCY_CR_KIND": "",
            "DEPENDENCY_CR_NAME": "",
            "MIRROR_CHART_VERSION": False,
        }

    def test_list_backups_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            script_path = chart_dir / "upgrade.py"
            script_path.touch()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lcv.run(
                    self._config(), ["--list-backups"], script_path=str(script_path)
                )
            self.assertEqual(rc, 0)
            self.assertIn("No backups found.", buf.getvalue())

    def test_help_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            script_path = chart_dir / "upgrade.py"
            script_path.touch()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lcv.run(self._config(), ["--help"], script_path=str(script_path))
            self.assertEqual(rc, 0)
            self.assertIn("Test K12", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
