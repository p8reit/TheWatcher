# The Watcher

A local EVE Online combat intelligence workbench: zKillboard ingestion, pilot/corporation/alliance profiles, exact lost-fit patterns, experimental activity forecasts, and optional local Ollama explanations.

**v0.2 prototype.** No hosted inference, automatic model downloads, or paid model API calls. Starts empty; demo data is explicitly synthetic and uses a separate database. Killmails describe reported combat, not login times or current locations.

## Hunter integration and NPC training

See [HUNTER_INTEGRATION.md](docs/HUNTER_INTEGRATION.md) for NPC collection, Hunter exports, audits, source timing, and model evaluation. The optional Hunter exporter is available in [draft PR #340](https://github.com/p8reit/thehunter/pull/340). This release adds a separate **NPC intelligence** view while retaining the existing combat APIs.

## Run the demo

Requires Docker Engine/Desktop with Compose v2. Clone the repository, then run the demo:

```sh
git clone https://github.com/p8reit/TheWatcher.git
cd TheWatcher
```

From the repository folder:

```sh
cp .env.example .env
docker compose -p new-eden-demo build
docker compose -p new-eden-demo run --rm web python -m app.demo
docker compose -p new-eden-demo up -d web
```

Open http://localhost:8088. The demo seed runs once on an empty database. Search `SYNTHETIC`, or load character ID `900001`. Explore fitting patterns and use **Train model** to exercise the chronological training pipeline. Demo metrics do not establish real predictive accuracy.

Stop with `docker compose -p new-eden-demo down`. Data persists in the named Docker volume. Stop the demo before starting live mode because both use port 8088.

## Collect live data

Edit `.env`:

```dotenv
COLLECTOR_ENABLED=true
USER_AGENT=NewEdenIntel/0.1 (your-real-contact-email-or-project-URL)
# Optional system scope for analytics; collector still stores all received data.
ANALYSIS_SYSTEM_IDS=30000142,30000144
```

Choose your own system IDs, or leave the scope empty. Then:

```sh
docker compose -p new-eden-live --profile live up -d --build
docker compose -p new-eden-live logs -f collector
```

The project name keeps live and demo volumes separate. First startup begins at the current R2Z2 sequence. Subsequent runs resume the saved next sequence. Ingestion is idempotent and preserves first receipt time. ESI names are cached; unresolved names display as IDs.

Optional import of the last seven completed UTC days:

```sh
docker compose -p new-eden-live --profile live run --rm collector python -m app.collector --backfill-days 7
```

This exits after importing; add `--follow` to keep polling. Maximum backfill: 90 days. Use `--refresh-archives` with `--backfill-days` to revisit completed archive days. Imports help descriptive analysis immediately, but their receipt times are **now**, so they cannot fabricate historical forecasting observations.

A missing live sequence stalls collection instead of silently skipping data. After inspecting the logs, an explicit `--reset-to-current` skips the unavailable interval and saves an audit record. Stop the regular collector before resetting:

```sh
docker compose -p new-eden-live --profile live stop collector
docker compose -p new-eden-live --profile live run --rm collector python -m app.collector --reset-to-current
docker compose -p new-eden-live --profile live up -d collector
```

Archive import and manual reset do not prove coverage of the gap. Consult the provider's [current API documentation](https://zkillboard.com/api/docs/) before operating a long-running collector. This release uses R2Z2 rather than the retired RedisQ interface.

## Optional local AI

Run your existing Ollama server with a downloaded model. Set `OLLAMA_NO_CLOUD=1` **on the Ollama server**, and restart that server. The app also rejects remote model origins and remote-backed model tags.

In `.env`:

```dotenv
LLM_ENABLED=true
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3.5:9b
LLM_DAILY_TOKEN_BUDGET=50000
```

Use the exact installed model tag shown by `ollama list`. No model is pulled by this app. A 9B model's performance depends on quantization, VRAM and other GPU usage; use a smaller installed model if necessary. With narration disabled, the analyst returns calculated summaries without using model tokens.

The Ollama server must be reachable from Docker. A host server listening only on `127.0.0.1` may not accept container traffic, especially on Linux. Configure an appropriate private Docker-facing bind address and firewall; keep port 11434 off public networks. For a native Python run, use `OLLAMA_URL=http://127.0.0.1:11434`.

Recreate the web service after changing `.env`:

```sh
docker compose -p new-eden-live up -d --force-recreate web
```

The budget is a configurable **local inference** limit, independent of ChatGPT/Codex subscription usage. Each request reserves 8,960 tokens, then settles to Ollama's reported usage. Failures retain their reservation. Requests stop when insufficient budget remains; the accounting day resets at UTC midnight. This app cannot read or enforce your ChatGPT daily allowance.

## Forecasting and evidence

- Profiles count distinct killmail involvement and separate attacker/victim sides. Association means shared attacker participation, not common account ownership.
- Fit patterns use exact fitted-module fingerprints of lost ships. They do not reconstruct attacker fits or confirm fleet doctrines.
- Forecasts estimate at least one recorded PvP killmail in a system over the next 60 minutes. Expected kills is a separate historical hourly mean.
- Training requires at least 21 days of first-seen history, plus label maturity and minimum sample/class checks. A chronological holdout must improve Brier score at least 2% over the per-system frequency baseline before the model is used. Otherwise the baseline remains active.
- Models remain experimental and uncalibrated. Holdout evaluation samples clock-hour origins; live estimates use a rolling horizon. Models expire after seven days and are retrained manually.
- Reports, archives and collection gaps are incomplete. Silent hours are not proof of zero combat. Only observed systems are ranked.
- Analytics cap each query at 50,000 records. Training refuses capped data. Configure a small `ANALYSIS_SYSTEM_IDS` scope for longer observation windows. Collection currently stores all events without automatic retention; monitor disk use.
- Evidence links are attached independently of the model; synthetic records never link to real killmails. The model can still misinterpret evidence, so inspect the underlying calculations.

## Development

Python 3.12; Node is needed only for the JavaScript syntax check.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
node --check app/static/app.js
DATABASE_PATH=./data/demo.db python -m app.demo
DATABASE_PATH=./data/demo.db uvicorn app.main:app --host 127.0.0.1 --port 8088
```

Native Python does not automatically load `.env`; export the settings you need. API routes: `/api/health`, `/api/status`, `/api/overview`, `/api/entities`, `/api/profile/{kind}/{id}`, `/api/doctrines`, `/api/forecast`, `POST /api/train`, and `POST /api/chat`.

The app has no multi-user authentication. Compose publishes only to host loopback. Do not expose it publicly. No arbitrary SQL, shell execution, or model tool use is available to the narrator.

## Repository

Source: [p8reit/TheWatcher](https://github.com/p8reit/TheWatcher). Internal Python modules and Compose project defaults retain the New Eden Intel name for compatibility. The companion Hunter exporter is a separate draft change; publishing this repository does not deploy either application.

## Validation and next milestones

Automated coverage includes API/assets, data validation, deduplication, cursor and archive handling, rate limits, demo/live separation, profile/fit semantics, observation-time leakage, chronological demo training, and local model budget behavior. See `docs/VALIDATION.md` for the release check results.

Next work: validate live collection on the target machine; add collection-coverage intervals and scoped retention; collect enough real history; evaluate calibration and seasonal baselines; improve fit clustering and natural-language entity lookup. This release does not fine-tune an LLM.

Independent project; not affiliated with CCP Games or zKillboard. EVE Online and related marks belong to CCP. Source code and provider data have separate rights; no license to redistribute third-party data is implied.
