"""Unit tests for upgrade_sync.detect.

Verifies the cascade order matches the bash ``detect_template`` original —
``external-oci-with-mirror`` shadows ``external-oci``, ``local-cr-version``
shadows ``external-oci-cr-version``, etc.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _loader import load


detect = load("upgrade_sync.detect")


def _seed(tmp: Path, body: str) -> Path:
    p = tmp / "upgrade.py"
    p.write_text(body, encoding="utf-8")
    return p


class DetectTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_external_oci_with_mirror_wins_over_external_oci(self) -> None:
        body = 'HELM_CHART="oci://example.com/x"\ndo_mirror() {\n  :;\n}\n'
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "external-oci-with-mirror")

    def test_external_oci_alone(self) -> None:
        body = 'HELM_CHART="oci://example.com/x"\n'
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "external-oci")

    def test_ansible_github_release_when_github_repo_present(self) -> None:
        # Make sure HELM_CHART starts with non-oci so the oci branch misses.
        body = 'HELM_CHART="https://charts.example.com"\nGITHUB_REPO="acme/foo"\n'
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "ansible-github-release")

    def test_local_cr_version_when_version_source_and_mirror_chart(self) -> None:
        body = 'VERSION_SOURCE="github"\nMIRROR_CHART_VERSION="1.0.0"\n'
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "local-cr-version")

    def test_external_oci_cr_version_when_only_version_source(self) -> None:
        body = 'VERSION_SOURCE="github"\n'
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "external-oci-cr-version")

    def test_local_with_templates_when_custom_templates(self) -> None:
        body = 'CUSTOM_TEMPLATES=("foo" "bar")\n'
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "local-with-templates")

    def test_external_with_image_tag_when_marker_string_present(self) -> None:
        body = "# Update image tags in values files\n"
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "external-with-image-tag")

    def test_falls_back_to_external_standard(self) -> None:
        body = "# nothing of interest here\nSCRIPT_NAME=foo\n"
        self.assertEqual(detect.detect_template(_seed(self.tmp, body)), "external-standard")


if __name__ == "__main__":
    unittest.main()
