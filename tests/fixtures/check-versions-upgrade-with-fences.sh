#!/usr/bin/env bash
# upgrade-template: external-oci-cr-version
# Fixture for check-versions tests — full CONFIG fence block.
set -euo pipefail

# ============================================================
# Configuration (ONLY section that differs between scripts)
# ============================================================
SCRIPT_NAME="Elasticsearch CR Stack Version Upgrade Script"
VALUES_FILE="values/dev.yaml"
VERSION_KEY="version"
HELM_REPO_NAME="elastic"
HELM_REPO_URL="oci://docker.elastic.co/helm"
VERSION_SOURCE="elastic-artifacts"
MAJOR_PIN="9"
GITHUB_TAG_PREFIX="${GITHUB_TAG_PREFIX:-v}"
CHART_SOURCE_TYPE="github-releases"
CHART_SOURCE_REPO="somaz94/helm-charts"
CHART_NAME="elasticsearch-eck"
# ============================================================

# Body intentionally omitted — fixture stops at the third fence.
