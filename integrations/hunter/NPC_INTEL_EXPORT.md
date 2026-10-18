# Optional NPC snapshots for New Eden Intel

This patch captures source-timestamped ESI system kill counts for offline NPC activity training. It does not change Hunter's classifier, map, dashboard, notifications, authentication, polling cadence or MySQL schema. Export is disabled unless HUNTER_NPC_SNAPSHOT_DIR is set in the collector container.

## Enable after review

From Hunter's root:

```sh
mkdir -p var/npc-intel
sudo docker compose -f compose.yaml -f compose.npc-intel.yaml config --quiet
sudo docker compose -f compose.yaml -f compose.npc-intel.yaml up -d --build collector
```

The optional overlay adds one persistent collector bind mount and environment variable. No new port, network or service is added. Source files appear at `var/npc-intel/esi-*.json` after ESI returns a fresh result. Each includes version=1, source=esi, quality=verified, source_end from Last-Modified, first observed_at in UTC, and the source-reported system counts. Missing source rows remain missing. Repeated epochs preserve first receipt; conflicting counts at an existing epoch are rejected. Publication uses a temporary file and an exclusive atomic hard link in the same directory.

Snapshot export failures log exception type and leave Hunter's primary collection running. Watch collector warnings and storage space. The collector does not silently convert export failure into a training sample. No automatic retention deletion is introduced: keep at least 90 days, monitor disk, and archive old exports explicitly. Existing seven-day legacy predictor retention is unchanged.

In the separately supplied New Eden Intel v0.2 package, run `python -m app.npc_import PATH` for an audit, then add `--apply` to write to its own SQLite dataset. Native imports preserve source and receipt time. Never import unknown snapshots as trusted provenance. Choose one ingestion source/scope per dataset; a repeated source epoch with different scope is a conflict.

## Legacy history audit

The optional script reads only npc_activity_history in a read-only MySQL transaction, using existing app configuration and connector. A SELECT-only identity is preferable. It does not write schema or records:

```sh
sudo docker compose run --rm --no-deps collector python -m scripts.export_npc_legacy --days 90 > hunter-npc-legacy.jsonl
```

This exports only history still retained; it cannot recover pruned records. Protect exports. New Eden Intel marks this history legacy and excludes it from training because source reporting timestamps are not preserved. Existing generated ship guesses are not training labels. Native snapshots support activity-count evaluation; they do not establish pilot identity, ship composition or exact anomaly.

## Review and validation

Summary: additive, opt-in data handoff; no model replacement or UI change.

Supported technology: Python standard library os.link/tempfile/email.utils; existing mysql-connector-python start_transaction(readonly=True). No dependency changes. MySQL 8 supports read-only transactions. The exporter targets the existing Linux Docker runtime.

Files: test.py (guarded callback), npc_intel_export.py (snapshot writer), scripts/export_npc_legacy.py (offline reader), compose.npc-intel.yaml (explicit overlay), tests/test_npc_intel_export.py and tests/test_npc_export_hook.py (tests), this document.

Compatibility/security: existing APIs, tables, volumes, ports, auth and default startup retain their behavior. Snapshot directory and output files are operator-managed. No database mutation, credential output, public listener or automatic upload is added. Export is synchronous on 200 responses; it validates at most 20,000 rows.

Risks: disk exhaustion/slow storage (Medium/Possible; monitor and archive, primary collection catches export exceptions but blocking storage may delay a cycle); missing/late reports (Medium/Likely; preserve both times and mark gaps); imported provenance errors (Medium/Possible; trusted files only); optional host bind permissions (Low/Possible; validate target path); legacy incompleteness (Medium/Likely; excluded from training). Remaining risks require host observation and real-data evaluation.

Tests performed: isolated exporter and real collector-function tests, including export failure and 304 handling, passed in a Python 3.12 test environment. See PR description for exact totals. The collector function is executed from its AST with mocked external services rather than importing its entire Flask stack.

Not performed: full Hunter regression suite, live MySQL export, Docker build/config/runtime on the target host, real ESI capture or visual QA. No rendered surfaces changed. Run the targeted tests in Hunter's existing test environment, then the normal regression/CI suite and the Compose check above before enabling. No claim is made that this is deployed or production validated.

Rollback:

```sh
sudo docker compose -f compose.yaml up -d --force-recreate collector
```

This disables the optional overlay on the recreated collector. Keep source archives; no MySQL rollback is required. If reverting source too, rebuild the collector from the previous reviewed revision using the base Compose file. Do not delete volumes. No production approval is requested by this draft; enabling export remains an explicit operator deployment action.

Next step: review this small patch, validate on the target host, and audit a protected legacy export. Ship-label training, joint NPC/PvP features and Hunter map embedding are later work.
