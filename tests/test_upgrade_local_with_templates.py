"""Unit tests for upgrade_core/local_with_templates.py.

``local_with_templates`` is an **independent** module — it does not extend
:mod:`external_standard` via hooks because the local chart flow (helm
pull --untar / git clone → templates/ replace + custom preserve +
_pod.tpl patch + extra dirs sync) is fundamentally different from the
external-chart 7-step body. Coverage focuses on the local-specific
helpers and the run() integration on the fluent-bit consumer shape.

Stdlib unittest only.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

lwt = load("upgrade_core.local_with_templates")


# =============================================================
# _fetch_latest_git — semver tag selection from git ls-remote
# =============================================================


class FetchLatestGitTests(unittest.TestCase):
    def _ls_remote_stdout(self, tags: list[str]) -> str:
        return "\n".join(f"abc123\trefs/tags/{t}" for t in tags) + "\n"

    def test_picks_first_semver_tag(self) -> None:
        # git ls-remote already returns sorted by -v:refname.
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=self._ls_remote_stdout(["v1.2.3", "v1.2.2", "v1.0.0"]),
            stderr="",
        )
        with mock.patch.object(lwt, "_run", return_value=fake):
            self.assertEqual(lwt._fetch_latest_git("git@x:foo.git"), "1.2.3")

    def test_skips_non_semver_tags(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=self._ls_remote_stdout(
                ["release-candidate", "nightly-20260101", "v0.1.0"]
            ),
            stderr="",
        )
        with mock.patch.object(lwt, "_run", return_value=fake):
            self.assertEqual(lwt._fetch_latest_git("git@x:foo.git"), "0.1.0")

    def test_bare_version_without_v_prefix(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=self._ls_remote_stdout(["2.5.1"]),
            stderr="",
        )
        with mock.patch.object(lwt, "_run", return_value=fake):
            self.assertEqual(lwt._fetch_latest_git("git@x:foo.git"), "2.5.1")

    def test_no_tags_returns_empty(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with mock.patch.object(lwt, "_run", return_value=fake):
            self.assertEqual(lwt._fetch_latest_git("git@x:foo.git"), "")

    def test_subprocess_failure_returns_empty(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="auth failed",
        )
        with mock.patch.object(lwt, "_run", return_value=fake):
            self.assertEqual(lwt._fetch_latest_git("git@x:foo.git"), "")


# =============================================================
# _diff_templates — modified / new / removed classification
# =============================================================


class DiffTemplatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.local = self.tmp / "local"
        self.upstream = self.tmp / "upstream"
        (self.local).mkdir()
        (self.upstream / "templates").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_modified_template_counted(self) -> None:
        self._write(self.local / "deployment.yaml", "OLD")
        self._write(self.upstream / "templates" / "deployment.yaml", "NEW")
        buf = io.StringIO()
        with redirect_stdout(buf):
            changed, added, removed = lwt._diff_templates(
                self.local, self.upstream, set(),
            )
        self.assertEqual((changed, added, removed), (1, 0, 0))
        self.assertIn("MODIFIED: templates/deployment.yaml", buf.getvalue())

    def test_removed_template_counted(self) -> None:
        self._write(self.local / "deprecated.yaml", "x")
        # upstream has no equivalent.
        buf = io.StringIO()
        with redirect_stdout(buf):
            changed, added, removed = lwt._diff_templates(
                self.local, self.upstream, set(),
            )
        self.assertEqual((changed, added, removed), (0, 0, 1))
        self.assertIn("REMOVED:  templates/deprecated.yaml", buf.getvalue())

    def test_new_template_counted(self) -> None:
        self._write(self.upstream / "templates" / "new-feature.yaml", "x")
        buf = io.StringIO()
        with redirect_stdout(buf):
            changed, added, removed = lwt._diff_templates(
                self.local, self.upstream, set(),
            )
        self.assertEqual((changed, added, removed), (0, 1, 0))
        self.assertIn("NEW:      templates/new-feature.yaml", buf.getvalue())

    def test_custom_template_skipped(self) -> None:
        self._write(self.local / "pv.yaml", "local-only")
        # No upstream equivalent — would normally be REMOVED, but the
        # custom set excludes it.
        buf = io.StringIO()
        with redirect_stdout(buf):
            changed, added, removed = lwt._diff_templates(
                self.local, self.upstream, {"pv.yaml"},
            )
        self.assertEqual((changed, added, removed), (0, 0, 0))
        self.assertNotIn("pv.yaml", buf.getvalue())

    def test_tests_subdir_handled(self) -> None:
        self._write(self.upstream / "templates" / "tests" / "smoke.yaml", "x")
        self._write(self.local / "tests" / "smoke.yaml", "y")
        # MODIFIED for the existing local test, NEW for an upstream-only one.
        self._write(self.upstream / "templates" / "tests" / "added.yaml", "z")
        buf = io.StringIO()
        with redirect_stdout(buf):
            changed, added, removed = lwt._diff_templates(
                self.local, self.upstream, set(),
            )
        out = buf.getvalue()
        self.assertIn("MODIFIED: templates/tests/smoke.yaml", out)
        self.assertIn("NEW:      templates/tests/added.yaml", out)
        self.assertEqual(changed, 1)
        self.assertEqual(added, 1)


# =============================================================
# _patch_pod_tpl — PVC patch injection
# =============================================================


class PatchPodTplTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.pod_tpl = self.tmp / "_pod.tpl"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_marker_missing_returns_one(self) -> None:
        self.pod_tpl.write_text("templates: {}\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lwt._patch_pod_tpl(self.pod_tpl, "patch-body")
        self.assertEqual(rc, 1)
        self.assertIn("Could not find extraVolumes marker", buf.getvalue())

    def test_already_patched_skips(self) -> None:
        self.pod_tpl.write_text(
            "templates:\n  - persistentVolumeClaims.enabled: true\n"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = lwt._patch_pod_tpl(self.pod_tpl, "patch-body")
        self.assertEqual(rc, 0)
        self.assertIn("already present", buf.getvalue())

    def test_insert_before_marker(self) -> None:
        self.pod_tpl.write_text(
            "spec:\n"
            "  containers: []\n"
            "{{- if .Values.extraVolumes }}\n"
            "  - extra-volume\n"
            "{{- end }}\n"
        )
        patch_body = "PVC_PATCH_LINE_1\nPVC_PATCH_LINE_2"
        with redirect_stdout(io.StringIO()):
            rc = lwt._patch_pod_tpl(self.pod_tpl, patch_body)
        self.assertEqual(rc, 0)
        text = self.pod_tpl.read_text()
        # The patch must appear *before* the marker line.
        patch_idx = text.index("PVC_PATCH_LINE_1")
        marker_idx = text.index("if .Values.extraVolumes")
        self.assertLess(patch_idx, marker_idx)
        self.assertIn("PVC_PATCH_LINE_2", text)


# =============================================================
# _download_chart_helm / _download_chart_git — subprocess mocking
# =============================================================


class DownloadChartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_helm_pull_returns_unpacked_dir(self) -> None:
        # Pre-create the dir _helm would have created.
        (self.tmp / "fluent-bit").mkdir()
        (self.tmp / "fluent-bit" / "templates").mkdir()
        with mock.patch.object(lwt, "_helm") as helm_mock:
            result = lwt._download_chart_helm(
                "fluent/fluent-bit", "0.49.0", self.tmp
            )
        helm_mock.assert_called_once()
        self.assertEqual(result, self.tmp / "fluent-bit")

    def test_helm_pull_no_dir_returns_none(self) -> None:
        with mock.patch.object(lwt, "_helm"):
            result = lwt._download_chart_helm(
                "fluent/fluent-bit", "0.49.0", self.tmp
            )
        self.assertIsNone(result)

    def test_git_clone_with_v_prefix_tag(self) -> None:
        # First _run = git ls-remote with v0.1.0 → success.
        # Second _run = git clone → success.
        clone_target = self.tmp / "git-src"
        clone_target.mkdir()

        def fake_run(cmd, **_kw):
            if cmd[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="abc\trefs/tags/v0.1.0\n", stderr="",
                )
            if cmd[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr=""
            )

        with mock.patch.object(lwt, "_run", side_effect=fake_run):
            result = lwt._download_chart_git(
                "git@x:foo.git", "", "0.1.0", self.tmp
            )
        self.assertEqual(result, clone_target)

    def test_git_clone_fallback_to_bare_version_tag(self) -> None:
        # First ls-remote: v0.1.0 not found → empty stdout.
        # Clone with bare "0.1.0" succeeds.
        clone_target = self.tmp / "git-src"
        clone_target.mkdir()

        def fake_run(cmd, **_kw):
            if cmd[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )
            if cmd[:2] == ["git", "clone"]:
                self.assertIn("0.1.0", cmd)
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr=""
            )

        with mock.patch.object(lwt, "_run", side_effect=fake_run):
            result = lwt._download_chart_git(
                "git@x:foo.git", "", "0.1.0", self.tmp
            )
        self.assertEqual(result, clone_target)

    def test_git_clone_with_path_returns_subdir(self) -> None:
        clone_target = self.tmp / "git-src"
        (clone_target / "charts" / "foo").mkdir(parents=True)

        def fake_run(cmd, **_kw):
            if cmd[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="abc\trefs/tags/v0.1.0\n", stderr="",
                )
            if cmd[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="",
            )

        with mock.patch.object(lwt, "_run", side_effect=fake_run):
            result = lwt._download_chart_git(
                "git@x:foo.git", "charts/foo", "0.1.0", self.tmp,
            )
        self.assertEqual(result, clone_target / "charts" / "foo")

    def test_git_clone_failure_returns_none(self) -> None:
        def fake_run(cmd, **_kw):
            if cmd[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="abc\trefs/tags/v0.1.0\n", stderr=""
                )
            if cmd[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=128,
                    stdout="", stderr="auth failed",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="",
            )

        with mock.patch.object(lwt, "_run", side_effect=fake_run):
            result = lwt._download_chart_git(
                "git@x:foo.git", "", "0.1.0", self.tmp
            )
        self.assertIsNone(result)


# =============================================================
# run() integration — exercises the full flow with subprocess mocks
# =============================================================


def _fake_subprocess(handler):
    """Returns a mock that routes subprocess.run(...) through handler(cmd)."""
    def _run(cmd, **kwargs):
        result = handler(list(cmd))
        if isinstance(result, subprocess.CompletedProcess):
            return result
        text = result if isinstance(result, str) else ""
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=text, stderr="",
        )
    return _run


class RunIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "chart"
        self.chart_dir.mkdir()
        (self.chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: fluent-bit\nversion: 0.49.0\n"
            "appVersion: 4.0.6\n"
        )
        (self.chart_dir / "values.yaml").write_text("global:\n  foo: 1\n")
        (self.chart_dir / "templates").mkdir()
        (self.chart_dir / "templates" / "deployment.yaml").write_text("d:1")
        (self.chart_dir / "templates" / "_pod.tpl").write_text(
            "spec:\n{{- if .Values.extraVolumes }}\n{{- end }}\n"
        )
        (self.chart_dir / "templates" / "pv.yaml").write_text("local-only")
        (self.chart_dir / "templates" / "pvc.yaml").write_text("local-only")
        (self.chart_dir / "values").mkdir()
        (self.chart_dir / "values" / "dev.yaml").write_text("global:\n  foo: 2\n")
        self.script = self.chart_dir / "upgrade.py"
        self.script.write_text("# stub\n")
        self.config = {
            "SCRIPT_NAME": "Test Local Chart",
            "HELM_REPO_NAME": "fluent",
            "HELM_REPO_URL": "https://fluent.github.io/helm-charts",
            "HELM_CHART": "fluent/fluent-bit",
            "CHANGELOG_URL": "https://example/CHANGELOG.md",
            "CHART_GIT_REPO": "",
            "CHART_GIT_PATH": "",
            "CUSTOM_TEMPLATES": ["pv.yaml", "pvc.yaml"],
            "CUSTOM_POD_PATCH": "PVC_PATCH_LINE",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_help_short_circuits(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                lwt.run(self.config, ["--help"], self.script)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("Usage:", buf.getvalue())

    def test_already_up_to_date_returns_zero(self) -> None:
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"0.49.0","app_version":"4.0.6"}]'
            return ""
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = lwt.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date! Nothing to do.", buf.getvalue())

    def test_step_headers_use_eight(self) -> None:
        """All 8 step headers render as ``[Step N/8]``."""
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"0.50.0","app_version":"4.0.7"}]'
            if cmd[:2] == ["helm", "pull"]:
                # Pre-create the unpacked dir.
                dest = Path(cmd[-1])
                pulled = dest / "fluent-bit"
                pulled.mkdir(parents=True, exist_ok=True)
                (pulled / "Chart.yaml").write_text(
                    "apiVersion: v2\nname: fluent-bit\nversion: 0.50.0\n"
                    "appVersion: 4.0.7\n"
                )
                (pulled / "values.yaml").write_text("global:\n  foo: 1\n")
                (pulled / "templates").mkdir()
                (pulled / "templates" / "deployment.yaml").write_text("d:2")
                (pulled / "templates" / "_pod.tpl").write_text(
                    "spec:\n{{- if .Values.extraVolumes }}\n{{- end }}\n"
                )
                return ""
            if cmd and cmd[0] == "diff":
                return ""
            return ""

        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = lwt.run(self.config, ["--dry-run"], self.script)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        for expected in (
            "[Step 1/8] Checking current version...",
            "[Step 2/8] Checking latest version...",
            "[Step 3/8] Downloading upstream chart v0.50.0...",
            "[Step 4/8] Chart.yaml diff",
            "[Step 5/8] values.yaml diff",
            "[Step 6/8] Custom _pod.tpl patch check",
            "[Step 7/8] Checking custom values for breaking changes",
            "[Step 8/8] DRY-RUN complete. No files were changed.",
        ):
            self.assertIn(expected, out, f"missing header: {expected!r}")

    def test_git_mode_uses_git_ls_remote(self) -> None:
        """CHART_GIT_REPO set → Step 2 uses git ls-remote, not helm search."""
        cfg = dict(self.config, CHART_GIT_REPO="git@example.com:foo.git")
        seen: list[list[str]] = []

        def handler(cmd):
            seen.append(list(cmd))
            if cmd[:2] == ["git", "ls-remote"]:
                # Tag that matches Chart.yaml so "already up to date" → 0.
                return "abc\trefs/tags/v0.49.0\n"
            return ""

        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(io.StringIO()):
                rc = lwt.run(cfg, [], self.script)
        self.assertEqual(rc, 0)
        self.assertTrue(
            any(c[:2] == ["git", "ls-remote"] for c in seen),
            "git ls-remote should have been invoked in git mode",
        )
        self.assertFalse(
            any(c[:2] == ["helm", "search"] for c in seen),
            "helm search should NOT run when CHART_GIT_REPO is set",
        )


# =============================================================
# --rollback never writes a helmfile back into an ArgoCD-delivered component
# =============================================================


class RollbackHelmfileTests(unittest.TestCase):
    """A rollback never writes a helmfile back into a component ArgoCD delivers."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.chart_dir = self.tmp / "fluent-bit"
        (self.chart_dir / "values").mkdir(parents=True)
        (self.chart_dir / "templates").mkdir()
        (self.chart_dir / "Chart.yaml").write_text("apiVersion: v2\nname: fluent-bit\nversion: 0.58.2\n")
        backup = self.chart_dir / "backup" / "20260101_000000"
        (backup / "templates").mkdir(parents=True)
        (backup / "Chart.yaml").write_text("apiVersion: v2\nname: fluent-bit\nversion: 0.57.9\n")
        (backup / "helmfile.yaml").write_text("hooks: [old]\n")
        (backup / "dev.yaml").write_text("old: true\n")

    def _rollback(self) -> str:
        out = io.StringIO()
        with mock.patch("builtins.input", return_value=""), redirect_stdout(out):
            lwt._do_rollback(
                self.chart_dir / "backup", self.chart_dir,
                self.chart_dir / "values", self.chart_dir / "templates",
            )
        return out.getvalue()

    def test_argocd_delivered_component_gets_no_helmfile(self) -> None:
        (self.chart_dir / "argocd-local").mkdir()
        out = self._rollback()
        self.assertFalse((self.chart_dir / "helmfile.yaml").exists())
        self.assertIn("Skipped helmfile.yaml", out)
        self.assertIn("version: 0.57.9", (self.chart_dir / "Chart.yaml").read_text())
        self.assertIn("ArgoCD renders this vendored chart from git", out)

    def test_a_retired_helmfile_on_disk_is_left_alone(self) -> None:
        (self.chart_dir / "argocd-local-aws").mkdir()
        (self.chart_dir / "helmfile.yaml").write_text("# RETIRED\nhooks: []\n")
        self._rollback()
        self.assertEqual((self.chart_dir / "helmfile.yaml").read_text(), "# RETIRED\nhooks: []\n")

    def test_a_retired_helmfile_pin_moves_back_with_the_chart(self) -> None:
        # The upgrade keeps bumping a retired helmfile's pin.
        (self.chart_dir / "argocd-local-aws").mkdir()
        (self.chart_dir / "helmfile.yaml").write_text("# RETIRED\nreleases:\n  - version: 0.58.2\n")
        out = self._rollback()
        self.assertEqual(
            (self.chart_dir / "helmfile.yaml").read_text(), "# RETIRED\nreleases:\n  - version: 0.57.9\n"
        )
        self.assertIn("Updated helmfile.yaml chart pin 0.58.2 -> 0.57.9", out)

    def test_a_gotmpl_helmfile_wins_over_a_plain_one(self) -> None:
        backup = self.chart_dir / "backup" / "20260101_000000"
        (backup / "helmfile.yaml.gotmpl").write_text("gotmpl: true\n")
        self._rollback()
        self.assertEqual((self.chart_dir / "helmfile.yaml.gotmpl").read_text(), "gotmpl: true\n")
        self.assertFalse((self.chart_dir / "helmfile.yaml").exists())

    def test_a_values_file_the_component_keeps_is_restored(self) -> None:
        (self.chart_dir / "values" / "dev.yaml").write_text("new: true\n")
        out = self._rollback()
        self.assertEqual((self.chart_dir / "values" / "dev.yaml").read_text(), "old: true\n")
        self.assertNotIn("Skipped values/dev.yaml", out)

    def test_a_values_file_the_component_dropped_is_not_revived(self) -> None:
        (self.chart_dir / "argocd-local").mkdir()
        out = self._rollback()
        self.assertFalse((self.chart_dir / "values" / "dev.yaml").exists())
        self.assertIn("Skipped values/dev.yaml (no longer in values/", out)

    def test_a_helmfile_rollback_restores_the_whole_snapshot(self) -> None:
        self._rollback()
        self.assertEqual((self.chart_dir / "values" / "dev.yaml").read_text(), "old: true\n")

    def test_the_schema_mirror_follows_chart_yaml(self) -> None:
        backup = self.chart_dir / "backup" / "20260101_000000"
        (backup / "values.schema.json").write_text('{"old": true}')
        (self.chart_dir / "values.schema.json").write_text('{"new": true}')
        self._rollback()
        self.assertEqual((self.chart_dir / "values.schema.json").read_text(), '{"old": true}')

    def test_a_component_without_a_schema_gets_none(self) -> None:
        (self.chart_dir / "backup" / "20260101_000000" / "values.schema.json").write_text("{}")
        self._rollback()
        self.assertFalse((self.chart_dir / "values.schema.json").exists())

    def test_next_steps_follow_the_delivery_path(self) -> None:
        self.assertIn("   3. Run: helmfile apply", lwt._next_steps(self.chart_dir))
        (self.chart_dir / "argocd-local").mkdir()
        steps = lwt._next_steps(self.chart_dir)
        self.assertTrue(any("commit and push" in s for s in steps))
        self.assertFalse(any("helmfile" in s for s in steps))

    def test_a_helmfile_delivered_component_still_gets_it_back(self) -> None:
        out = self._rollback()
        self.assertEqual((self.chart_dir / "helmfile.yaml").read_text(), "hooks: [old]\n")
        self.assertIn("Run 'helmfile diff' to verify.", out)


if __name__ == "__main__":
    unittest.main()
