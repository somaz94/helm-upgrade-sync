#!/usr/bin/env bash
# upgrade-template: external-oci-cr-version
# Component fixture — CR/image template variant (elasticsearch-shaped).
set -euo pipefail

# CONFIG
VALUES_FILE="values/dev.yaml"
VERSION_KEY="version"
HELM_REPO_NAME="elastic"
HELM_REPO_URL="oci://docker.elastic.co/helm"
