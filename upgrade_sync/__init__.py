"""upgrade_sync — canonical-to-consumer body sync for per-chart upgrade.py.

This package replaces the bash ``scripts/upgrade-sync/sync.sh`` (P5-A,
resolute-bison). It keeps every per-chart ``upgrade.py`` body in sync with
its canonical template under ``scripts/upgrade-sync/templates/``.

External CLI surface (preserved byte-for-byte from bash sync.sh):

- ``--check``                       diff every managed upgrade.py against its canonical
- ``--apply [--force]``             rewrite every managed upgrade.py from its canonical
- ``--status``                      template tally + canonicals + unmanaged chart list
- ``--print-expected <file>``       stdout what <file> would look like after sync

The package layout exposes three reusable sub-modules consumed by
``scripts/upgrade-sync/check-versions.py`` and ``scripts/ci/auto-upgrade.py``:

- :mod:`upgrade_sync.discovery` — ``find_managed_files`` / ``parse_template_header``
- :mod:`upgrade_sync.detect`    — ``detect_template`` (content-based fallback)
- :mod:`upgrade_sync.extract`   — CONFIG / body extraction + ``build_expected``
"""
