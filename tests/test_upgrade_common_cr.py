"""Unit tests for upgrade_core/_common_cr.py.

Shared CR-version helpers used by both ``local_cr_version`` and
``external_oci_cr_version``. Coverage focuses on:
  - 3-backend version fetching (elastic-artifacts / github-releases /
    docker-hub-tags)
  - YAML read/write quote-style preservation
  - kubectl mocks (cluster health / dependency CR / live CR version)
  - Bearer-token image verification flow
  - Helmfile namespace + release metadata extraction
  - Semver tuple compare

Stdlib unittest only.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ccr = load("upgrade_core._common_cr")


# =============================================================
# semver_compare
# =============================================================


class SemverCompareTests(unittest.TestCase):
    def test_less(self) -> None:
        self.assertEqual(ccr.semver_compare("1.0.0", "1.0.1"), -1)

    def test_equal(self) -> None:
        self.assertEqual(ccr.semver_compare("9.2.3", "9.2.3"), 0)

    def test_greater(self) -> None:
        self.assertEqual(ccr.semver_compare("9.3.0", "9.2.99"), 1)

    def test_minor_rank(self) -> None:
        self.assertEqual(ccr.semver_compare("9.0.10", "9.0.9"), 1)


# =============================================================
# YAML read / update
# =============================================================


class YamlValueTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
        tmp.write_text(content)
        return tmp

    def test_read_bare(self) -> None:
        p = self._write("version: 9.2.3\nname: foo\n")
        self.assertEqual(ccr.read_yaml_value(p, "version"), "9.2.3")

    def test_read_double_quoted(self) -> None:
        p = self._write('version: "9.2.3"\n')
        self.assertEqual(ccr.read_yaml_value(p, "version"), "9.2.3")

    def test_read_single_quoted(self) -> None:
        p = self._write("version: '9.2.3'\n")
        self.assertEqual(ccr.read_yaml_value(p, "version"), "9.2.3")

    def test_read_with_inline_comment(self) -> None:
        p = self._write("version: 9.2.3   # comment\n")
        self.assertEqual(ccr.read_yaml_value(p, "version"), "9.2.3")

    def test_read_missing(self) -> None:
        p = self._write("other: yes\n")
        self.assertEqual(ccr.read_yaml_value(p, "version"), "")

    def test_update_double_quoted_preserves(self) -> None:
        p = self._write('version: "9.2.3"\nname: foo\n')
        ccr.update_yaml_value(p, "version", "9.3.0")
        self.assertEqual(p.read_text(), 'version: "9.3.0"\nname: foo\n')

    def test_update_single_quoted_preserves(self) -> None:
        p = self._write("version: '9.2.3'\nname: foo\n")
        ccr.update_yaml_value(p, "version", "9.3.0")
        self.assertEqual(p.read_text(), "version: '9.3.0'\nname: foo\n")

    def test_update_bare_preserves_bare(self) -> None:
        p = self._write("version: 9.2.3\nname: foo\n")
        ccr.update_yaml_value(p, "version", "9.3.0")
        self.assertEqual(p.read_text(), "version: 9.3.0\nname: foo\n")

    def test_update_only_first_match(self) -> None:
        p = self._write("version: 9.2.3\nversion: 8.0.0\n")
        ccr.update_yaml_value(p, "version", "9.9.9")
        self.assertEqual(p.read_text(), "version: 9.9.9\nversion: 8.0.0\n")


# =============================================================
# fetch_ga_versions — 3 backends
# =============================================================


class FetchGaVersionsTests(unittest.TestCase):
    def _patch_http(self, body: bytes):
        return mock.patch.object(ccr, "http_get", return_value=body)

    def test_elastic_artifacts_filters_ga_and_sorts(self) -> None:
        payload = json.dumps(
            {"versions": ["9.0.0", "9.1.2", "9.0.5-beta1", "8.99.0", "not-a-version"]}
        ).encode()
        with self._patch_http(payload):
            got = ccr.fetch_ga_versions("elastic-artifacts", "", "9")
        self.assertEqual(got, ["9.1.2", "9.0.0"])

    def test_elastic_artifacts_no_major_pin(self) -> None:
        payload = json.dumps({"versions": ["9.0.0", "8.0.0", "8.10.5"]}).encode()
        with self._patch_http(payload):
            got = ccr.fetch_ga_versions("elastic-artifacts", "", "")
        self.assertEqual(got, ["9.0.0", "8.10.5", "8.0.0"])

    def test_github_releases_strips_v_prefix_and_drops_draft(self) -> None:
        payload = json.dumps(
            [
                {"tag_name": "v1.2.3", "prerelease": False, "draft": False},
                {"tag_name": "1.2.2", "prerelease": False, "draft": False},
                {"tag_name": "v1.2.4", "prerelease": True, "draft": False},
                {"tag_name": "v1.2.5", "prerelease": False, "draft": True},
            ]
        ).encode()
        with self._patch_http(payload):
            got = ccr.fetch_ga_versions("github-releases", "owner/repo", "")
        self.assertEqual(got, ["1.2.3", "1.2.2"])

    def test_github_releases_empty_source_arg(self) -> None:
        with self._patch_http(b'[{"tag_name": "v1.0.0"}]'):
            got = ccr.fetch_ga_versions("github-releases", "", "")
        self.assertEqual(got, [])

    def test_docker_hub_tags(self) -> None:
        payload = json.dumps(
            {
                "results": [
                    {"name": "8.0.0"},
                    {"name": "v9.1.0"},
                    {"name": "8.10.5"},
                    {"name": "9.0.0-rc1"},
                    {"name": "latest"},
                ]
            }
        ).encode()
        with self._patch_http(payload):
            got = ccr.fetch_ga_versions("docker-hub-tags", "ns/repo", "")
        self.assertEqual(got, ["9.1.0", "8.10.5", "8.0.0"])

    def test_unknown_source(self) -> None:
        got = ccr.fetch_ga_versions("nonexistent", "", "")
        self.assertEqual(got, [])

    def test_empty_response_returns_empty(self) -> None:
        with self._patch_http(b""):
            got = ccr.fetch_ga_versions("elastic-artifacts", "", "")
        self.assertEqual(got, [])

    def test_invalid_json_returns_empty(self) -> None:
        with self._patch_http(b"not-json"):
            got = ccr.fetch_ga_versions("elastic-artifacts", "", "")
        self.assertEqual(got, [])


class FetchLatestVersionTests(unittest.TestCase):
    def test_returns_first(self) -> None:
        with mock.patch.object(
            ccr, "fetch_ga_versions", return_value=["3.0.0", "2.0.0", "1.0.0"]
        ):
            self.assertEqual(
                ccr.fetch_latest_version("github-releases", "x/y", ""), "3.0.0"
            )

    def test_empty(self) -> None:
        with mock.patch.object(ccr, "fetch_ga_versions", return_value=[]):
            self.assertEqual(
                ccr.fetch_latest_version("github-releases", "x/y", ""), ""
            )


class FindLatestAvailableVersionTests(unittest.TestCase):
    def test_first_with_image(self) -> None:
        with mock.patch.object(ccr, "fetch_ga_versions", return_value=["9.2.0", "9.1.5"]):
            with mock.patch.object(
                ccr,
                "verify_image_exists",
                side_effect=lambda image, tag: tag == "9.1.5",
            ):
                with redirect_stdout(io.StringIO()):
                    got = ccr.find_latest_available_version(
                        "elastic-artifacts", "", "9", "image/x"
                    )
        self.assertEqual(got, "9.1.5")

    def test_none_available(self) -> None:
        with mock.patch.object(ccr, "fetch_ga_versions", return_value=["9.2.0"]):
            with mock.patch.object(ccr, "verify_image_exists", return_value=False):
                with redirect_stdout(io.StringIO()):
                    got = ccr.find_latest_available_version(
                        "elastic-artifacts", "", "9", "image/x"
                    )
        self.assertEqual(got, "")


# =============================================================
# verify_image_exists
# =============================================================


class VerifyImageExistsTests(unittest.TestCase):
    def test_empty_image_or_tag_returns_true(self) -> None:
        self.assertTrue(ccr.verify_image_exists("", "9.0.0"))
        self.assertTrue(ccr.verify_image_exists("foo/bar", ""))

    def test_invalid_image_format_returns_false(self) -> None:
        self.assertFalse(ccr.verify_image_exists("foobar", "9.0.0"))

    def test_simple_200(self) -> None:
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = mock.MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = mock.MagicMock(return_value=False)
        with mock.patch.object(
            ccr.urllib.request, "urlopen", return_value=fake_resp
        ):
            self.assertTrue(
                ccr.verify_image_exists(
                    "docker.elastic.co/elasticsearch/elasticsearch", "9.0.0"
                )
            )

    def test_404_returns_false(self) -> None:
        err = ccr.urllib.error.HTTPError(
            url="x", code=404, msg="not found", hdrs={}, fp=None
        )
        with mock.patch.object(ccr.urllib.request, "urlopen", side_effect=err):
            self.assertFalse(ccr.verify_image_exists("registry/repo", "9.0.0"))

    def test_bearer_token_flow(self) -> None:
        ok_resp = mock.MagicMock()
        ok_resp.status = 200
        ok_resp.__enter__ = mock.MagicMock(return_value=ok_resp)
        ok_resp.__exit__ = mock.MagicMock(return_value=False)

        err = ccr.urllib.error.HTTPError(
            url="x",
            code=401,
            msg="unauth",
            hdrs={"WWW-Authenticate": 'Bearer realm="https://auth/token",service="svc",scope="repository:repo:pull"'},
            fp=None,
        )
        with mock.patch.object(
            ccr.urllib.request,
            "urlopen",
            side_effect=[err, ok_resp],
        ):
            with mock.patch.object(
                ccr,
                "http_get",
                return_value=json.dumps({"token": "xyz"}).encode(),
            ):
                self.assertTrue(
                    ccr.verify_image_exists("registry/repo", "9.0.0")
                )


# =============================================================
# check_dependency_version (kubectl mock)
# =============================================================


class CheckDependencyVersionTests(unittest.TestCase):
    def test_empty_dep_returns_true(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertTrue(
                ccr.check_dependency_version(
                    target="9.0.0",
                    dep_kind="",
                    dep_name="",
                    helmfile_path=None,
                    component_label="x",
                )
            )

    def test_target_le_dep_passes(self) -> None:
        with mock.patch.object(ccr, "kubectl_available", return_value=True):
            with mock.patch.object(
                ccr, "read_helmfile_namespace", return_value="logging"
            ):
                rc = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="9.5.0", stderr=""
                )
                with mock.patch.object(ccr.subprocess, "run", return_value=rc):
                    with redirect_stdout(io.StringIO()):
                        self.assertTrue(
                            ccr.check_dependency_version(
                                target="9.4.0",
                                dep_kind="elasticsearch",
                                dep_name="elasticsearch",
                                helmfile_path=None,
                                component_label="kibana",
                            )
                        )

    def test_target_gt_dep_fails(self) -> None:
        with mock.patch.object(ccr, "kubectl_available", return_value=True):
            with mock.patch.object(
                ccr, "read_helmfile_namespace", return_value="logging"
            ):
                rc = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="9.5.0", stderr=""
                )
                with mock.patch.object(ccr.subprocess, "run", return_value=rc):
                    with redirect_stdout(io.StringIO()):
                        self.assertFalse(
                            ccr.check_dependency_version(
                                target="9.6.0",
                                dep_kind="elasticsearch",
                                dep_name="elasticsearch",
                                helmfile_path=None,
                                component_label="kibana",
                            )
                        )


# =============================================================
# kubectl_jsonpath (N3)
# =============================================================


class KubectlJsonpathTests(unittest.TestCase):
    def test_returns_stdout_stripped(self) -> None:
        rc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Ready\n", stderr=""
        )
        with mock.patch.object(ccr.subprocess, "run", return_value=rc) as m_run:
            got = ccr.kubectl_jsonpath("logging", "elasticsearch", "elasticsearch", ".status.phase")
        self.assertEqual(got, "Ready")
        # Verify the jsonpath argv was constructed correctly.
        called_args = m_run.call_args[0][0]
        self.assertIn("-o", called_args)
        idx = called_args.index("-o")
        self.assertEqual(called_args[idx + 1], "jsonpath={.status.phase}")


# =============================================================
# Helmfile helpers
# =============================================================


class ReadHelmfileNamespaceTests(unittest.TestCase):
    def test_extracts_first_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "helmfile.yaml"
            f.write_text(
                "releases:\n"
                "  - name: elasticsearch\n"
                "    namespace: logging\n"
                "    chart: oci://x/y\n"
            )
            self.assertEqual(ccr.read_helmfile_namespace(f), "logging")

    def test_missing_file(self) -> None:
        self.assertEqual(ccr.read_helmfile_namespace(None), "")

    def test_quoted_namespace(self) -> None:
        # In helmfile YAML, `namespace:` is a field of the release dict —
        # indented under `- name:`, no `-` of its own.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "helmfile.yaml"
            f.write_text("releases:\n  - name: foo\n    namespace: \"obs\"\n")
            self.assertEqual(ccr.read_helmfile_namespace(f), "obs")


class ReadHelmfileReleaseMetadataTests(unittest.TestCase):
    def test_extracts_first_name_and_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "helmfile.yaml"
            f.write_text(
                "releases:\n"
                "  - name: elasticsearch\n"
                "    namespace: logging\n"
                "    chart: oci://x/y\n"
                "    version: 0.1.0\n"
            )
            release, ns = ccr.read_helmfile_release_metadata(f)
            self.assertEqual(release, "elasticsearch")
            self.assertEqual(ns, "logging")

    def test_missing_file(self) -> None:
        self.assertEqual(ccr.read_helmfile_release_metadata(None), ("", ""))

    def test_skips_templated_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "helmfile.yaml.gotmpl"
            f.write_text(
                "releases:\n"
                "  - name: {{ .Values.release }}\n"
                "  - name: actual\n"
                "    namespace: ns\n"
            )
            release, ns = ccr.read_helmfile_release_metadata(f)
            self.assertEqual(release, "actual")
            self.assertEqual(ns, "ns")


# =============================================================
# Helm release JSON parsing
# =============================================================


class ReadHelmReleaseStatusTests(unittest.TestCase):
    def test_returns_status_from_json(self) -> None:
        rc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"info": {"status": "deployed"}}),
            stderr="",
        )
        with mock.patch.object(ccr.subprocess, "run", return_value=rc):
            self.assertEqual(ccr.read_helm_release_status("rel", "ns"), "deployed")

    def test_empty_stdout_returns_empty(self) -> None:
        rc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(ccr.subprocess, "run", return_value=rc):
            self.assertEqual(ccr.read_helm_release_status("rel", "ns"), "")

    def test_invalid_json_returns_empty(self) -> None:
        rc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr=""
        )
        with mock.patch.object(ccr.subprocess, "run", return_value=rc):
            self.assertEqual(ccr.read_helm_release_status("rel", "ns"), "")


class ReadLastGoodRevisionTests(unittest.TestCase):
    def test_picks_newest_deployed_or_superseded(self) -> None:
        history = [
            {"revision": 1, "status": "superseded"},
            {"revision": 2, "status": "deployed"},
            {"revision": 3, "status": "failed"},
        ]
        rc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(history), stderr=""
        )
        with mock.patch.object(ccr.subprocess, "run", return_value=rc):
            self.assertEqual(ccr.read_last_good_revision("rel", "ns"), "2")

    def test_no_good_returns_empty(self) -> None:
        history = [{"revision": 1, "status": "failed"}]
        rc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(history), stderr=""
        )
        with mock.patch.object(ccr.subprocess, "run", return_value=rc):
            self.assertEqual(ccr.read_last_good_revision("rel", "ns"), "")


# =============================================================
# handle_downgrade_rollback — manual 7-step instructions output
# =============================================================


class HandleDowngradeRollbackTests(unittest.TestCase):
    """Byte-parity guard for the manual instructions block previously
    inlined in both CR templates ``_do_rollback``. Operator/webhook config is
    intentionally left empty so the auto-webhook branch is skipped and
    we exercise the 7-step manual path."""

    def _config(self) -> dict:
        return {
            "COMPONENT_LABEL": "elasticsearch",
            # Auto-webhook config left empty so input() prompt is skipped.
        }

    def _run(self, *, operator_chart_label: str | None = None) -> str:
        cfg = self._config()
        buf = io.StringIO()
        kwargs = {}
        if operator_chart_label is not None:
            kwargs["operator_chart_label"] = operator_chart_label
        with redirect_stdout(buf):
            ccr.handle_downgrade_rollback(
                cfg, Path("/tmp"), None, "8.0.0", "7.17.0", **kwargs,
            )
        return buf.getvalue()

    def test_default_label_is_operator_dir(self) -> None:
        out = self._run()
        self.assertIn("WARNING: This is a version downgrade (8.0.0 -> 7.17.0).", out)
        self.assertIn("Operator admission webhooks typically block CR version downgrades.", out)
        self.assertIn("To apply this rollback manually:", out)
        self.assertIn("5. Recreate webhook: cd <operator-dir> && helmfile sync", out)
        # Step 7 uses COMPONENT_LABEL in both the resource and the jsonpath wait.
        self.assertIn(
            "7. Wait for CR: kubectl -n <ns> wait elasticsearch/elasticsearch",
            out,
        )
        self.assertIn("--for=jsonpath='{.status.phase}'=Ready --timeout=300s", out)

    def test_eck_operator_dir_label(self) -> None:
        out = self._run(operator_chart_label="eck-operator-dir")
        self.assertIn("5. Recreate webhook: cd <eck-operator-dir> && helmfile sync", out)
        # Other steps unchanged.
        self.assertIn("4. helmfile apply", out)

    def test_step_numbering_and_order(self) -> None:
        out = self._run()
        # All 7 step labels present, in order.
        positions = [out.index(f"    {n}.") for n in range(1, 8)]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
