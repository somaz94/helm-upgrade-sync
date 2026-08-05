#!/usr/bin/env python3
# upgrade-template: external-oci-cr-version

# ============================================================
# Configuration (Python dict CONFIG fixture — Python canonical form)
# ============================================================
CONFIG = {
    "SCRIPT_NAME":            "Elasticsearch (ECK CR, OCI chart) Stack Version Upgrade Script",
    "COMPONENT_LABEL":        "elasticsearch",
    "VERSION_SOURCE":         "elastic-artifacts",
    "VERSION_SOURCE_ARG":     "",
    "VALUES_FILE":            "values/dev.yaml",
    "VERSION_KEY":             "version",
    "MAJOR_PIN":              "9",
    "CHANGELOG_URL":          "https://example.com/changelog",
    "CONTAINER_IMAGE":        "docker.elastic.co/elasticsearch/elasticsearch",
    "CR_WEBHOOK_NAME":        "elastic-operator.elastic-system.k8s.elastic.co",
    "CR_OPERATOR_NS":         "elastic-system",
    "CR_OPERATOR_STS":        "elastic-operator",
    "CR_OPERATOR_CHART_DIR":  "eck-operator",
    "DEPENDENCY_CR_KIND":     "",
    "DEPENDENCY_CR_NAME":     "",
    "CHART_SOURCE_TYPE":      "github-releases",
    "CHART_SOURCE_REPO":      "somaz94/helm-charts",
    "CHART_NAME":             "elasticsearch-eck",
}
# ============================================================
