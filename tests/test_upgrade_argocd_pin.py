"""Unit + integration tests for upgrade_core/argocd_pin.py.

The ``argocd-pin`` template is a thin dispatcher that reuses the
external_standard / external_oci_with_mirror flow and swaps only the
version-pin WRITE target — via the ``pin_write_hook`` extension point —
so the chart version lands in ``<component>/argocd/<release>.yaml``
instead of a helmfile (the migrated components have no helmfile).

Coverage:
  - ``_make_pin_write_hook`` — resolves ARGOCD_PIN_FILES relative to the
    component dir and fans out to ``_common_argocd.update_argocd_pins``.
  - ``run()`` dispatch — BASE routing + error guards.
  - Integration — a real external_standard.run() with ``pin_write_hook``
    bumps the ArgoCD metadata pin (no helmfile present), proving the
    end-to-end wiring.
  - Regression — ``pin_write_hook=None`` keeps the helmfile pin path.

Stdlib unittest only.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ap = load("upgrade_core.argocd_pin")
es = load("upgrade_core.external_standard")
ca = load("upgrade_core._common_argocd")


def _fake_subprocess(handler):
    def _run(cmd, **kwargs):
        text = handler(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=text, stderr="")
    return _run


# =============================================================
# _make_pin_write_hook
# =============================================================


class MakePinWriteHookTests(unittest.TestCase):
    def test_resolves_relative_files_and_fans_out(self) -> None:
        chart_dir = Path(tempfile.mkdtemp())
        argocd = chart_dir / "argocd"
        argocd.mkdir()
        (argocd / "a.yaml").write_text("chart:\n  version: 1.0.0\n")
        (argocd / "b.yaml").write_text("chart:\n  version: 1.0.0\n")

        hook = ap._make_pin_write_hook(["argocd/a.yaml", "argocd/b.yaml"])
        n = hook(chart_dir=chart_dir, current_version="1.0.0", latest_version="1.1.0")
        self.assertEqual(n, 2)
        self.assertIn("version: 1.1.0", (argocd / "a.yaml").read_text())
        self.assertIn("version: 1.1.0", (argocd / "b.yaml").read_text())


# =============================================================
# run() dispatch + guards
# =============================================================


class RunDispatchTests(unittest.TestCase):
    BASE_CFG = {
        "SCRIPT_NAME": "x",
        "HELM_REPO_NAME": "x",
        "HELM_REPO_URL": "https://x.example",
        "HELM_CHART": "x/x",
        "CHANGELOG_URL": "https://example/CHANGELOG.md",
        "CHART_TYPE": "external",
        "ARGOCD_PIN_FILES": ["argocd/x.yaml"],
    }

    def test_base_standard_routes_to_external_standard_with_hook(self) -> None:
        captured: dict = {}

        def fake_run(config, argv, script_path, **hooks):
            captured.update(hooks)
            return 0

        cfg = dict(self.BASE_CFG, BASE="standard")
        with mock.patch.object(ap, "_run_external_standard", side_effect=fake_run):
            rc = ap.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 0)
        self.assertIn("pin_write_hook", captured)
        self.assertIsNotNone(captured["pin_write_hook"])

    def test_base_oci_routes_to_oci_with_mirror_with_hook(self) -> None:
        captured: dict = {}

        def fake_run(config, argv, script_path, **hooks):
            captured.update(hooks)
            return 0

        cfg = dict(self.BASE_CFG, BASE="oci", GITHUB_REPO="owner/repo")
        with mock.patch.object(ap, "_run_oci_with_mirror", side_effect=fake_run):
            rc = ap.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 0)
        self.assertIn("pin_write_hook", captured)
        self.assertIsNotNone(captured["pin_write_hook"])

    def test_default_base_is_standard(self) -> None:
        captured: dict = {}

        def fake_run(config, argv, script_path, **hooks):
            captured.update(hooks)
            return 0

        cfg = dict(self.BASE_CFG)  # no BASE key
        with mock.patch.object(ap, "_run_external_standard", side_effect=fake_run):
            rc = ap.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 0)
        self.assertIn("pin_write_hook", captured)

    def test_missing_argocd_pin_files_errors(self) -> None:
        cfg = {k: v for k, v in self.BASE_CFG.items() if k != "ARGOCD_PIN_FILES"}
        cfg["BASE"] = "standard"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ap.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 1)

    def test_empty_argocd_pin_files_errors(self) -> None:
        cfg = dict(self.BASE_CFG, BASE="standard", ARGOCD_PIN_FILES=[])
        rc = ap.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 1)

    def test_unknown_base_errors(self) -> None:
        cfg = dict(self.BASE_CFG, BASE="bogus")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ap.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 1)


# =============================================================
# Integration — pin_write_hook bumps the ArgoCD metadata
# =============================================================


class ArgocdPinApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "comp"
        self.chart_dir.mkdir()
        # Migrated component layout: Chart.yaml (mirror) + values + argocd/,
        # NO helmfile (retired to backup/).
        (self.chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: x\nversion: 1.0.0\nappVersion: 7.0.0\n"
        )
        (self.chart_dir / "values.yaml").write_text("global:\n  foo: 1\n")
        (self.chart_dir / "values").mkdir()
        (self.chart_dir / "values" / "dev.yaml").write_text("global:\n  foo: 2\n")
        argocd = self.chart_dir / "argocd"
        argocd.mkdir()
        self.release = argocd / "release.yaml"
        self.release.write_text(
            "component: x\nreleaseName: x\nchart:\n"
            "  repoURL: https://x.example\n  name: x\n"
            '  version: "1.0.0"\n'
            "valueFile: comp/values/dev.yaml\n"
        )
        self.script = self.chart_dir / "upgrade.py"
        self.script.write_text("# stub\n")
        self.config = {
            "SCRIPT_NAME": "Argocd-pin Test",
            "HELM_REPO_NAME": "x",
            "HELM_REPO_URL": "https://x.example",
            "HELM_CHART": "x/x",
            "CHANGELOG_URL": "https://example/CHANGELOG.md",
            "CHART_TYPE": "external",
            "BASE": "standard",
            "ARGOCD_PIN_FILES": ["argocd/release.yaml"],
        }

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _handler(self, cmd):
        if cmd[:2] == ["helm", "search"]:
            return '[{"version":"1.1.0","app_version":"7.1.0"}]'
        if cmd[:3] == ["helm", "show", "chart"]:
            return "apiVersion: v2\nname: x\nversion: 1.1.0\nappVersion: 7.1.0\n"
        if cmd[:3] == ["helm", "show", "values"]:
            return "global:\n  foo: 2\n"
        if cmd[:2] == ["helm", "pull"]:
            return ""
        if cmd and cmd[0] == "diff":
            return ""
        return ""

    def test_apply_bumps_argocd_pin(self) -> None:
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._handler)):
            with redirect_stdout(buf):
                rc = ap.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Updated version pin (1 file(s): 1.0.0 -> 1.1.0)", out)
        # ArgoCD metadata pin flipped, quote style preserved.
        self.assertIn('  version: "1.1.0"', self.release.read_text())
        # Local Chart.yaml mirror also refreshed to the new version.
        self.assertIn("version: 1.1.0", (self.chart_dir / "Chart.yaml").read_text())

    def test_dry_run_leaves_argocd_pin_untouched(self) -> None:
        original = self.release.read_text()
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._handler)):
            with redirect_stdout(buf):
                rc = ap.run(self.config, ["--dry-run"], self.script)
        self.assertEqual(rc, 0)
        self.assertEqual(self.release.read_text(), original)


# =============================================================
# Regression — pin_write_hook=None keeps the helmfile pin path
# =============================================================


class HelmfilePathUnaffectedTests(unittest.TestCase):
    def test_no_hook_still_writes_helmfile(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        chart_dir = tmp / "comp"
        chart_dir.mkdir()
        (chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: x\nversion: 1.0.0\nappVersion: 7.0.0\n"
        )
        (chart_dir / "values.yaml").write_text("global:\n  foo: 1\n")
        (chart_dir / "values").mkdir()
        helmfile = chart_dir / "helmfile.yaml"
        helmfile.write_text(
            "releases:\n  - name: x\n    chart: x/x\n    version: 1.0.0\n"
        )
        script = chart_dir / "upgrade.py"
        script.write_text("# stub\n")
        config = {
            "SCRIPT_NAME": "x",
            "HELM_REPO_NAME": "x",
            "HELM_REPO_URL": "https://x.example",
            "HELM_CHART": "x/x",
            "CHANGELOG_URL": "https://example/CHANGELOG.md",
            "CHART_TYPE": "external",
        }

        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"1.1.0","app_version":"7.1.0"}]'
            if cmd[:3] == ["helm", "show", "chart"]:
                return "apiVersion: v2\nname: x\nversion: 1.1.0\nappVersion: 7.1.0\n"
            if cmd[:3] == ["helm", "show", "values"]:
                return "global:\n  foo: 2\n"
            if cmd[:2] == ["helm", "pull"]:
                return ""
            if cmd and cmd[0] == "diff":
                return ""
            return ""

        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = es.run(config, [], script)  # no pin_write_hook
        self.assertEqual(rc, 0)
        # Helmfile pin flipped via the default path.
        self.assertIn("version: 1.1.0", helmfile.read_text())
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================
# Pin-only components — no local Chart.yaml mirror
# =============================================================


class ArgocdPinNoLocalChartYamlTests(unittest.TestCase):
    """A component whose version SSOT is the ArgoCD metadata file and which
    ships NO local Chart.yaml mirror.

    Before ``current_version_hook``, Step 1 read the absent Chart.yaml, got
    "", and every downstream step degraded silently: the values diff compared
    latest against latest, the breaking-change scan reported nothing removed,
    and the pin rewrite matched 0 files while still printing success and
    returning 0.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "comp"
        self.chart_dir.mkdir()
        # Pin-only layout: values/ + argocd/ only. No Chart.yaml, no
        # values.yaml, no helmfile.
        (self.chart_dir / "values").mkdir()
        (self.chart_dir / "values" / "prod.yaml").write_text("global:\n  foo: 2\n")
        argocd = self.chart_dir / "argocd-aws"
        argocd.mkdir()
        self.release = argocd / "release.yaml"
        self.release.write_text(
            "component: x\nreleaseName: x\nchart:\n"
            "  repoURL: https://x.example\n  name: x\n"
            '  version: "1.0.0"\n'
            "autoSync: true\n"
        )
        self.script = self.chart_dir / "upgrade.py"
        self.script.write_text("# stub\n")
        self.config = {
            "SCRIPT_NAME": "Argocd-pin Pin-only Test",
            "HELM_REPO_NAME": "x",
            "HELM_REPO_URL": "https://x.example",
            "HELM_CHART": "x/x",
            "CHANGELOG_URL": "https://example/CHANGELOG.md",
            "CHART_TYPE": "external",
            "BASE": "standard",
            "ARGOCD_PIN_FILES": ["argocd-aws/release.yaml"],
        }

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _handler(self, cmd):
        if cmd[:2] == ["helm", "search"]:
            return '[{"version":"1.1.0","app_version":"7.1.0"}]'
        if cmd[:3] == ["helm", "show", "chart"]:
            return "apiVersion: v2\nname: x\nversion: 1.1.0\nappVersion: 7.1.0\n"
        if cmd[:3] == ["helm", "show", "values"]:
            return "global:\n  foo: 2\n"
        if cmd[:2] == ["helm", "pull"]:
            return ""
        if cmd and cmd[0] == "diff":
            return ""
        return ""

    def _run(self, argv):
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._handler)):
            with redirect_stdout(buf):
                rc = ap.run(self.config, argv, self.script)
        return rc, buf.getvalue()

    def test_step1_reads_current_version_from_pin(self) -> None:
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("current version read from the version pin", out)
        # The left-hand side of the upgrade arrow is populated, not blank.
        self.assertIn("Installed - Chart: 1.0.0", out)

    def test_apply_bumps_pin_without_creating_chart_mirror(self) -> None:
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("Updated version pin (1 file(s): 1.0.0 -> 1.1.0)", out)
        self.assertIn('  version: "1.1.0"', self.release.read_text())
        # The mirror is NOT fabricated.
        self.assertIn("Skipped local chart mirror write", out)
        self.assertFalse((self.chart_dir / "Chart.yaml").exists())
        self.assertFalse((self.chart_dir / "values.yaml").exists())
        self.assertFalse((self.chart_dir / "values.schema.json").exists())

    def test_dry_run_writes_nothing(self) -> None:
        original = self.release.read_text()
        rc, _ = self._run(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.release.read_text(), original)
        self.assertFalse((self.chart_dir / "Chart.yaml").exists())

    def test_unresolvable_current_version_aborts(self) -> None:
        """No Chart.yaml AND a pin file that carries no chart.version — the
        run must fail loudly instead of proceeding with an empty current."""
        self.release.write_text("component: x\nreleaseName: x\nautoSync: true\n")
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._handler)):
            with redirect_stdout(buf), redirect_stderr(err):
                rc = ap.run(self.config, [], self.script)
        self.assertEqual(rc, 1)
        self.assertIn("could not determine the current chart version", err.getvalue())
        self.assertFalse((self.chart_dir / "Chart.yaml").exists())

    def test_pin_rewrite_matching_zero_files_fails(self) -> None:
        """The pin SSOT sits at a version other than the resolved current
        (e.g. hand-edited between runs), so the rewrite matches nothing. That
        used to print success and return 0 — a green no-op deploy."""
        (self.chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: x\nversion: 0.9.0\nappVersion: 6.0.0\n"
        )
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._handler)):
            with redirect_stdout(buf), redirect_stderr(err):
                rc = ap.run(self.config, [], self.script)
        self.assertEqual(rc, 1)
        self.assertIn("matched 0 file(s)", err.getvalue())
        # The pin is left exactly as it was.
        self.assertIn('  version: "1.0.0"', self.release.read_text())


if __name__ == "__main__":
    unittest.main()
