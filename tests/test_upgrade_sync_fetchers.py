"""Unit tests for scripts/python/upgrade_sync/fetchers.py.

Upstream version + container-image fetchers extracted from
``check-versions.py``. All network and subprocess interactions are
mocked so the suite never hits the network.

Stdlib unittest only.
"""

from __future__ import annotations

import email.message
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Make ``upgrade_sync`` importable when the test is launched directly.
_pkg_root = REPO_ROOT / "scripts" / "python"
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from upgrade_sync import fetchers  # noqa: E402


class TestSemverSortDesc(unittest.TestCase):

    def test_descending_triplet_sort(self) -> None:
        self.assertEqual(
            fetchers._semver_sort_desc(["1.0.0", "1.10.0", "1.2.0"]),
            ["1.10.0", "1.2.0", "1.0.0"],
        )

    def test_handles_empty_list(self) -> None:
        self.assertEqual(fetchers._semver_sort_desc([]), [])

    def test_invalid_versions_demoted(self) -> None:
        # A garbage entry sorts to the bottom (key = (-1,-1,-1)).
        self.assertEqual(
            fetchers._semver_sort_desc(["abc", "1.0.0", "0.5.0"])[-1], "abc"
        )


class TestFetchLatestHelmRepo(unittest.TestCase):

    def test_returns_first_version_from_json(self) -> None:
        fake_stdout = json.dumps([
            {"name": "elastic/eck-operator", "version": "3.1.2"},
            {"name": "elastic/eck-operator", "version": "3.1.1"},
        ])
        fake = mock.MagicMock(returncode=0, stdout=fake_stdout)
        with mock.patch.object(fetchers.subprocess, "run", return_value=fake):
            self.assertEqual(fetchers.fetch_latest_helm_repo("eck-operator"), "3.1.2")

    def test_returns_empty_when_empty_chart_arg(self) -> None:
        self.assertEqual(fetchers.fetch_latest_helm_repo(""), "")

    def test_returns_empty_when_helm_fails(self) -> None:
        fake = mock.MagicMock(returncode=1, stdout="")
        with mock.patch.object(fetchers.subprocess, "run", return_value=fake):
            self.assertEqual(fetchers.fetch_latest_helm_repo("foo"), "")

    def test_returns_empty_when_empty_array(self) -> None:
        fake = mock.MagicMock(returncode=0, stdout="[]")
        with mock.patch.object(fetchers.subprocess, "run", return_value=fake):
            self.assertEqual(fetchers.fetch_latest_helm_repo("foo"), "")


class TestFetchLatestGitTags(unittest.TestCase):

    def test_picks_first_semver_triplet(self) -> None:
        fake_stdout = (
            "abc123\trefs/tags/v2.0.0\n"
            "def456\trefs/tags/v1.10.5\n"
        )
        fake = mock.MagicMock(returncode=0, stdout=fake_stdout)
        with mock.patch.object(fetchers.subprocess, "run", return_value=fake):
            self.assertEqual(
                fetchers.fetch_latest_git_tags("https://example/foo.git"), "2.0.0"
            )

    def test_skips_non_semver_tags(self) -> None:
        # Non-semver lines must be ignored even if they sort earlier.
        fake_stdout = (
            "abc\trefs/tags/release-candidate\n"
            "def\trefs/tags/v1.0.0\n"
        )
        fake = mock.MagicMock(returncode=0, stdout=fake_stdout)
        with mock.patch.object(fetchers.subprocess, "run", return_value=fake):
            self.assertEqual(
                fetchers.fetch_latest_git_tags("https://example/foo.git"), "1.0.0"
            )


class TestFetchGaVersionsSource(unittest.TestCase):

    def test_elastic_artifacts_filters_ga_and_major(self) -> None:
        fake_body = {"versions": ["9.0.0", "9.0.1", "8.15.0", "9.1.0-rc1"]}
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_ga_versions_source("elastic-artifacts", "9")
        self.assertEqual(result, ["9.0.1", "9.0.0"])

    def test_github_releases_strips_v_prefix(self) -> None:
        fake_body = [
            {"tag_name": "v1.5.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.4.0", "prerelease": False, "draft": False},
        ]
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_ga_versions_source(
                "github-releases", "", "owner/repo", ""
            )
        self.assertEqual(result, ["1.5.0", "1.4.0"])

    def test_github_releases_filters_prerelease(self) -> None:
        fake_body = [
            {"tag_name": "v2.0.0-rc1", "prerelease": True, "draft": False},
            {"tag_name": "v1.0.0", "prerelease": False, "draft": False},
        ]
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_ga_versions_source(
                "github-releases", "", "owner/repo", ""
            )
        self.assertEqual(result, ["1.0.0"])

    def test_github_releases_honors_custom_prefix(self) -> None:
        fake_body = [
            {"tag_name": "chart-2.0.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.0.0", "prerelease": False, "draft": False},
        ]
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_ga_versions_source(
                "github-releases", "", "owner/repo", "chart-"
            )
        self.assertEqual(result, ["2.0.0"])

    def test_docker_hub_tags(self) -> None:
        fake_body = {
            "results": [
                {"name": "1.2.3"},
                {"name": "v1.2.4"},
                {"name": "latest"},
            ],
        }
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_ga_versions_source(
                "docker-hub-tags", "", "owner/repo"
            )
        self.assertEqual(result, ["1.2.4", "1.2.3"])

    def test_unknown_source_returns_empty(self) -> None:
        self.assertEqual(fetchers.fetch_ga_versions_source("nonexistent", ""), [])

    def test_empty_args_short_circuit(self) -> None:
        self.assertEqual(
            fetchers.fetch_ga_versions_source("github-releases", "", ""), []
        )


class TestFetchLatestChartVersionGh(unittest.TestCase):

    def test_picks_newest_matching_prefix(self) -> None:
        fake_body = [
            {"tag_name": "elasticsearch-eck-0.1.9", "prerelease": False, "draft": False},
            {"tag_name": "kibana-eck-0.1.5", "prerelease": False, "draft": False},
            {"tag_name": "elasticsearch-eck-0.1.8", "prerelease": False, "draft": False},
        ]
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_latest_chart_version_gh(
                "somaz94/helm-charts", "elasticsearch-eck"
            )
        self.assertEqual(result, "0.1.9")

    def test_returns_empty_when_no_matching_prefix(self) -> None:
        fake_body = [
            {"tag_name": "other-1.0.0", "prerelease": False, "draft": False},
        ]
        with mock.patch.object(fetchers, "_http_get_json", return_value=fake_body):
            result = fetchers.fetch_latest_chart_version_gh(
                "somaz94/helm-charts", "elasticsearch-eck"
            )
        self.assertEqual(result, "")

    def test_returns_empty_on_empty_args(self) -> None:
        self.assertEqual(fetchers.fetch_latest_chart_version_gh("", "foo"), "")
        self.assertEqual(fetchers.fetch_latest_chart_version_gh("foo", ""), "")


class TestVerifyImageExists(unittest.TestCase):

    def test_empty_args_short_circuit_to_true(self) -> None:
        self.assertTrue(fetchers.verify_image_exists("", "1.0.0"))
        self.assertTrue(fetchers.verify_image_exists("foo", ""))

    def test_returns_true_on_200(self) -> None:
        # The anonymous pass returns 200 — no bearer flow needed.
        fake_resp = mock.MagicMock(status=200)
        fake_resp.__enter__ = lambda s: fake_resp
        fake_resp.__exit__ = lambda *a: None
        with mock.patch.object(
            fetchers.urllib.request, "urlopen", return_value=fake_resp,
        ):
            self.assertTrue(
                fetchers.verify_image_exists("docker.elastic.co/foo", "1.0.0")
            )

    def test_returns_false_on_unauthenticated_403(self) -> None:
        err = fetchers.urllib.error.HTTPError(
            url="x", code=403, msg="forbidden", hdrs=None, fp=None,
        )
        with mock.patch.object(fetchers.urllib.request, "urlopen", side_effect=err):
            self.assertFalse(
                fetchers.verify_image_exists("docker.elastic.co/foo", "1.0.0")
            )

    def test_401_bearer_flow_success(self) -> None:
        """401 + WWW-Authenticate triggers token fetch → retry → 200."""
        ok_resp = mock.MagicMock(status=200)
        ok_resp.__enter__ = lambda s: ok_resp
        ok_resp.__exit__ = lambda *a: None

        headers = email.message.Message()
        headers["WWW-Authenticate"] = (
            'Bearer realm="https://auth.example/token",service="r",scope="repository:foo:pull"'
        )
        challenge = fetchers.urllib.error.HTTPError(
            url="x", code=401, msg="auth required", hdrs=headers, fp=None,
        )

        with mock.patch.object(
            fetchers.urllib.request, "urlopen", side_effect=[challenge, ok_resp],
        ), mock.patch.object(
            fetchers, "_http_get_json", return_value={"token": "abc123"},
        ):
            self.assertTrue(
                fetchers.verify_image_exists("auth.example/foo", "1.0.0")
            )


class TestFindLatestAvailableSource(unittest.TestCase):

    def test_caps_at_max_attempts(self) -> None:
        """A regression at upstream (all top-N images missing) must not
        hang — the probe stops at IMAGE_PROBE_MAX_ATTEMPTS.
        """
        fake_versions = [f"9.0.{n}" for n in range(20)]
        with mock.patch.object(
            fetchers, "fetch_ga_versions_source", return_value=fake_versions,
        ), mock.patch.object(fetchers, "verify_image_exists", return_value=False) as m_probe:
            result = fetchers.find_latest_available_source(
                "elastic-artifacts", "9", "docker.elastic.co/foo",
            )
        self.assertEqual(result, "")
        # IMAGE_PROBE_MAX_ATTEMPTS = 15 → probe called exactly 15 times.
        self.assertEqual(m_probe.call_count, fetchers.IMAGE_PROBE_MAX_ATTEMPTS)

    def test_returns_first_available(self) -> None:
        fake_versions = ["9.1.0", "9.0.5", "9.0.4"]
        with mock.patch.object(
            fetchers, "fetch_ga_versions_source", return_value=fake_versions,
        ), mock.patch.object(fetchers, "verify_image_exists", side_effect=[False, True]):
            result = fetchers.find_latest_available_source(
                "elastic-artifacts", "9", "docker.elastic.co/foo",
            )
        self.assertEqual(result, "9.0.5")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
