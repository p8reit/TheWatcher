# New Eden Intel v0.1 contract
Local Docker app, Python FastAPI + SQLite WAL + vanilla JS dashboard. No remote LLMs. No paid API calls.
Database module app/store.py provides connect(db_path=None), init_db(), ingest_killmail(payload, source='r2z2', observed_at=None)->bool, list_killmails(days=30,limit=1000)->list[dict], get_state(key,default=None), set_state(key,value), entity_name(id)->str. Each list record raw ESI fields + zkb + observed_at + source. env DATABASE_PATH default ./data/intel.db. UTC ISO timestamps. Ingestion source demo must never mix with live data. source column and stats warnings.
app/collector.py provides CLI python -m app.collector: --once, --backfill-days N optional; env COLLECTOR_ENABLED=false default, USER_AGENT must identify project, R2Z2 with persisted sequence and missing/expired gap detection; current official docs https://zkillboard.com/api/docs/. HTTP retry bounded with backoff, no burst. enrich cached ESI entity names in entities(id,name,category,updated_at). Don't assume current corp affiliation is historical.
Root implements API, analytics, model training, chat, deployment.
API contract for frontend (JSON):
GET /api/overview?days=30 {total_kills,total_value,active_systems,active_pilots,last_observed_at,data_mode,activity:[{hour,kills}],systems:[{id,name,kills,value}],recent:[{killmail_id,time,system_id,system_name,victim_ship,victim_name,value,url}],collector:{enabled,cursor,last_error},limitations:[str]}
GET /api/entities?q=text {items:[{id,name,category}]}
GET /api/profile/{kind}/{id}?days=30 kind character|corporation|alliance returns {id,name,kind,kills,losses,activity:[{hour,kills}],ships:[{id,name,count}],systems:[{id,name,count}],associates:[{id,name,count,weighted_score}],evidence:[{killmail_id,url,time}],limitations:[str]}
GET /api/doctrines?days=30 {items:[{ship_id,ship_name,signature,modules:[str],losses,evidence:[{killmail_id,url}]}],limitations:[str]}
GET /api/forecast?days=90 {items:[{system_id,system_name,probability,expected_kills,history_hours,method,confidence}],model:{status,trained_at,metrics},limitations:[str]}
GET /api/status {data_mode,llm:{enabled,model,daily_budget,tokens_used},collector:{enabled,cursor,last_error},model:{status,metrics}}
POST /api/chat {question:string,entity_kind?:string,entity_id?:int} => {answer,evidence:[{killmail_id,url}],mode,tokens_used}; 400/429/503 actionable errors {detail:string}.
POST /api/train {} => {status,metrics,message}; mutating requests require Origin same host if present; app binds localhost by default. No raw SQL or shell exposed.
GET /api/health {status:'ok'}.
Frontend read-only status/data dashboard; profile form kind + ID, optional entity search; doctrine view; forecasts with honest baseline labels; chat, train button. Poll refresh manual/60s; no fake numbers; excellent empty/loading/error UI. demo CLI python -m app.demo, manually invoked into empty DB only, shows conspicuous synthetic dataset label. No auto demo seeding.
