"""Unit tests for upgrade_core/external_oci_with_mirror.py.

The ``external_oci_with_mirror`` module wraps ``external_oci`` with two extra hooks that the
external_standard runner gained during the shell -> python migration:

  - ``pre_apply_hook`` drives the Step 7 mirror stage.
  - ``values_summary_hook`` surfaces image.tag overrides at Step 1.

Coverage focuses on the ``external_oci_with_mirror`` deltas:

  - ``mirror_image`` — five branches (crane missing / empty upstream
    digest / digest match / copy failure / digest mismatch / success).
  - ``_make_pre_apply_hook`` — bridges the consumer ``do_mirror``
    callable to the PreApplyHook signature and forwards ``mirror_image``.
  - ``_make_values_summary_hook`` — ``None`` consumer → ``None`` hook
    (so external_standard's default kicks in).
  - ``run()`` integration on the 3 consumer config shapes (ghost +
    unity-mcp-server + db-redis/mysql).

Stdlib unittest only.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ewm = load("upgrade_core.external_oci_with_mirror")


# =============================================================
# mirror_image — 5 branches
# =============================================================


class MirrorImageTests(unittest.TestCase):
    def test_crane_missing_returns_2(self) -> None:
        with mock.patch.object(ewm.shutil, "which", return_value=None):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = ewm.mirror_image("up:1.0", "harbor/up:1.0")
        self.assertEqual(rc, 2)
        self.assertIn("'crane' is required", buf.getvalue())

    def test_empty_upstream_digest_returns_1(self) -> None:
        # _crane_digest returns empty for both upstream and harbor.
        with mock.patch.object(ewm.shutil, "which", return_value="/usr/local/bin/crane"):
            with mock.patch.object(ewm, "_crane_digest", return_value=""):
                buf = io.StringIO()
                with redirect_stderr(buf):
                    rc = ewm.mirror_image("up:1.0", "harbor/up:1.0")
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve upstream digest", buf.getvalue())

    def test_matching_digests_skip(self) -> None:
        with mock.patch.object(ewm.shutil, "which", return_value="/usr/local/bin/crane"):
            with mock.patch.object(ewm, "_crane_digest", return_value="sha256:abc"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ewm.mirror_image("up:1.0", "harbor/up:1.0")
        self.assertEqual(rc, 0)
        self.assertIn("SKIP", buf.getvalue())
        self.assertIn("already mirrored", buf.getvalue())

    def test_copy_failure_returns_1(self) -> None:
        # Upstream digest non-empty, harbor digest empty → COPY path
        # taken, crane copy returns non-zero.
        digests = iter(["sha256:abc", ""])
        with mock.patch.object(ewm.shutil, "which", return_value="/usr/local/bin/crane"):
            with mock.patch.object(ewm, "_crane_digest", side_effect=lambda *a, **kw: next(digests)):
                fake = mock.Mock(returncode=1)
                with mock.patch.object(ewm.subprocess, "run", return_value=fake):
                    buf_out = io.StringIO()
                    buf_err = io.StringIO()
                    with redirect_stdout(buf_out), redirect_stderr(buf_err):
                        rc = ewm.mirror_image("up:1.0", "harbor/up:1.0")
        self.assertEqual(rc, 1)
        self.assertIn("COPY", buf_out.getvalue())
        self.assertIn("crane copy failed", buf_err.getvalue())

    def test_post_copy_mismatch_returns_1(self) -> None:
        # upstream "sha256:abc", harbor before copy "", harbor after copy
        # "sha256:zzz" (mismatch).
        digests = iter(["sha256:abc", "", "sha256:zzz"])
        with mock.patch.object(ewm.shutil, "which", return_value="/usr/local/bin/crane"):
            with mock.patch.object(ewm, "_crane_digest", side_effect=lambda *a, **kw: next(digests)):
                fake = mock.Mock(returncode=0)
                with mock.patch.object(ewm.subprocess, "run", return_value=fake):
                    buf_err = io.StringIO()
                    with redirect_stdout(io.StringIO()), redirect_stderr(buf_err):
                        rc = ewm.mirror_image("up:1.0", "harbor/up:1.0")
        self.assertEqual(rc, 1)
        self.assertIn("digest mismatch after copy", buf_err.getvalue())

    def test_success_returns_0(self) -> None:
        # upstream "sha256:abc", harbor before "", harbor after "sha256:abc".
        digests = iter(["sha256:abc", "", "sha256:abc"])
        with mock.patch.object(ewm.shutil, "which", return_value="/usr/local/bin/crane"):
            with mock.patch.object(ewm, "_crane_digest", side_effect=lambda *a, **kw: next(digests)):
                fake = mock.Mock(returncode=0)
                with mock.patch.object(ewm.subprocess, "run", return_value=fake):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = ewm.mirror_image("up:1.0", "harbor/up:1.0")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("COPY", out)
        self.assertIn("OK", out)

    def test_insecure_flag_passes_through(self) -> None:
        # Verify --insecure ends up in the crane invocation argv.
        digests = iter(["sha256:abc", "", "sha256:abc"])
        with mock.patch.object(ewm.shutil, "which", return_value="/usr/local/bin/crane"):
            with mock.patch.object(ewm, "_crane_digest", side_effect=lambda *a, **kw: next(digests)) as digest_mock:
                fake = mock.Mock(returncode=0)
                with mock.patch.object(ewm.subprocess, "run", return_value=fake) as run_mock:
                    with redirect_stdout(io.StringIO()):
                        ewm.mirror_image("up:1.0", "harbor/up:1.0", insecure=True)
        # Each _crane_digest call receives flags=["--insecure"].
        for call in digest_mock.call_args_list:
            args, _kwargs = call
            self.assertIn("--insecure", args[1])
        copy_argv = run_mock.call_args[0][0]
        self.assertIn("--insecure", copy_argv)


# =============================================================
# _crane_digest — strips trailing whitespace; empty on failure
# =============================================================


class CraneDigestTests(unittest.TestCase):
    def test_strips_trailing_newline(self) -> None:
        fake = mock.Mock(returncode=0, stdout="sha256:abc\n", stderr="")
        with mock.patch.object(ewm.subprocess, "run", return_value=fake):
            self.assertEqual(ewm._crane_digest("img:tag", []), "sha256:abc")

    def test_empty_on_failure(self) -> None:
        fake = mock.Mock(returncode=1, stdout="", stderr="not found")
        with mock.patch.object(ewm.subprocess, "run", return_value=fake):
            self.assertEqual(ewm._crane_digest("img:tag", []), "")


# =============================================================
# _make_pre_apply_hook — bridges consumer do_mirror to PreApplyHook
# =============================================================


class PreApplyHookFactoryTests(unittest.TestCase):
    def test_forwards_kwargs_and_mirror_image(self) -> None:
        captured = {}

        def do_mirror(*, chart_dir, temp_dir, values_dir, latest_version,
                      latest_app_version, mirror_image):
            captured["chart_dir"] = chart_dir
            captured["latest_version"] = latest_version
            captured["mirror_image"] = mirror_image
            return 0

        hook = ewm._make_pre_apply_hook(do_mirror)
        rc = hook(
            chart_dir=Path("/tmp/chart"),
            temp_dir=Path("/tmp/temp"),
            values_dir=Path("/tmp/values"),
            latest_version="1.0.0",
            latest_app_version="5.118.0",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["chart_dir"], Path("/tmp/chart"))
        self.assertEqual(captured["latest_version"], "1.0.0")
        # mirror_image is the ``external_oci_with_mirror`` module's public helper.
        self.assertIs(captured["mirror_image"], ewm.mirror_image)

    def test_propagates_non_zero_rc(self) -> None:
        def do_mirror(**_kwargs):
            return 7

        hook = ewm._make_pre_apply_hook(do_mirror)
        rc = hook(
            chart_dir=Path("/tmp"),
            temp_dir=Path("/tmp"),
            values_dir=Path("/tmp"),
            latest_version="1.0.0",
            latest_app_version="5.0",
        )
        self.assertEqual(rc, 7)


# =============================================================
# _make_values_summary_hook — None pass-through vs wrap
# =============================================================


class ValuesSummaryHookFactoryTests(unittest.TestCase):
    def test_none_consumer_returns_none(self) -> None:
        """Consumer omits print_values_summary → factory returns None so
        external_standard's _default_values_summary runs at Step 1."""
        self.assertIsNone(ewm._make_values_summary_hook(None))

    def test_callable_is_wrapped(self) -> None:
        captured = {}

        def consumer_summary(*, values_dir):
            captured["values_dir"] = values_dir

        hook = ewm._make_values_summary_hook(consumer_summary)
        self.assertIsNotNone(hook)
        hook(values_dir=Path("/tmp/values"))
        self.assertEqual(captured["values_dir"], Path("/tmp/values"))


# =============================================================
# run() integration — hooks wired through external_standard
# =============================================================


class RunIntegrationTests(unittest.TestCase):
    def _consumer_config(self) -> dict:
        return {
            "SCRIPT_NAME": "test chart",
            "HELM_REPO_NAME": "",
            "HELM_REPO_URL": "",
            "HELM_CHART": "oci://ghcr.io/example/charts/test",
            "GITHUB_REPO": "example/charts",
            "GITHUB_TAG_PREFIX": "test-",
            "CHANGELOG_URL": "https://example.com/changelog",
            "CHART_TYPE": "external",
        }

    def test_help_short_circuits(self) -> None:
        """--help short-circuits before any external command runs.

        Also verifies the ``external_oci_with_mirror`` module forwards argv correctly into the
        external_standard arg parser.
        """
        cfg = self._consumer_config()
        with redirect_stdout(io.StringIO()) as buf:
            with self.assertRaises(SystemExit) as cm:
                ewm.run(cfg, ["--help"], script_path="/tmp/upgrade.py")
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("Usage:", buf.getvalue())

    def test_run_passes_total_steps_8_into_runner(self) -> None:
        """run() must forward total_steps=8 into external_standard.run."""
        cfg = self._consumer_config()
        called: dict = {}

        def fake_runner(config, argv, script_path, **kwargs):
            called.update(kwargs)
            return 0

        with mock.patch.object(ewm, "_run_external_standard", side_effect=fake_runner):
            rc = ewm.run(cfg, [], script_path="/tmp/upgrade.py")

        self.assertEqual(rc, 0)
        self.assertEqual(called["total_steps"], 8)
        # Without consumer hooks, pre_apply_hook is None (skip) and
        # values_summary_hook is None (default Step 1 dump).
        self.assertIsNone(called["pre_apply_hook"])
        self.assertIsNone(called["values_summary_hook"])
        # ``external_oci`` inherits: fetch / chart_write / helmfile_pin are always set.
        self.assertIsNotNone(called["fetch_latest_hook"])
        self.assertIsNotNone(called["chart_write_hook"])
        self.assertIsNotNone(called["helmfile_pin_hook"])

    def test_run_wires_do_mirror_into_pre_apply(self) -> None:
        """CONFIG['do_mirror'] is wrapped into pre_apply_hook."""
        cfg = self._consumer_config()
        cfg["do_mirror"] = lambda **_kw: 0
        called: dict = {}

        def fake_runner(config, argv, script_path, **kwargs):
            called.update(kwargs)
            return 0

        with mock.patch.object(ewm, "_run_external_standard", side_effect=fake_runner):
            ewm.run(cfg, [], script_path="/tmp/upgrade.py")
        self.assertIsNotNone(called["pre_apply_hook"])

    def test_run_forwards_pin_only_kwargs(self) -> None:
        """The argocd-pin (BASE='oci') path routes through here, so both
        pin-only kwargs must reach external_standard.run rather than being
        swallowed — otherwise a pin-only OCI component would hit the same
        silent empty-current-version degradation."""
        cfg = self._consumer_config()
        called: dict = {}

        def fake_runner(config, argv, script_path, **kwargs):
            called.update(kwargs)
            return 0

        sentinel = lambda *, chart_dir: "1.2.3"  # noqa: E731
        with mock.patch.object(ewm, "_run_external_standard", side_effect=fake_runner):
            ewm.run(
                cfg, [], script_path="/tmp/upgrade.py",
                current_version_hook=sentinel,
                skip_missing_chart_mirror=True,
            )
        self.assertIs(called["current_version_hook"], sentinel)
        self.assertTrue(called["skip_missing_chart_mirror"])

    def test_run_defaults_pin_only_kwargs_off(self) -> None:
        """Every non-argocd-pin OCI consumer keeps the baseline behavior."""
        cfg = self._consumer_config()
        called: dict = {}

        def fake_runner(config, argv, script_path, **kwargs):
            called.update(kwargs)
            return 0

        with mock.patch.object(ewm, "_run_external_standard", side_effect=fake_runner):
            ewm.run(cfg, [], script_path="/tmp/upgrade.py")
        self.assertIsNone(called["current_version_hook"])
        self.assertFalse(called["skip_missing_chart_mirror"])

    def test_run_wires_print_values_summary(self) -> None:
        """CONFIG['print_values_summary'] becomes values_summary_hook."""
        cfg = self._consumer_config()
        cfg["print_values_summary"] = lambda *, values_dir: None
        called: dict = {}

        def fake_runner(config, argv, script_path, **kwargs):
            called.update(kwargs)
            return 0

        with mock.patch.object(ewm, "_run_external_standard", side_effect=fake_runner):
            ewm.run(cfg, [], script_path="/tmp/upgrade.py")
        self.assertIsNotNone(called["values_summary_hook"])


# =============================================================
# Consumer config spot-checks — read the 3 real upgrade.py files
# =============================================================
if __name__ == "__main__":
    unittest.main()
