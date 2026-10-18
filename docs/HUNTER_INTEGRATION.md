# Hunter NPC integration — v0.2

This release adds source-timestamped NPC activity ingestion and evaluated count forecasts to New Eden Intel. Hunter integration is an opt-in snapshot exporter and an offline legacy-history reader. It does not replace Hunter's classifier or alter its maps, UI, authentication, notifications, or database schema. Embedding evaluated results in Hunter's protected map/dashboard is a later, separately reviewed change.

## Fastest local demo

Use the existing README demo instructions, then seed the NPC examples in that same demo project:

```sh
docker compose -p new-eden-demo run --rm web python -m app.npc_demo
docker compose -p new-eden-demo up -d --build web
```

Open http://localhost:8088 and select **NPC intelligence**. Click **Train NPC model**. The count model trains on 36 days of fictional source reports. Its evaluation checks software behavior, not actual hunting performance. Seeding is explicit and refuses existing NPC history or live datasets.

## Direct ESI collection (no Hunter changes required)

In the New Eden Intel `.env`, configure:

```dotenv
NPC_COLLECTOR_ENABLED=true
NPC_SYSTEM_IDS=30000142,30000144
USER_AGENT=NewEdenIntel/0.2 (your-real-contact-email-or-project-URL)
```

Choose your own systems. Start with a small scope (roughly 3–10 systems) to keep training and disk usage bounded. The examples are system identifiers, not recommended hunting locations. The collector stores only returned rows in scope; absent systems are unknown rather than fabricated zero counts. This makes the training population conditional on source reporting.

```sh
docker compose -p new-eden-live --profile npc up -d --build web npc-collector
docker compose -p new-eden-live logs -f npc-collector
```

This uses the existing local port 8088 and database volume. It does not enable the zKill collector. Do not run live collection in the demo project. If another app owns port 8088, resolve that deployment conflict before starting. Source `Last-Modified` and first receipt time are retained; 304 responses do not add samples; retry/cache headers defer subsequent polling.

## Export new source snapshots from Hunter

The Hunter draft patch adds `npc_intel_export.py`, a small collector hook, tests, and an opt-in Compose overlay. If applying the packaged source manually, copy `integrations/hunter/npc_intel_export.py` to Hunter's root and apply `integrations/hunter/collector.patch` from that root with `git apply --check` followed by `git apply`. The patch targets the inspected main revision; stop if it fails rather than replacing the complete collector.

After reviewing/merging the Hunter patch, enable its overlay explicitly:

```sh
mkdir -p var/npc-intel
sudo docker compose -f compose.yaml -f compose.npc-intel.yaml config --quiet
sudo docker compose -f compose.yaml -f compose.npc-intel.yaml up -d --build collector
```

The overlay changes only the collector's environment and adds a persistent bind mount. It publishes no port and starts no extra service. Existing Hunter volumes remain in the base configuration. Snapshot export errors log the exception type and allow primary collection to continue; monitor warnings and free disk space. New files appear in `var/npc-intel/esi-*.json` after ESI returns a new snapshot. Atomic publication preserves the first saved receipt time. Source-epoch conflicts are rejected. Keep at least 90 days externally; no automatic deletion is introduced. All files remain retained until the operator archives/removes them.

Choose direct collection OR Hunter snapshots for a database: the same source epoch with a different scope is a conflicting replay, not a safe merge.

## Audit and import snapshots

On the New Eden Intel host, place an exported file in an `imports` directory. Native import does not require MySQL access. The mount is read-only, and `--apply` writes only to the intelligence database:

```sh
mkdir -p imports
# Copy a trusted exported file here as imports/snapshot.json.
docker compose -p new-eden-live run --rm -v "$PWD/imports:/imports:ro" web python -m app.npc_import /imports/snapshot.json
# Review the audit output before applying.
docker compose -p new-eden-live run --rm -v "$PWD/imports:/imports:ro" web python -m app.npc_import /imports/snapshot.json --apply
```

Repeat per file; imports are idempotent and atomic per source snapshot. A multi-snapshot import can commit earlier valid batches before a later conflicting batch fails; rerunning after resolving the conflict is safe. Files are capped at 50 MiB and each snapshot at 20,000 rows. Provenance is trusted operator input, not cryptographic verification: do not import unknown files as verified evidence.

The audit reports quality, date span, per-system sample counts, long gaps, and consecutive equal counts. Equal values may be genuine activity; the audit does not automatically treat them as proof of duplicates. Summary coverage is limited to the first 100 systems. Forecast analysis caps at 200,000 rows and raises an error rather than truncating silently. Narrow `NPC_SYSTEM_IDS` when needed.

## Audit Hunter's retained legacy history

The current Hunter predictor prunes its history after seven days. This release preserves that behavior and creates a separate durable source archive for future training. Historical predictor rows do not retain the ESI reporting epoch, so they cannot establish source-hour training labels reliably.

The optional `export_npc_legacy.py` uses Hunter's existing MySQL connector and a read-only transaction. In the Hunter draft it lives at `scripts/export_npc_legacy.py`. Run it through the existing service; a SELECT-only database identity is preferable:

```sh
sudo docker compose run --rm --no-deps collector python -m scripts.export_npc_legacy --days 90 > hunter-npc-legacy.jsonl
```

It exports only retained history. It cannot recover pruned records. Protect the output file and inspect command failures before importing. Audit/import it using the same `app.npc_import` commands above. Legacy observations stay descriptive and are excluded from model training. Generated Hunter ship guesses and prediction strings are not imported as ground truth.

## Forecast target and evaluation

The model forecasts the next source-reported hourly NPC count. Source reporting delays mean its target period may already have started by the time the preceding report arrived. The UI displays the actual target end. Forecasts for already-ended target periods are withheld, even when the last source snapshot is less than two hours old.

Training requires at least 21 days of verified receipt-time history, at least 300 training and 100 held-out report pairs, and source periods separated by 45–75 minutes. Only observations available at the historical forecast origin become features. Labels crossing the chronological split or received after that split are purged from training. The model must lower holdout MAE by at least 2% against the strongest evaluated baseline (persistence and, when adequately covered, a 24-hour seasonal baseline). Otherwise the baseline remains active. Models expire after seven days.

No ship composition, pilot identity, exact anomaly, or live location is learned from aggregate counts. Scout-labeled training, joining NPC features into PvP forecasts, and embedding results in Hunter's map remain future work.

## Interfaces

- `GET /api/npc/status`: provenance/quality totals and collector state.
- `GET /api/npc/forecast`: forecasts, source age, target period, method, coverage, and evaluation metrics.
- `POST /api/npc/train` with JSON `{}`: explicit bounded-scope CPU training, guarded against concurrent requests.
- Local analyst questions containing `NPC` or `ratting` retrieve these count estimates. No remote inference is introduced.

Existing PvP endpoints are retained. The new SQLite tables are additive. No migration writes to Hunter MySQL. Snapshot imports and model training are explicit operations; no background training or automatic notifications are enabled.
