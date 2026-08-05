"""Unit tests for scripts/python/upgrade_core/_common_argocd.py.

Covers the ArgoCD-metadata version helpers backing the ``argocd-pin``
template: the nested ``chart.version`` reader, the quote-preserving
in-place updater, and the multi-file fan-out.

Stdlib unittest only.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ca = load("upgrade_core._common_argocd")


_GHOST_META = """\
# Per-release ArgoCD metadata for ghost.
component: ghost
releaseName: ghost
namespace: blog
syncWave: "5"
chart:
  repoURL: ghcr.io/somaz94/charts   # registered OCI repo
  name: ghost
  version: "0.1.6"
valueFile: tools/ghost/values/dev.yaml
createNamespace: true
autoSync: true
"""


def _write(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp())
    f = tmp / "release.yaml"
    f.write_text(body)
    return f


# =============================================================
# read_argocd_chart_version
# =============================================================


class ReadArgocdChartVersionTests(unittest.TestCase):
    def test_reads_quoted_nested_version(self) -> None:
        f = _write(_GHOST_META)
        self.assertEqual(ca.read_argocd_chart_version(f), "0.1.6")

    def test_reads_bare_version(self) -> None:
        f = _write("chart:\n  name: x\n  version: 1.2.3\n")
        self.assertEqual(ca.read_argocd_chart_version(f), "1.2.3")

    def test_reads_single_quoted_version(self) -> None:
        f = _write("chart:\n  name: x\n  version: '9.9.9'\n")
        self.assertEqual(ca.read_argocd_chart_version(f), "9.9.9")

    def test_strips_inline_comment(self) -> None:
        f = _write('chart:\n  version: "3.0.0"   # pinned\n')
        self.assertEqual(ca.read_argocd_chart_version(f), "3.0.0")

    def test_ignores_top_level_version_outside_chart_block(self) -> None:
        # A column-0 `version:` (e.g. some other doc) must not be picked.
        f = _write("version: 99.0.0\nchart:\n  version: 1.0.0\n")
        self.assertEqual(ca.read_argocd_chart_version(f), "1.0.0")

    def test_block_closes_on_next_top_level_key(self) -> None:
        # `version:` appearing AFTER the chart block (indented under another
        # key) must not be matched once the chart block has closed.
        body = (
            "chart:\n"
            "  name: x\n"
            "other:\n"
            "  version: 5.5.5\n"
        )
        f = _write(body)
        self.assertEqual(ca.read_argocd_chart_version(f), "")

    def test_missing_key_returns_empty(self) -> None:
        f = _write("chart:\n  name: x\n  repoURL: y\n")
        self.assertEqual(ca.read_argocd_chart_version(f), "")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(
            ca.read_argocd_chart_version(Path("/nonexistent/release.yaml")), ""
        )


# =============================================================
# read_argocd_chart_url / read_argocd_release_name (Phase D)
# =============================================================


class ReadArgocdChartUrlTests(unittest.TestCase):
    def test_builds_oci_url_from_repo_and_name(self) -> None:
        f = _write(_GHOST_META)
        # repoURL has no scheme -> oci:// is prepended, name appended.
        self.assertEqual(
            ca.read_argocd_chart_url(f), "oci://ghcr.io/somaz94/charts/ghost"
        )

    def test_scheme_in_repo_url_not_double_prefixed(self) -> None:
        f = _write(
            "chart:\n  repoURL: oci://ghcr.io/x/charts\n  name: foo\n  version: 1.0.0\n"
        )
        self.assertEqual(ca.read_argocd_chart_url(f), "oci://ghcr.io/x/charts/foo")

    def test_missing_repo_or_name_returns_empty(self) -> None:
        f = _write("chart:\n  name: foo\n  version: 1.0.0\n")  # no repoURL
        self.assertEqual(ca.read_argocd_chart_url(f), "")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(
            ca.read_argocd_chart_url(Path("/nonexistent/release.yaml")), ""
        )


class ReadArgocdReleaseNameTests(unittest.TestCase):
    def test_reads_top_level_release_name(self) -> None:
        f = _write(_GHOST_META)
        self.assertEqual(ca.read_argocd_release_name(f), "ghost")

    def test_strips_inline_comment(self) -> None:
        f = _write("releaseName: demo-db   # adoption name\nchart:\n  version: 1\n")
        self.assertEqual(ca.read_argocd_release_name(f), "demo-db")

    def test_quoted_release_name(self) -> None:
        f = _write('releaseName: "elasticsearch"\n')
        self.assertEqual(ca.read_argocd_release_name(f), "elasticsearch")

    def test_missing_returns_empty(self) -> None:
        f = _write("chart:\n  version: 1.0.0\n")
        self.assertEqual(ca.read_argocd_release_name(f), "")


# =============================================================
# update_argocd_chart_version
# =============================================================


class UpdateArgocdChartVersionTests(unittest.TestCase):
    def test_quoted_preserved_and_comment_kept(self) -> None:
        f = _write(_GHOST_META)
        n = ca.update_argocd_chart_version(f, "0.1.6", "0.1.7")
        self.assertEqual(n, 1)
        text = f.read_text()
        self.assertIn('  version: "0.1.7"', text)
        # repoURL inline comment + other fields untouched.
        self.assertIn("repoURL: ghcr.io/somaz94/charts   # registered OCI repo", text)
        self.assertIn("name: ghost", text)

    def test_bare_preserved(self) -> None:
        f = _write("chart:\n  name: x\n  version: 1.0.0\n")
        n = ca.update_argocd_chart_version(f, "1.0.0", "1.1.0")
        self.assertEqual(n, 1)
        self.assertIn("  version: 1.1.0\n", f.read_text())

    def test_single_quote_preserved(self) -> None:
        f = _write("chart:\n  version: '1.0.0'\n")
        n = ca.update_argocd_chart_version(f, "1.0.0", "2.0.0")
        self.assertEqual(n, 1)
        self.assertIn("  version: '2.0.0'\n", f.read_text())

    def test_inline_comment_preserved_on_update(self) -> None:
        f = _write('chart:\n  version: "1.0.0"  # keep me\n')
        n = ca.update_argocd_chart_version(f, "1.0.0", "1.0.1")
        self.assertEqual(n, 1)
        self.assertIn('  version: "1.0.1"  # keep me\n', f.read_text())

    def test_no_match_when_current_mismatches(self) -> None:
        f = _write('chart:\n  version: "9.9.9"\n')
        original = f.read_text()
        n = ca.update_argocd_chart_version(f, "1.0.0", "1.1.0")
        self.assertEqual(n, 0)
        self.assertEqual(f.read_text(), original)

    def test_does_not_touch_top_level_version(self) -> None:
        f = _write("version: 1.0.0\nchart:\n  version: 1.0.0\n")
        n = ca.update_argocd_chart_version(f, "1.0.0", "2.0.0")
        self.assertEqual(n, 1)
        text = f.read_text()
        # Only the chart-block pin flips; the column-0 version stays.
        self.assertIn("version: 1.0.0\nchart:\n  version: 2.0.0\n", text)

    def test_missing_file_returns_zero(self) -> None:
        self.assertEqual(
            ca.update_argocd_chart_version(
                Path("/nonexistent/release.yaml"), "1.0.0", "1.1.0"
            ),
            0,
        )


# =============================================================
# update_argocd_pins — multi-file fan-out
# =============================================================


class UpdateArgocdPinsTests(unittest.TestCase):
    def test_multiple_files_all_bumped(self) -> None:
        a = _write("chart:\n  version: 1.0.0\n")
        b = _write("chart:\n  version: 1.0.0\n")
        n = ca.update_argocd_pins([a, b], "1.0.0", "1.1.0")
        self.assertEqual(n, 2)
        self.assertIn("version: 1.1.0", a.read_text())
        self.assertIn("version: 1.1.0", b.read_text())

    def test_mixed_only_matching_bumped(self) -> None:
        # An "old" pinned release at a different version is left alone.
        tracked = _write("chart:\n  version: 1.0.0\n")
        pinned_old = _write("chart:\n  version: 0.7.0\n")
        n = ca.update_argocd_pins([tracked, pinned_old], "1.0.0", "1.1.0")
        self.assertEqual(n, 1)
        self.assertIn("version: 1.1.0", tracked.read_text())
        self.assertIn("version: 0.7.0", pinned_old.read_text())

    def test_empty_list_returns_zero(self) -> None:
        self.assertEqual(ca.update_argocd_pins([], "1.0.0", "1.1.0"), 0)


if __name__ == "__main__":
    unittest.main()
