"""Unit tests for upgrade_core/external_oci.py.

The ``external_oci`` module is a thin extension of :mod:`external_standard` via three
hook injection points (during the shell -> python migration). Coverage focuses
on the ``external_oci`` deltas:

  - ``_fetch_latest_via_github`` — single-chart vs multi-chart prefix.
  - ``_patch_wrapper_chart_version`` — quote-preserving Chart.yaml patch.
  - ``_update_helmfile_pins_tracked_scope`` — release-block scoped pin.
  - ``run()`` integration on the 4 consumer config shapes.

Stdlib unittest only.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

eo = load("upgrade_core.external_oci")
cm = load("upgrade_core._common")


# =============================================================
# _fetch_latest_via_github — single-chart and multi-chart prefix
# =============================================================


class FetchLatestViaGithubTests(unittest.TestCase):
    def test_single_chart_releases_latest(self) -> None:
        # When tag_prefix is "v", we hit /releases/latest and read tag_name.
        payload = json.dumps({"tag_name": "v2.5.1"}).encode("utf-8")
        with mock.patch.object(cm, "_github_releases_request", return_value=payload.decode()):
            ver, tag = eo._fetch_latest_via_github("nginx/nginx-gateway-fabric", "v")
        self.assertEqual(ver, "2.5.1")
        self.assertEqual(tag, "v2.5.1")

    def test_multi_chart_prefix_picks_first_match(self) -> None:
        # When tag_prefix is "keycloak-cr-", scan releases and return the
        # first matching tag.
        payload = json.dumps([
            {"tag_name": "keycloak-operator-0.1.0"},
            {"tag_name": "keycloak-cr-0.2.0"},
            {"tag_name": "keycloak-cr-0.1.0"},
        ]).encode("utf-8")
        with mock.patch.object(cm, "_github_releases_request", return_value=payload.decode()):
            ver, tag = eo._fetch_latest_via_github("somaz94/helm-charts", "keycloak-cr-")
        # First matching tag is `keycloak-cr-0.2.0` → ver after strip = "0.2.0".
        self.assertEqual(ver, "0.2.0")
        self.assertEqual(tag, "keycloak-cr-0.2.0")

    def test_no_releases_returns_empty(self) -> None:
        with mock.patch.object(cm, "_github_releases_request", return_value=""):
            ver, tag = eo._fetch_latest_via_github("foo/bar", "v")
        self.assertEqual((ver, tag), ("", ""))

    def test_empty_prefix_keeps_tag(self) -> None:
        # Empty prefix → no strip, tag returned as-is. We must use the
        # multi-chart branch in cm to honor empty prefix (the /latest
        # endpoint also works but returns the bare tag).
        payload = json.dumps({"tag_name": "1.2.3"}).encode("utf-8")
        with mock.patch.object(cm, "_github_releases_request", return_value=payload.decode()):
            ver, tag = eo._fetch_latest_via_github("foo/bar", "")
        self.assertEqual(ver, "1.2.3")
        self.assertEqual(tag, "1.2.3")


# =============================================================
# _patch_wrapper_chart_version — quote-preserving
# =============================================================


class WrapperChartPatchTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "Chart.yaml"
        f.write_text(body)
        return f

    def test_quoted_version_preserves_quotes(self) -> None:
        f = self._write(
            'apiVersion: v2\nname: keycloak\nversion: "0.1.0"\nappVersion: "24.0.4"\n'
        )
        eo._patch_wrapper_chart_version(f, "0.1.0", "0.2.0")
        result = f.read_text()
        self.assertIn('version: "0.2.0"', result)
        # appVersion stays — wrapper mode preserves it.
        self.assertIn('appVersion: "24.0.4"', result)

    def test_bare_version_preserves_bare(self) -> None:
        f = self._write("version: 1.0.0\nname: x\n")
        eo._patch_wrapper_chart_version(f, "1.0.0", "1.1.0")
        self.assertIn("version: 1.1.0\n", f.read_text())

    def test_only_first_version_patched(self) -> None:
        # When two `version:` lines exist (unusual but possible in raw
        # YAML), only the first is patched (matches the bash awk
        # `next` semantics).
        f = self._write('version: 1.0.0\n# duplicated:\nversion: 1.0.0\n')
        eo._patch_wrapper_chart_version(f, "1.0.0", "1.1.0")
        text = f.read_text()
        self.assertEqual(text.count("version: 1.1.0"), 1)
        self.assertEqual(text.count("version: 1.0.0"), 1)

    def test_no_match_when_current_mismatches(self) -> None:
        f = self._write('version: "9.9.9"\n')
        eo._patch_wrapper_chart_version(f, "1.0.0", "1.1.0")
        self.assertIn('"9.9.9"', f.read_text())


# =============================================================
# _update_helmfile_pins_tracked_scope — release-block scoped
# =============================================================


class TrackedScopePinTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "helmfile.yaml"
        f.write_text(body)
        return f

    def test_only_tracked_release_block_updates(self) -> None:
        body = (
            "releases:\n"
            "  - name: keycloak\n"
            "    chart: oci://ghcr.io/somaz94/charts/keycloak-cr\n"
            '    version: "0.1.0"\n'
            "  - name: keycloak-postgresql\n"
            "    chart: oci://ghcr.io/somaz94/charts/postgresql\n"
            '    version: "0.1.0"\n'
        )
        f = self._write(body)
        n = eo._update_helmfile_pins_tracked_scope(
            f, "0.1.0", "0.2.0", "oci://ghcr.io/somaz94/charts/keycloak-cr"
        )
        self.assertEqual(n, 1)
        result = f.read_text()
        # keycloak release bumped, postgresql sibling untouched.
        self.assertIn('version: "0.2.0"', result)  # keycloak
        self.assertEqual(result.count('version: "0.1.0"'), 1)  # postgresql

    def test_no_match_when_tracked_chart_absent(self) -> None:
        body = (
            "releases:\n"
            "  - name: foo\n"
            "    chart: oci://example/foo\n"
            '    version: "1.0.0"\n'
        )
        f = self._write(body)
        n = eo._update_helmfile_pins_tracked_scope(
            f, "1.0.0", "1.1.0", "oci://different/chart"
        )
        self.assertEqual(n, 0)
        self.assertIn('"1.0.0"', f.read_text())

    def test_bare_version_in_tracked_block(self) -> None:
        body = (
            "releases:\n"
            "  - name: x\n"
            "    chart: oci://example/x\n"
            "    version: 1.0.0\n"
        )
        f = self._write(body)
        n = eo._update_helmfile_pins_tracked_scope(
            f, "1.0.0", "1.1.0", "oci://example/x"
        )
        self.assertEqual(n, 1)
        self.assertIn("version: 1.1.0\n", f.read_text())


# =============================================================
# _helmfile_pin_default_or_scoped — dispatch
# =============================================================


class HelmfilePinDispatchTests(unittest.TestCase):
    def test_empty_tracked_falls_back_to_baseline(self) -> None:
        with mock.patch.object(eo, "update_helmfile_pins", return_value=3) as mu:
            n = eo._helmfile_pin_default_or_scoped(
                helmfile_path=Path("/tmp/x"),
                helmfile_name="helmfile.yaml",
                current_version="1.0.0",
                latest_version="1.1.0",
                tracked_chart="",
            )
        mu.assert_called_once()
        self.assertEqual(n, 3)

    def test_tracked_routes_to_scoped(self) -> None:
        with mock.patch.object(eo, "_update_helmfile_pins_tracked_scope", return_value=1) as ms:
            n = eo._helmfile_pin_default_or_scoped(
                helmfile_path=Path("/tmp/x"),
                helmfile_name="helmfile.yaml",
                current_version="1.0.0",
                latest_version="1.1.0",
                tracked_chart="oci://example/x",
            )
        ms.assert_called_once()
        self.assertEqual(n, 1)


# =============================================================
# Run() hook wiring — confirm closure carries CONFIG keys
# =============================================================


class RunHookWiringTests(unittest.TestCase):
    def test_run_passes_three_hooks_to_external_standard(self) -> None:
        captured: dict = {}

        def fake_run(config, argv, script_path, **hooks):
            captured.update(hooks)
            return 0

        cfg = {
            "SCRIPT_NAME": "x",
            "HELM_REPO_NAME": "",
            "HELM_REPO_URL": "",
            "HELM_CHART": "oci://example/x",
            "GITHUB_REPO": "owner/repo",
            "GITHUB_TAG_PREFIX": "v",
            "CHANGELOG_URL": "https://example/CHANGELOG",
            "CHART_TYPE": "external",
            "WRAPPER_CHART_YAML": False,
            "HELMFILE_TRACKED_CHART": "",
        }
        with mock.patch.object(eo, "_run_external_standard", side_effect=fake_run):
            rc = eo.run(cfg, [], "/tmp/upgrade.py")
        self.assertEqual(rc, 0)
        self.assertIn("fetch_latest_hook", captured)
        self.assertIn("chart_write_hook", captured)
        self.assertIn("helmfile_pin_hook", captured)
        self.assertIsNotNone(captured["fetch_latest_hook"])
        self.assertIsNotNone(captured["chart_write_hook"])
        self.assertIsNotNone(captured["helmfile_pin_hook"])


# =============================================================
# 4 ``external_oci`` consumer CONFIG spot-check
# =============================================================
if __name__ == "__main__":
    unittest.main()
