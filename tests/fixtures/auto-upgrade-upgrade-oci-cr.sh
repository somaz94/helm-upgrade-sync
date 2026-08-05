#!/usr/bin/env bash
# upgrade-template: external-oci-cr-version
# Fixture for auto-upgrade tests — header + CONFIG block only, no body.
set -euo pipefail

# CONFIG
VALUES_FILE="values/dev.yaml"
VERSION_KEY="version"
HELM_REPO_NAME="elastic"
HELM_REPO_URL="oci://docker.elastic.co/helm"
