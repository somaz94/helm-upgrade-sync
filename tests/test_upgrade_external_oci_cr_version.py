"""Unit tests for upgrade_core/external_oci_cr_version.py.

``external_oci_cr_version`` is an **independent** module — does not extend
:mod:`external_standard` via hooks. Shared CR helpers were extracted to
:mod:`upgrade_core._common_cr` in the shell -> python migration — those have
their own test file (``test_upgrade_common_cr.py``).

This file covers ``external_oci_cr_version``-specific helpers only:
  - Backup classifier (chart vs stack) + reader (N15 — values_file-aware)
  - OCI chart-pin helpers (read/update gotmpl hoist + indented version)
  - List chart versions (publisher releases filtered by CHART_NAME)
  - Argument parsing (9 commands incl. chart-pin sub-commands)
  - run() integration on the safest no-side-effect paths

Stdlib unittest only.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ecv = load("upgrade_core.external_oci_cr_version")


# =============================================================
# Backup classifier + version reader (``external_oci_cr_version``-specific)
# =============================================================


class ClassifyBackupTests(unittest.TestCase):
    def test_dash_chart_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000-chart"
            d.mkdir()
            self.assertEqual(ecv._classify_backup(d), "chart")

    def test_helmfile_yaml_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            (d / "helmfile.yaml").touch()
            self.assertEqual(ecv._classify_backup(d), "chart")

    def test_helmfile_gotmpl_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            (d / "helmfile.yaml.gotmpl").touch()
            self.assertEqual(ecv._classify_backup(d), "chart")

    def test_stack_with_values_yaml_explicit(self) -> None:
        """N15: passing values_file tightens to basename match."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            (d / "dev.yaml").write_text("version: 9.0.0\n")
            self.assertEqual(ecv._classify_backup(d, "values/dev.yaml"), "stack")

    def test_stack_loose_fallback(self) -> None:
        """Bash parity: empty values_file → any *.yaml child = stack."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            (d / "some.yaml").write_text("data: 1\n")
            self.assertEqual(ecv._classify_backup(d), "stack")

    def test_values_file_mismatch_is_unknown(self) -> None:
        """N15: with values_file, missing basename → unknown (tighter)."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            (d / "other.yaml").write_text("x: 1\n")
            self.assertEqual(ecv._classify_backup(d, "values/dev.yaml"), "unknown")

    def test_unknown_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            self.assertEqual(ecv._classify_backup(d), "unknown")


class ReadBackupVersionTests(unittest.TestCase):
    def test_stack_reads_version_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000"
            d.mkdir()
            (d / "dev.yaml").write_text("version: 9.2.3\nother: foo\n")
            self.assertEqual(
                ecv._read_backup_version(d, "values/dev.yaml", "version"),
                "9.2.3",
            )

    def test_chart_reads_helmfile_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000-chart"
            d.mkdir()
            (d / "helmfile.yaml").write_text(
                "releases:\n  - name: foo\n    chart: oci://x\n    version: 0.1.5\n"
            )
            self.assertEqual(
                ecv._read_backup_version(d, "values/dev.yaml", "version"),
                "0.1.5",
            )

    def test_chart_prefers_gotmpl_hoist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "20260101_000000-chart"
            d.mkdir()
            (d / "helmfile.yaml.gotmpl").write_text(
                "{{- $chartVersion := \"0.2.0\" }}\n"
                "releases:\n  - name: foo\n    version: 0.1.5\n"
            )
            self.assertEqual(
                ecv._read_backup_version(d, "values/dev.yaml", "version"),
                "0.2.0",
            )


# =============================================================
# OCI chart-pin helpers (``external_oci_cr_version``-specific)
# =============================================================


class HelmfileChartPinTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
        tmp.write_text(content)
        return tmp

    def test_read_chart_url(self) -> None:
        p = self._write(
            "releases:\n"
            "  - name: foo\n"
            "    chart: oci://ghcr.io/x/foo\n"
            "    version: 0.1.0\n"
        )
        self.assertEqual(
            ecv._read_helmfile_chart_url(p), "oci://ghcr.io/x/foo"
        )

    def test_read_pin_indented_bare(self) -> None:
        p = self._write(
            "releases:\n  - name: foo\n    chart: oci://x\n    version: 9.2.3\n"
        )
        self.assertEqual(ecv._read_helmfile_chart_pin(p), "9.2.3")

    def test_read_pin_indented_quoted(self) -> None:
        p = self._write(
            "releases:\n  - name: foo\n    chart: oci://x\n    version: \"9.2.3\"\n"
        )
        self.assertEqual(ecv._read_helmfile_chart_pin(p), "9.2.3")

    def test_read_pin_gotmpl_hoist_preferred(self) -> None:
        p = self._write(
            '{{- $chartVersion := "0.3.0" }}\n'
            "releases:\n  - name: foo\n    chart: oci://x\n"
            "    version: {{ $chartVersion | quote }}\n"
        )
        self.assertEqual(ecv._read_helmfile_chart_pin(p), "0.3.0")

    def test_update_pin_double_quoted_preserves(self) -> None:
        p = self._write(
            "releases:\n  - name: foo\n    chart: oci://x\n    version: \"9.2.3\"\n"
        )
        ecv._update_helmfile_chart_pin(p, "9.3.0")
        self.assertIn('    version: "9.3.0"', p.read_text())
        self.assertNotIn("9.2.3", p.read_text())

    def test_update_pin_bare_preserves_bare(self) -> None:
        p = self._write(
            "releases:\n  - name: foo\n    chart: oci://x\n    version: 9.2.3\n"
        )
        ecv._update_helmfile_chart_pin(p, "9.3.0")
        self.assertIn("    version: 9.3.0", p.read_text())

    def test_update_pin_gotmpl_hoist(self) -> None:
        p = self._write(
            '{{- $chartVersion := "0.3.0" }}\n'
            "releases:\n  - name: foo\n    version: {{ $chartVersion | quote }}\n"
        )
        ecv._update_helmfile_chart_pin(p, "0.4.1")
        text = p.read_text()
        self.assertIn('$chartVersion := "0.4.1"', text)
        self.assertIn("version: {{ $chartVersion | quote }}", text)

    def test_update_pin_only_first_match(self) -> None:
        p = self._write(
            "releases:\n"
            "  - name: foo\n    chart: oci://x\n    version: 9.2.3\n"
            "  - name: bar\n    chart: oci://y\n    version: 9.2.3\n"
        )
        ecv._update_helmfile_chart_pin(p, "9.3.0")
        text = p.read_text()
        self.assertIn("- name: foo\n    chart: oci://x\n    version: 9.3.0", text)
        self.assertIn("- name: bar\n    chart: oci://y\n    version: 9.2.3", text)


class ListChartVersionsTests(unittest.TestCase):
    def test_filters_by_chart_name_prefix(self) -> None:
        payload = json.dumps(
            [
                {"tag_name": "elasticsearch-eck-0.1.2", "prerelease": False, "draft": False},
                {"tag_name": "elasticsearch-eck-0.2.0", "prerelease": False, "draft": False},
                {"tag_name": "kibana-eck-0.1.2", "prerelease": False, "draft": False},
                {"tag_name": "elasticsearch-eck-0.3.0", "prerelease": True, "draft": False},
                {"tag_name": "elasticsearch-eck-not-semver", "prerelease": False, "draft": False},
            ]
        ).encode()
        with mock.patch.object(ecv, "http_get", return_value=payload):
            got = ecv._list_chart_versions(
                "github-releases", "somaz94/helm-charts", "elasticsearch-eck"
            )
        self.assertEqual(got, ["0.2.0", "0.1.2"])

    def test_unsupported_source_type(self) -> None:
        got = ecv._list_chart_versions("docker-hub-tags", "x/y", "name")
        self.assertEqual(got, [])

    def test_empty_repo(self) -> None:
        got = ecv._list_chart_versions("github-releases", "", "name")
        self.assertEqual(got, [])


class ReadHelmfileReleaseNameTests(unittest.TestCase):
    def test_skips_templated_then_picks_literal(self) -> None:
        """N5 fix: {{ skip BEFORE name extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "helmfile.yaml.gotmpl"
            f.write_text(
                "releases:\n"
                "  - name: {{ .Values.release }}\n"
                "  - name: actual-release\n"
                "    namespace: ns\n"
            )
            self.assertEqual(ecv._read_helmfile_release_name(f), "actual-release")

    def test_picks_first_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "helmfile.yaml"
            f.write_text("releases:\n  - name: foo\n  - name: bar\n")
            self.assertEqual(ecv._read_helmfile_release_name(f), "foo")


# =============================================================
# Argument parsing
# =============================================================


class ParseArgvTests(unittest.TestCase):
    _CFG = {"SCRIPT_NAME": "Test", "VALUES_FILE": "values/dev.yaml"}

    def test_default_is_stack(self) -> None:
        mode, *_ = ecv._parse_argv([], self._CFG, 5)
        self.assertEqual(mode, "stack")

    def test_dry_run(self) -> None:
        mode, _, _, dry, rc = ecv._parse_argv(["--dry-run"], self._CFG, 5)
        self.assertEqual((mode, dry, rc), ("stack", True, -1))

    def test_check_chart(self) -> None:
        mode, *_ = ecv._parse_argv(["--check-chart"], self._CFG, 5)
        self.assertEqual(mode, "check-chart")

    def test_upgrade_chart_with_version(self) -> None:
        mode, tv, tcv, dry, rc = ecv._parse_argv(
            ["--upgrade-chart", "--chart-version", "0.5.0"], self._CFG, 5
        )
        self.assertEqual((mode, tv, tcv, dry, rc), ("upgrade-chart", "", "0.5.0", False, -1))

    def test_stack_target_version(self) -> None:
        mode, tv, tcv, dry, rc = ecv._parse_argv(
            ["--version", "9.5.0"], self._CFG, 5
        )
        self.assertEqual((mode, tv, dry, rc), ("stack", "9.5.0", False, -1))

    def test_help_returns_zero(self) -> None:
        with redirect_stdout(io.StringIO()):
            _, _, _, _, rc = ecv._parse_argv(["--help"], self._CFG, 5)
        self.assertEqual(rc, 0)

    def test_unknown_option_returns_one(self) -> None:
        with redirect_stdout(io.StringIO()):
            _, _, _, _, rc = ecv._parse_argv(["--bogus"], self._CFG, 5)
        self.assertEqual(rc, 1)

    def test_chart_version_missing_arg(self) -> None:
        with redirect_stdout(io.StringIO()):
            _, _, _, _, rc = ecv._parse_argv(["--chart-version"], self._CFG, 5)
        self.assertEqual(rc, 1)

    def test_rollback_list_cleanup_modes(self) -> None:
        for arg, expected in (
            ("--rollback", "rollback"),
            ("--list-backups", "list-backups"),
            ("--cleanup-backups", "cleanup-backups"),
        ):
            mode, *_ = ecv._parse_argv([arg], self._CFG, 5)
            self.assertEqual(mode, expected)


# =============================================================
# _find_chart_root — helm pull layout (2 levels)
# =============================================================


class FindChartRootTests(unittest.TestCase):
    def test_finds_direct_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            chart_dir = parent / "mychart"
            chart_dir.mkdir()
            (chart_dir / "Chart.yaml").touch()
            self.assertEqual(ecv._find_chart_root(parent), chart_dir)

    def test_finds_nested_child(self) -> None:
        """Helm pull --untar may unpack into parent/<unpack-name>/<chart>/."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            outer = parent / "out"
            inner = outer / "chart"
            inner.mkdir(parents=True)
            (inner / "Chart.yaml").touch()
            self.assertEqual(ecv._find_chart_root(parent), inner)

    def test_returns_none_on_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ecv._find_chart_root(Path(tmp)))


# =============================================================
# run() integration — list-backups path (no network / no kubectl)
# =============================================================


class RunListBackupsIntegrationTests(unittest.TestCase):
    """End-to-end through ``run()`` on the safest no-side-effect path."""

    def _config(self) -> dict:
        return {
            "SCRIPT_NAME": "Test CR Upgrade",
            "COMPONENT_LABEL": "elasticsearch",
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
            "CHART_SOURCE_TYPE": "",
            "CHART_SOURCE_REPO": "",
            "CHART_NAME": "",
        }

    def test_list_backups_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            script_path = chart_dir / "upgrade.py"
            script_path.touch()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ecv.run(
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
                rc = ecv.run(self._config(), ["--help"], script_path=str(script_path))
            self.assertEqual(rc, 0)
            self.assertIn("Test CR Upgrade", buf.getvalue())

    def test_check_chart_without_source_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            script_path = chart_dir / "upgrade.py"
            script_path.touch()
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    ecv.run(
                        self._config(), ["--check-chart"], script_path=str(script_path)
                    )
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("chart pin tracking is not configured", buf.getvalue())


class RollbackTests(unittest.TestCase):
    """Rollback of a component ArgoCD delivers: never a helmfile, ArgoCD next steps."""

    CONFIG = {"VALUES_FILE": "values/dev.yaml", "VERSION_KEY": "version",
              "COMPONENT_LABEL": "elasticsearch"}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.chart_dir = self.tmp / "elasticsearch"
        (self.chart_dir / "values").mkdir(parents=True)
        (self.chart_dir / "values" / "dev.yaml").write_text("version: 9.5.4\n")
        self.backup_dir = self.chart_dir / "backup"
        # Never reach a cluster, even with KUBE_CONTEXT set in the environment.
        patcher = mock.patch.object(ecv, "get_live_cr_version", return_value="")
        self.live = patcher.start()
        self.addCleanup(patcher.stop)

    def _argocd(self) -> None:
        (self.chart_dir / "argocd").mkdir()
        (self.chart_dir / "argocd" / "elasticsearch.yaml").write_text(
            'chart:\n  name: elasticsearch-eck\n  version: "0.3.3"\n'
        )

    def _rollback(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("builtins.input", return_value=""), \
             redirect_stdout(out), redirect_stderr(err):
            code = ecv._do_rollback(self.CONFIG, self.chart_dir, self.backup_dir, None)
        return code, out.getvalue(), err.getvalue()

    def _chart_backup(self) -> None:
        b = self.backup_dir / "20260101_000000-chart"
        b.mkdir(parents=True)
        (b / "helmfile.yaml").write_text("releases:\n  - name: elasticsearch\n    version: 0.1.0\n")

    def _stack_backup(self, version: str) -> None:
        b = self.backup_dir / "20260101_000000"
        b.mkdir(parents=True)
        (b / "dev.yaml").write_text(f"version: {version}\n")

    def test_a_pre_argocd_chart_backup_restores_nothing(self) -> None:
        self._argocd()
        self._chart_backup()
        code, out, err = self._rollback()
        self.assertEqual(code, 1)
        self.assertFalse((self.chart_dir / "helmfile.yaml").exists())
        self.assertIn("Nothing was restored", err)
        self.assertIn("(chart pin 0.1.0)", err)
        self.assertIn("argocd/elasticsearch.yaml", err)
        self.assertIn("(chart, pre-ArgoCD, not restorable: 0.1.0)", out)

    def test_a_marker_dir_without_a_pin_still_gets_a_pointer(self) -> None:
        (self.chart_dir / "argocd").mkdir()
        self._chart_backup()
        _, _, err = self._rollback()
        self.assertIn("set chart.version in the argocd*/ markers by hand", err)

    def test_a_helmfile_component_still_restores_its_chart_pin(self) -> None:
        self._chart_backup()
        code, out, _ = self._rollback()
        self.assertEqual(code, 0)
        self.assertIn("version: 0.1.0", (self.chart_dir / "helmfile.yaml").read_text())
        self.assertIn("Chart pin rollback complete!", out)

    def test_a_stack_backup_restores_the_values_file(self) -> None:
        self._argocd()
        self._stack_backup("9.5.4")
        (self.chart_dir / "values" / "dev.yaml").write_text("version: 9.5.1\n")
        code, out, _ = self._rollback()
        self.assertEqual(code, 0)
        self.assertEqual((self.chart_dir / "values" / "dev.yaml").read_text(), "version: 9.5.4\n")
        self.assertIn("ArgoCD applies the CR version", out)
        self.assertNotIn("WARNING", out)
        self.assertNotIn("helmfile", out)

    def test_a_downgrade_is_restored_with_a_warning(self) -> None:
        self._argocd()
        self._stack_backup("9.5.1")
        code, out, _ = self._rollback()
        self.assertEqual(code, 0)
        self.assertEqual((self.chart_dir / "values" / "dev.yaml").read_text(), "version: 9.5.1\n")
        self.assertIn("version downgrade (9.5.4 -> 9.5.1, current version from the values file)", out)
        self.assertIn("git restore values/dev.yaml", out)
        self.assertNotIn("Rollback complete!", out)
        self.live.assert_called_once_with("elasticsearch", self.chart_dir / "argocd" / "elasticsearch.yaml")

    def test_the_live_cr_decides_when_readable(self) -> None:
        # A bump whose sync failed: git says 9.5.4, the cluster still runs 9.5.1.
        self._argocd()
        self._stack_backup("9.5.1")
        self.live.return_value = "9.5.1"
        code, out, _ = self._rollback()
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", out)
        self.assertIn("Rollback complete!", out)

    def test_a_live_downgrade_names_its_basis(self) -> None:
        self._argocd()
        self._stack_backup("9.5.1")
        (self.chart_dir / "values" / "dev.yaml").write_text("version: 9.5.1\n")
        self.live.return_value = "9.5.4"
        _, out, _ = self._rollback()
        self.assertIn("(9.5.4 -> 9.5.1, current version from the live CR)", out)

    def test_a_helmfile_component_keeps_the_webhook_flow(self) -> None:
        self._stack_backup("9.5.1")
        self.live.return_value = "9.5.4"
        with mock.patch.object(ecv, "handle_downgrade_rollback") as handler:
            code, _, _ = self._rollback()
        self.assertEqual(code, 0)
        handler.assert_called_once()


# =============================================================
# ArgoCD chart-pin redirect (Phase D — migrated CR components)
# =============================================================


_ES_ARGOCD_META = """\
component: elasticsearch
releaseName: elasticsearch
namespace: logging
chart:
  repoURL: ghcr.io/somaz94/charts
  name: elasticsearch-eck
  version: "0.1.9"
valueFile: observability/logging/elasticsearch/values/dev.yaml
autoSync: true
"""


class DetectArgocdPinFileTests(unittest.TestCase):
    def test_finds_file_with_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            argocd = chart_dir / "argocd"
            argocd.mkdir()
            (argocd / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
            found = ecv._detect_argocd_pin_file(chart_dir)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "elasticsearch.yaml")

    def test_no_argocd_dir_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ecv._detect_argocd_pin_file(Path(tmp)))

    def test_argocd_dir_without_pin_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            argocd = chart_dir / "argocd"
            argocd.mkdir()
            (argocd / "x.yaml").write_text("component: x\nchart:\n  name: x\n")
            self.assertIsNone(ecv._detect_argocd_pin_file(chart_dir))

    def test_finds_aws_marker_dir(self) -> None:
        # The AWS variant uses `argocd-aws/`, not `argocd/`.
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            argocd = chart_dir / "argocd-aws"
            argocd.mkdir()
            (argocd / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
            found = ecv._detect_argocd_pin_file(chart_dir)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "elasticsearch.yaml")
            self.assertEqual(found.parent.name, "argocd-aws")

    def test_onprem_marker_wins_when_both_present(self) -> None:
        # on-prem-first ordering: `argocd/` must win over `argocd-aws/` so a
        # hypothetical both-present component stays byte-identical to on-prem.
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            (chart_dir / "argocd").mkdir()
            (chart_dir / "argocd" / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
            (chart_dir / "argocd-aws").mkdir()
            (chart_dir / "argocd-aws" / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
            found = ecv._detect_argocd_pin_file(chart_dir)
            self.assertIsNotNone(found)
            self.assertEqual(found.parent.name, "argocd")


class ChartPinDispatcherTests(unittest.TestCase):
    """The dispatchers prefer the ArgoCD file when present, else helmfile."""

    def _argocd(self) -> Path:
        f = Path(tempfile.mkstemp(suffix=".yaml")[1])
        f.write_text(_ES_ARGOCD_META)
        return f

    def _helmfile(self) -> Path:
        f = Path(tempfile.mkstemp(suffix=".yaml")[1])
        f.write_text(
            "releases:\n  - name: es\n    chart: oci://ghcr.io/x/es\n"
            '    version: "0.5.0"\n'
        )
        return f

    def test_current_prefers_argocd(self) -> None:
        a, h = self._argocd(), self._helmfile()
        self.assertEqual(ecv._chart_pin_current(h, a), "0.1.9")  # argocd wins
        self.assertEqual(ecv._chart_pin_current(h, None), "0.5.0")  # helmfile

    def test_url_prefers_argocd(self) -> None:
        a, h = self._argocd(), self._helmfile()
        self.assertEqual(
            ecv._chart_pin_url(h, a), "oci://ghcr.io/somaz94/charts/elasticsearch-eck"
        )
        self.assertEqual(ecv._chart_pin_url(h, None), "oci://ghcr.io/x/es")

    def test_release_name_prefers_argocd(self) -> None:
        a, h = self._argocd(), self._helmfile()
        self.assertEqual(ecv._chart_pin_release_name(h, a), "elasticsearch")
        self.assertEqual(ecv._chart_pin_release_name(h, None), "es")

    def test_label_distinguishes_source(self) -> None:
        # The label uses the pin file's real parent dir: `argocd/` for on-prem,
        # `argocd-aws/` for the AWS variant (feeds git diff/restore commands).
        with tempfile.TemporaryDirectory() as tmp:
            onprem = Path(tmp) / "argocd"
            onprem.mkdir()
            f_on = onprem / "elasticsearch.yaml"
            f_on.write_text(_ES_ARGOCD_META)
            self.assertEqual(
                ecv._chart_pin_label("helmfile.yaml", f_on),
                "argocd/elasticsearch.yaml",
            )
            aws = Path(tmp) / "argocd-aws"
            aws.mkdir()
            f_aws = aws / "elasticsearch.yaml"
            f_aws.write_text(_ES_ARGOCD_META)
            self.assertEqual(
                ecv._chart_pin_label("helmfile.yaml", f_aws),
                "argocd-aws/elasticsearch.yaml",
            )
        self.assertEqual(ecv._chart_pin_label("helmfile.yaml", None), "helmfile.yaml")

    def test_write_fans_out_across_every_marker(self) -> None:
        # The regression this guards: a component enrolled on two delivery
        # tracks carries one metadata file per cluster. The READ path is
        # first-match, so bumping only the primary leaves the second cluster
        # pinned to the old chart AND keeps reporting the new version -- the
        # drift is invisible. Unlike the `argocd-pin` template there is no
        # CONFIG.ARGOCD_PIN_FILES here; auto-discovery is the only guard.
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            files = {}
            for marker in ("argocd", "argocd-aws"):
                d = chart_dir / marker
                d.mkdir()
                f = d / "elasticsearch.yaml"
                f.write_text(_ES_ARGOCD_META)
                files[marker] = f
            primary = ecv._detect_argocd_pin_file(chart_dir)
            written, skipped = ecv._chart_pin_write(None, primary, "0.1.9", "0.1.10")
            self.assertEqual(len(written), 2)
            self.assertEqual(skipped, [])
            for marker, f in files.items():
                self.assertIn('  version: "0.1.10"', f.read_text(), marker)

    def test_write_skips_and_reports_a_divergent_marker(self) -> None:
        # A marker deliberately held at another version is never force-matched
        # (update_argocd_chart_version only rewrites an exact `current`); it
        # comes back in `skipped` so the caller can warn instead of the bump
        # silently half-landing.
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            onprem = chart_dir / "argocd"
            onprem.mkdir()
            (onprem / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
            other = chart_dir / "argocd-aws"
            other.mkdir()
            held = other / "elasticsearch.yaml"
            held.write_text(_ES_ARGOCD_META.replace('"0.1.9"', '"0.1.4"'))
            primary = ecv._detect_argocd_pin_file(chart_dir)
            written, skipped = ecv._chart_pin_write(None, primary, "0.1.9", "0.1.10")
            self.assertEqual([f.parent.name for f in written], ["argocd"])
            self.assertEqual([f.parent.name for f in skipped], ["argocd-aws"])
            self.assertIn('  version: "0.1.4"', held.read_text())

    def test_detect_pin_files_returns_every_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart_dir = Path(tmp)
            for marker in ("argocd-aws", "argocd-unlisted", "argocd"):
                d = chart_dir / marker
                d.mkdir()
                (d / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
            found = ecv._detect_argocd_pin_files(chart_dir)
            # Marker-order, not filesystem order; unlisted dirs are ignored.
            self.assertEqual(
                [f.parent.name for f in found], ["argocd", "argocd-aws"]
            )

    def test_write_returns_helmfile_when_no_argocd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hf = Path(tmp) / "helmfile.yaml"
            hf.write_text(
                "releases:\n  - name: es\n    chart: oci://x/es\n    version: 0.1.9\n"
            )
            written, skipped = ecv._chart_pin_write(hf, None, "0.1.9", "0.1.10")
            self.assertEqual(written, [hf])
            self.assertEqual(skipped, [])

    def test_write_targets_argocd_when_present(self) -> None:
        a = self._argocd()
        ecv._chart_pin_write(None, a, "0.1.9", "0.1.10")
        self.assertIn('  version: "0.1.10"', a.read_text())

    def test_write_targets_helmfile_when_no_argocd(self) -> None:
        h = self._helmfile()
        ecv._chart_pin_write(h, None, "0.5.0", "0.6.0")
        self.assertIn('    version: "0.6.0"', h.read_text())


class CheckChartArgocdIntegrationTests(unittest.TestCase):
    """run(--check-chart) reads the current pin from ArgoCD metadata when the
    component has no helmfile (migrated CR component)."""

    def _config(self) -> dict:
        return {
            "SCRIPT_NAME": "ES CR Upgrade",
            "COMPONENT_LABEL": "elasticsearch",
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
            "CHART_SOURCE_TYPE": "github-releases",
            "CHART_SOURCE_REPO": "somaz94/helm-charts",
            "CHART_NAME": "elasticsearch-eck",
        }

    def _build(self, tmp: str) -> Path:
        chart_dir = Path(tmp)
        (chart_dir / "argocd").mkdir()
        (chart_dir / "argocd" / "elasticsearch.yaml").write_text(_ES_ARGOCD_META)
        script_path = chart_dir / "upgrade.py"
        script_path.touch()
        return script_path

    def test_check_chart_update_available_from_argocd(self) -> None:
        payload = json.dumps(
            [{"tag_name": "elasticsearch-eck-0.1.10", "prerelease": False, "draft": False}]
        ).encode()
        with tempfile.TemporaryDirectory() as tmp:
            script_path = self._build(tmp)
            buf = io.StringIO()
            with mock.patch.object(ecv, "http_get", return_value=payload):
                with redirect_stdout(buf):
                    rc = ecv.run(
                        self._config(), ["--check-chart"], script_path=str(script_path)
                    )
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("Current pin (argocd/elasticsearch.yaml): 0.1.9", out)
            self.assertIn("UPDATE AVAILABLE — 0.1.9 -> 0.1.10", out)

    def test_check_chart_up_to_date_from_argocd(self) -> None:
        payload = json.dumps(
            [{"tag_name": "elasticsearch-eck-0.1.9", "prerelease": False, "draft": False}]
        ).encode()
        with tempfile.TemporaryDirectory() as tmp:
            script_path = self._build(tmp)
            buf = io.StringIO()
            with mock.patch.object(ecv, "http_get", return_value=payload):
                with redirect_stdout(buf):
                    rc = ecv.run(
                        self._config(), ["--check-chart"], script_path=str(script_path)
                    )
            self.assertEqual(rc, 0)
            self.assertIn("up to date", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
