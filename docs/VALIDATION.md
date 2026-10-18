# v0.2 validation

## Executed

- `.venv/bin/pytest -q tests integrations/hunter/tests`: **54 passed** (fresh Python 3.12 virtual environment with declared dependencies).
- In the isolated Hunter review tree: `../eve-intel/.venv/bin/pytest -q tests`: **5 passed**, including the three shared exporter tests and two additional collector-loop tests. Unique total across both runs: 56 tests.
- `node --check app/static/app.js`: passed.
- The prior standalone repository-publishing helper was shell-checked, then removed when the user supplied TheWatcher as the destination.
- `git diff --check`: passed.
- Python compilation of patched Hunter collector and exporter: passed.
- Explicit synthetic NPC seed/train/forecast smoke check: 864 source snapshots across four systems; real chronological training and four fresh count forecasts. Synthetic performance is not real-world predictive accuracy.

The fresh environment reports two inherited warnings: Starlette/AnyIO deprecation and the v0.1 PvP logistic model's SciPy solver `iprint` option. Neither is suppressed; no unrelated dependency upgrades were made.

API/assets, source epoch replay/conflict, atomic invalid batches, receipt preservation, demo/live isolation, legacy exclusion, import audit/apply, 304 deduplication, Retry-After deferral, analysis scope/caps, same-origin writes, receipt-time feature leakage, chronological purge, baseline promotion/retention and model freshness/expiry are exercised. Hunter's real collector loop function is extracted via AST and run with mocked collaborators to verify export failure isolation and 304 behavior.

## Not executed

Docker build/Compose runtime (Docker unavailable); supported browser visual rendering; real R2Z2/ESI ingestion; actual GPU/Ollama narration; live Hunter MySQL read-only export; the complete Hunter regression suite; remote CI. No live database was audited, no production installation was changed, and no production deployment occurred. TheWatcher publication is recorded in the GitHub pull request; this validation describes local checks. The draft patch needs normal Hunter CI and target-host checks before enablement.

## Target-host follow-up

```sh
# New Eden Intel, after making a local .env
pytest -q tests integrations/hunter/tests
node --check app/static/app.js
docker compose config --quiet
docker compose build

# Hunter patch, in its configured test environment
pytest -q tests/test_npc_intel_export.py tests/test_npc_export_hook.py
# Then run Hunter's normal full regression/CI commands.
sudo docker compose -f compose.yaml -f compose.npc-intel.yaml config --quiet
```
