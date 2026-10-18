# NPC integration review

## 1. Summary

Additive NPC count snapshots, offline Hunter import/audit, chronological model evaluation, and a separate New Eden Intel NPC view. A small Hunter patch adds an opt-in exporter. This is a reviewable prototype; no production installation or database was accessed.

## 2. Supported-technology verification

No new application dependency is added or upgraded. Native code uses existing HTTPX, FastAPI, NumPy and scikit-learn plus Python's standard library. The Hunter exporter uses only the standard library. Its offline legacy exporter uses the project's existing mysql-connector-python. Relevant primary references: Python 3.11 os.link and email.utils documentation; MySQL Connector/Python start_transaction(readonly=True); HTTPX timeout documentation; scikit-learn 1.6 Ridge/StandardScaler documentation. Ridge is a stable API; the resulting application forecast remains experimentally evaluated, not a proven operational prediction service.

Inherited dependency warnings remain in unrelated v0.1 paths: Starlette references an AnyIO alias, and the PvP logistic model passes a SciPy solver option that SciPy now ignores. No warning suppression or unrelated dependency upgrade was introduced. Fresh-environment tests pass; pinning/revalidating transitive versions is still needed before production deployment.

## 3. Changes made

First receipt time and source period end are stored separately. Snapshots are atomic and idempotent; conflicting replays fail. Legacy history remains descriptive. Hourly count prediction is evaluated against baselines, with an explicit promotion gate, source-age checks and model expiry. No generated ship guesses are training labels. Missing source rows are unknown.

## 4. Files changed

- `app/npc_store.py`: additive local snapshot tables, provenance and cap.
- `app/npc_collector.py`: opt-in ESI snapshots and conditional cache handling.
- `app/npc_import.py`: offline audit/import and legacy classification.
- `app/npc_predictor.py`: count model, chronological evaluation and baseline fallback.
- `app/npc_demo.py`: explicit synthetic NPC pipeline dataset.
- `app/main.py`: additive NPC API routes and initialization.
- `app/store.py`, `app/demo.py`, `app/analytics.py`: cross-dataset demo/live protection and synthetic status handling.
- `app/chat.py`: NPC retrieval for local summaries/narration.
- `app/static/index.html`, `app/static/app.js`: separate NPC view in the new app.
- `.env.example`, `compose.yaml`: opt-in NPC settings/service; existing published port unchanged.
- `.github/workflows/tests.yml`: includes exporter unit tests.
- `tests/test_npc_pipeline.py`, `tests/test_npc_predictor.py`: ingestion, API, leakage, forecasting and failure coverage.
- `integrations/hunter/npc_intel_export.py`, `export_npc_legacy.py`, `collector.patch`, `compose.npc-intel.yaml`, `tests/`, `hunter_tests/`: reviewable Hunter integration.
- `README.md`, `docs/HUNTER_INTEGRATION.md`, `docs/NPC_CONTRACT.md`, `docs/VALIDATION.md`, this review: operation and limitations.

## 5. Port, protocol, and network impact

New Eden Intel retains its existing loopback host mapping 127.0.0.1:8088. The optional worker makes outbound HTTPS calls to ESI. Hunter's overlay publishes no port, opens no listener and adds no Docker network. Target-host listener ownership was not inspectable. Check before launch. No DNS, firewall, credential, TLS or production changes were made.

## 6. Breaking-change assessment

Existing PvP API shapes, Hunter data tables, collector cadence, maps and prediction results are retained. New SQLite tables and API routes are additive. Hunter's source is a minimal import and guarded optional callback; its overlay requires explicit selection. No volume replacement or schema conversion. Existing Hunter map/dashboard rendering remains outside this patch.

## 7. Security assessment

Parameterized local SQL; bounded file/batch/query sizes; strict numeric/time validation; no pickle/model-code loading; append-only publication; optional read-only MySQL transaction. New endpoints inherit same-origin JSON/host guards and local binding. Provenance metadata is trusted operator input rather than cryptographic attestation. No public multi-user authentication is introduced. Keep the app local and snapshot/export files protected.

## 8. Risks and mitigations

| Risk | Severity / likelihood | Impact | Mitigation and residual risk |
|---|---|---|---|
| Missing/delayed or selectively reported rows | Medium / likely | Biased activity estimates | Explicit missingness, receipt timing, holdout and baselines; reporting bias remains |
| Misread count as ship identity | Medium / possible | Incorrect hunt decisions | Counts only, visible caveats; scout labels still required |
| Source archive/dataset growth | Medium / likely over time | Disk or CPU pressure | Scope collection, 90-day analysis window, 200k-row cap, operator archival; no automatic retention deletion |
| Export storage failure | Low / possible | Lost optional training epochs | Warning plus isolated primary pipeline; missing history remains missing |
| Inaccurate or fabricated imported provenance | Medium / possible | Invalid training evaluation | Audit and trusted exports only; cannot cryptographically verify receipt times |
| Scope changes conflict with saved epochs | Low / possible | Replay import/collector rejects a batch | Choose one ingestion source/scope per dataset; preserve original snapshots |
| Untested target Docker/MySQL/browser | Medium / possible | Deployment or UI issues | Draft integration, explicit validation commands, no deployment; host validation remains |
| Concurrent repeated CPU training | Low / possible | Temporary resource pressure | Per-process lock, existing two-thread math limits, bounded selected systems/data; single-worker deployment recommended |
| Inherited dependency warnings | Low / certain | Future incompatibility or noisy logs | Documented, no suppression; fresh CI and transitive dependency review still required |

## 9. Tests performed

See `VALIDATION.md` for exact commands and final results. New coverage includes source epoch conflicts, invalid batches, receipt preservation, synthetic separation, legacy exclusion, conditional cache responses, retry delays, source scopes/caps, API origin guards, real chronological model training, stale/expired model behavior, and Hunter collector export failure isolation.

## 10. Tests not performed

Docker build/run and target listener checks: Docker and target host unavailable. Real ESI ingestion, GPU/Ollama narration, live MySQL read-only export, full Hunter regression suite and browser rendering were not exercised. Hunter's narrow callback is function-tested without importing its full Flask stack. The exporter is disabled by default. Review/CI and host validation are needed before enabling it.

## 11. Rollback procedure

New app: `docker compose -p new-eden-live --profile npc stop npc-collector`; return to the previous source/image, retaining the existing volume. The added tables can remain unused; do not drop data to roll back. Restore the matching prior UI/backend together.

Hunter: `sudo docker compose -f compose.yaml up -d --force-recreate collector` returns to the base configuration without the export mount/env. Keep exported files; reverting the patch does not require deleting them or changing MySQL. If source code is reverted, rebuild the collector from that reviewed revision with the same base Compose file. Do not run `down -v`.

## 12. Remaining concerns

Live database history has not been audited. Seven-day legacy retention cannot be reversed. No learned ship classifier, scout-feedback training, joint NPC/PvP model, automatic model scheduler, or Hunter map/dashboard embedding is claimed. Seasonal/persistence holdout comparison is an evaluation gate, not a guarantee of future performance.

## 13. Recommended next step

Review the draft Hunter exporter patch, validate the target Compose configuration, and audit a protected legacy export. Collect verified snapshots for the chosen systems. Evaluate on actual retained data before using the count model operationally. Production enablement remains an operator action; no further approval is needed to inspect or test the delivered source locally.
