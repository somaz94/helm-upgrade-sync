# helm-upgrade-sync

Keep per-component chart upgrade scripts in sync from canonical templates.

A repository that manages many Helm components tends to grow one `upgrade.py` per component — and those scripts are 95% identical. Fixing one line in the shared logic means editing every copy, and the copies drift apart in between. This tool splits each script into a **per-component config block** and a **shared body**, keeps the body in one canonical template, and propagates it everywhere on demand.

It also probes upstream for newer chart versions, and cleans up the backup snapshots the upgrade scripts leave behind.

<br/>

## How it works

Every managed script declares which canonical it follows on line 2:

```python
#!/usr/bin/env python3
# upgrade-template: external-standard

# ============================================================
# Configuration (per-component)
# ============================================================
SCRIPT_NAME = "ArgoCD Helm Chart Upgrade"      # ┐
HELM_REPO_NAME = "argo"                         # │ yours — never overwritten
HELM_CHART = "argo/argo-cd"                     # │
CHART_TYPE = "external"                         # ┘
# ============================================================
# ── canonical body below this marker is managed by sync.py ──
```

`sync.py` reads the header, rebuilds the file as *your config + the canonical body*, and compares. `--check` reports drift and exits non-zero; `--apply` rewrites. The config block round-trips untouched, so a sync never clobbers component-specific settings.

<br/>

## Install

No dependencies beyond Python 3.10+ — the whole tool is standard library.

```bash
git clone https://github.com/somaz94/helm-upgrade-sync.git
cd helm-upgrade-sync
./sync.py --repo-root /path/to/your/infra --status
```

<br/>

## Usage

### sync.py — canonical propagation

```bash
./sync.py --status                    # template assignment + drift summary
./sync.py --check                     # exit 1 on drift (CI-friendly)
./sync.py --apply                     # rewrite every managed script
./sync.py --apply --force             # skip the dirty-working-tree guard
./sync.py --print-expected <file>     # what <file> would become, on stdout
```

`--apply` refuses to run against a dirty git tree so a bad propagation is always
recoverable with `git checkout`. Inspect a single file before committing to the
whole sweep:

```bash
./sync.py --print-expected apps/argo-cd/upgrade.py | diff - apps/argo-cd/upgrade.py
```

### check-versions.py — upstream upgrade scan

Read-only. Registers the helm repos it finds in the managed configs, then reports which components have a newer chart upstream.

```bash
./check-versions.py                      # full scan
./check-versions.py --updates-only       # only rows needing an upgrade
./check-versions.py --only logging       # substring filter, repeatable
./check-versions.py --no-update          # skip `helm repo update`
```

### manage-backups.py — snapshot janitor

The upgrade scripts snapshot each component into `<component>/backup/<TIMESTAMP>/` before touching it. Those accumulate.

```bash
./manage-backups.py --list               # per-component count / size / age
./manage-backups.py --total-size
./manage-backups.py --cleanup --keep 3   # keep the 3 newest per component
./manage-backups.py --purge              # remove everything (confirmation required)
```

<br/>

## Which repository does it act on?

Every entry point resolves its target the same way, first match wins:

| # | Source | Notes |
|---|--------|-------|
| 1 | `--repo-root <dir>` | Explicit; beats everything |
| 2 | `UPGRADE_SYNC_REPO_ROOT` | Handy in CI |
| 3 | Embedded layout | When the tool sits at `<repo>/scripts/upgrade-sync/`, that repo |
| 4 | `git rev-parse --show-toplevel` | Git root of the current directory |
| 5 | Current directory | Last resort, outside a worktree |

Rule 3 deliberately outranks rule 4: a copy vendored inside a repository keeps managing *that* repository even when invoked from somewhere else.

<br/>

## Canonical templates

| Template | For |
|---|---|
| `external-standard` | Chart from a helm repo — the common case |
| `external-with-image-tag` | Same, plus an image tag tracked in values |
| `external-oci` | OCI chart, version tracked via GitHub Releases |
| `external-oci-cr-version` | OCI chart consumed by a CR wrapper (`values.version`) |
| `external-oci-with-mirror` | OCI chart mirrored into a private registry first |
| `local-with-templates` | Chart vendored in-repo, with custom templates |
| `local-cr-version` | Vendored CR wrapper — `values.version` + `Chart.yaml.appVersion` |
| `argocd-pin` | Component pinned by an ArgoCD marker file rather than helmfile |
| `ansible-github-release` | Ansible-deployed component tracking GitHub Releases |

### Cluster access for the CR templates

`local-cr-version` and `external-oci-cr-version` talk to a cluster (health probe, dependency-version guard, live CR version, and the downgrade rollback). Every such call is pinned to the context named in `KUBE_CONTEXT`; the current kubectl context is never used implicitly. Set it to the kube-context the chart targets:

```bash
KUBE_CONTEXT="$(kubectl config current-context)" ./upgrade.py --dry-run
```

When `KUBE_CONTEXT` is unset, the read-only checks are skipped and each one says so (`REFUSED` / `constraint NOT verified`), and the downgrade rollback — which scales the operator down and deletes its admission webhook — refuses to run and exits 2. The other templates never contact a cluster and do not need it.

<br/>

## Adding a component

1. Copy the closest canonical from `templates/` to `<component>/upgrade.py`.
2. Set line 2 to `# upgrade-template: <template-name>`.
3. Fill in the config block; leave everything below the marker alone.
4. `./sync.py --check` — the new file should report `OK`.

Discovery walks the target repo for `upgrade.{sh,py}`, skipping `backup/`, `_deprecated/`, `_optional/`, and test fixtures. Nothing needs registering.

<br/>

## Two layouts

**Standalone** — clone it anywhere and point `--repo-root` at the repository you manage. Best when several repositories share one copy of the tool.

**Embedded** — vendor it at `<repo>/scripts/upgrade-sync/` (entry points and `templates/`) plus `<repo>/scripts/python/` (the `upgrade_sync` / `upgrade_core` packages). The tool then defaults to its own repository with no flags, which suits a single infrastructure monorepo and its CI.

<br/>

## Development

```bash
make test          # unittest suite (stdlib only, no network)
make lint          # byte-compile every module
```

The suite builds synthetic repositories in temporary directories, so it never
reads the repository it is checked out into and gives the same result anywhere.

<br/>

## License

Apache License 2.0 — see [LICENSE](LICENSE).
