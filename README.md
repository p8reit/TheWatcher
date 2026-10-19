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

Open http://localhost:8088 (or your configured `UI_PORT`). The demo seed runs once on an empty database. Search `SYNTHETIC`, or load character ID `900001`. Explore fitting patterns and use **Train model** to exercise the chronological training pipeline. Demo metrics do not establish real predictive accuracy.

Stop with `docker compose -p new-eden-demo down`. Data persists in the named Docker volume. Stop the demo before starting live mode because both use port 8088.

## Change the UI port

Set `UI_PORT` in your existing `.env` file. For example:

```dotenv
UI_PORT=8099
```

Recreate the web container using the same Compose project name you used when starting it:

```sh
docker compose -p new-eden-demo up -d --force-recreate web
```

For live mode use `-p new-eden-live` instead. Open http://localhost:8099 for this example. Existing `.env` files without `UI_PORT` default to 8088. The mapping stays bound to host loopback; the internal web port and health check remain 8080. Changing the host port preserves the database; do not rerun the demo seed or remove volumes.

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

A missing live sequence is retried without crashing or advancing the cursor. A pointer ahead of a 404 is not treated as proof of permanent loss. Polling waits at least six seconds, backs off to 60 seconds when the pointer is ahead, and resumes automatically when the exact record becomes available. `/api/status` reports the wait state, first detection, attempts and retry interval. An indefinitely missing/expired record still requires operator recovery; retries cannot reconstruct it. Only after confirming that recovery is necessary, an explicit `--reset-to-current` skips the unavailable interval and saves an audit record. Stop the regular collector before resetting:

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
- Live first-seen training requires at least 21 days of first-seen history, plus label maturity and minimum sample/class checks. A chronological holdout must improve Brier score at least 2% over the per-system frequency baseline before the model is used. Otherwise the baseline remains active.
- Models remain experimental and uncalibrated. Holdout evaluation samples clock-hour origins; live estimates use a rolling horizon. Models expire after seven days and are retrained manually.
- Reports, archives and collection gaps are incomplete. Silent hours are not proof of zero combat. Only observed systems are ranked.
- Descriptive analytics cap each query at 50,000 records. Live first-seen training refuses capped data; historical archive training aggregates its selected systems directly in SQLite. Configure a small `ANALYSIS_SYSTEM_IDS` scope for longer observation windows. Collection currently stores all events without automatic retention; monitor disk use.
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

### Recent system activity

Open **System activity** to compare the latest stored ESI NPC/ship/pod report with individually recorded killmails in a rolling 1, 6 or 24 hour window. `/api/system-activity?hours=6` serves the same local-only data. Dashboard refreshes never poll upstream services or invoke a model.

ESI columns show the **latest hourly report inside the window**, not sums across the window. Missing reports display unknown; reports older than two hours are marked stale. Sampled UTC hours indicate reporting samples, not uninterrupted coverage. Ship counts include destruction by NPCs; differences from killmail counts do not prove unpublished PvP kills. Event time, source report time and first receipt time remain separate. Legacy Hunter snapshots are excluded; demo data stays isolated.

Enable the existing `npc-collector` service for ESI reports and the existing `collector` service for zKill R2Z2 discovery. Both remain opt-in and retain their cache/backoff behavior. Historical backfills use daily archives. The view applies the intersection of `NPC_SYSTEM_IDS` and `ANALYSIS_SYSTEM_IDS` when both are configured, ranks up to 200 systems, and explicitly rejects analysis exceeding 200,000 report rows or 50,000 killmails instead of silently truncating counts. Optional authenticated ESI character/corporation killmail ingestion is a future addition; this view does not introduce EVE SSO.

### Human readable names

The web service automatically resolves names in the background using public ESI and stores them in the shared SQLite cache. Set a contact-bearing `USER_AGENT` (for example `TheWatcher/0.2 (https://github.com/p8reit/TheWatcher)`). `NAME_RESOLVER_ENABLED` defaults to `true`; set it to `false` to disable outbound name lookups. No EVE SSO or model inference is required.

Existing killmail history is discovered incrementally after upgrading. New killmails and NPC snapshots queue system, pilot, corporation, alliance, faction, ship and nested fitting-item references automatically. Names needed by a displayed view take priority. Cached names refresh after 30 days; requests use batches of at most 100 IDs, pacing, bounded invalid-batch isolation, a persistent global retry pause for failed requests, and daily retries for unavailable names. Lookup failures no longer stop combat collection. Synthetic demo IDs are never sent to ESI.

Views use cached names and automatically refresh each minute. While the initial backlog resolves, they show `Resolving … name…`; unavailable names get an explicit label instead of an ID or invented identity. The sidebar reports pending names and lookup errors, also available in `/api/status` under `names`. Existing cached names remain visible during an outage. Dashboard requests queue missing references locally; they never call ESI directly. Large initial caches can take several minutes or longer to populate.

Numeric IDs, exact fitting signatures and event/receipt timestamps remain intact in stored data, APIs and model training. Cards, search results and evidence links display readable labels; the explicit manual ID input is under **Advanced lookup by EVE ID**. Local model narration receives resolved labels with identifier fields stripped. Rebuild/recreate `web`, `collector` and `npc-collector` using your existing live Compose project to activate the update; no seed, volume deletion or retraining is required. Preserve your local port configuration.

### Ask analyst by location

The analyst area accepts a question with a **System**, **Constellation**, or **Region** selection, or **From question** to use a location named in the question. For example: “What combat was observed in Jita over the last 24 hours?” or “Summarize activity in The Forge over the last 7 days.” The default window is 24 hours; supported windows are 1–2160 hours. Selecting a cached location can also come from the contextual **Ask analyst** actions. Profile context is included only when **Use the loaded entity profile** is explicitly checked.

Answers summarize local killmail evidence: recorded pilot, corporation, alliance and ship involvement. Killmails are partial combat observations. They do not show whether a character is online or establish a current position. Missing location data is reported as unavailable; region and constellation answers state that they cover mapped systems only. Demo mode uses synthetic seeded geography and evidence.

Named location parsing uses the local geography cache and consumes zero LLM tokens. The background geography resolver uses public ESI and requires `esi.evetech.net` to be reachable, plus the existing contact-bearing `USER_AGENT` and `NAME_RESOLVER_ENABLED=true`. `GEOGRAPHY_RESOLVER_ENABLED` defaults to `true`; set it to `false` to disable background geography refreshes. Cached reads never fetch ESI per question. Unresolved known system IDs remain selectable by ID while their names are unavailable.

The local endpoints are `GET /api/analyst/locations?q=&kind=` for cached choices and `POST /api/chat` for a question, optionally with `location: {"kind":"system|constellation|region","id":123}` and integer `hours` from 1 to 2160. A location omitted from the request can be parsed from the question when it matches the local cache.


### Train immediately on historical killmail archives

In **Activity forecasts**, choose **Live first-seen** or **Historical killmail archives**, then select 1–12 systems from the **System activity estimates** table before clicking **Train & evaluate**. The preview reports records read per system; it does not promise that every record becomes a training label. Live counts include NPC records before PvP filtering. The UI shows the aggregate for the selected systems. Training uses a local statistical model and 0 LLM tokens. The live selection must stay below 50,000 records. A selection narrows `ANALYSIS_SYSTEM_IDS` when configured, and training still applies its chronological and sample checks. The UI requires at least one selected system.

The API accepts an optional selection, for example `POST /api/train?mode=live` or `POST /api/train?mode=historical&days=42` with `{"system_ids":[930001]}`. `GET /api/training-scope?mode=live` or `?mode=historical&days=42` returns per-system record counts, the window and cap metadata. Omitting `system_ids` or posting `{}` preserves the configured automatic defaults.

For historical training, first import 21–42 completed UTC days of zKill archives into the live project. Then open **Activity forecasts**, select **Historical killmail archives**, select systems from **System activity estimates**, and click **Train & evaluate**. Alternatively:

```sh
curl -X POST 'http://127.0.0.1:8099/api/train?mode=historical&days=42' \
  -H 'Content-Type: application/json' -d '{}'
```

Use your configured UI port. The historical window uses up to the requested number of most recent consecutive completed archive days, with a minimum of 21. Incomplete days and gaps are not treated as completed coverage. Seven days warm up lag features; the remaining clock hours split chronologically 75%/25%. Systems are selected only from the training portion (up to 12, restricted by `ANALYSIS_SYSTEM_IDS` when set). SQL aggregation avoids silently truncating large global imports to the display's 50,000 records. Existing sample/class checks and the 2% Brier improvement gate still apply. A `baseline_retained` result means training ran, but the model did not earn promotion.

This is **retrospective event-time evaluation**: it assumes an archived killmail was available by its event time, since its actual historical publication delay is unknown. It does not demonstrate real-time prediction accuracy, current positions or online status. Archive completeness means a successful provider-file import, not a complete record of all combat in EVE. Numeric IDs and original event/receipt times are preserved. The artifact is stored separately from the live first-seen model.

An accepted, unexpired live model takes precedence. Otherwise an accepted historical model can supply estimates explicitly labeled `Experimental historical model (retrospective)` with live accuracy unverified. Current historical-model features use actual received-by-now records across the full selected system scope. Both artifacts expire after seven days; the API exposes both under `/api/forecast` → `models`. `/api/train` without a mode continues to enforce the live first-seen gate. Demo datasets default to live/demo training. NPC count training still requires verified ESI snapshot history; zKill archives do not supply historical ratting counts.

### Contextual intelligence workspace

TheWatcher groups its views into observation, analysis, and the local analyst. System names expose Dotlan, zKillboard, and copy-name actions; unresolved names retain the ID-backed zKill action while Dotlan waits for a resolved name. Synthetic demo systems and killmails never link to real evidence. Sortable table headers on all views sort the displayed rows and toggle direction.

**Battle watch** opens in fresh mode with a five-minute window, based on both killmail event time and first receipt time. While visible, it refreshes every 15 seconds. One fresh PvP loss is an early signal; two fresh losses in a cluster that started within 15 minutes are marked **Possible forming**. An older cluster with a new loss is marked **Recent combat**. These labels describe reported evidence, not a confirmed fleet or a battle known to be ongoing. Delayed backfills do not promote a cluster to a fresh signal. Use **History** to review the existing 6- or 24-hour reports.

Fresh view filters are available through `GET /api/battles?mode=fresh&fresh_minutes=5&space=lowsec&hours=6`; `fresh_minutes`, `space`, and `hours` are optional. The API keeps history mode as its default for compatibility. Space filters use cached ESI security metadata and show **High Sec**, **Low Sec**, **Null Sec**, **J Space (Wormhole)**, or **Unknown space** badges. Raw security `>= 0.45` is highsec, `0 < security < 0.45` is lowsec, and `<= 0` is nullsec. System IDs in `[31000000, 32000000)` identify wormhole space regardless of security; missing security for other systems stays unknown and is never assumed to mean nullsec. Names and security metadata come from the local cache; see [Human readable names](#human-readable-names) for the background ESI resolver requirements. The view makes no ESI requests.

History reports (`GET /api/battles?hours=24`, or `6`) group locally observed PvP killmails in the same system when successive events are at most 30 minutes apart, with a maximum two-hour span and at least two kills. Cards expose recorded losses, distinct identified pilots, reported ISK, and killmail evidence. WarBeacon and EVE Tools links open contextual reports for the system and starting UTC hour; review their window and side assignment. They do not publish a saved report or guarantee exactly the local set of kills. URL contracts follow zKillboard's `templates/related.pug` and `classes/RelatedReport.php`.

Combat analysis respects `ANALYSIS_SYSTEM_IDS`, excludes future events/receipts and NPC-only losses, and fails explicitly above 50,000 records. It displays the newest 40 history windows and up to 12 evidence links per card. Existing collectors remain opt-in; collection timing, model training and Docker ports are unchanged.
