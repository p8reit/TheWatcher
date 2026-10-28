'use strict';
const $ = id => document.getElementById(id);
const state = {view:'overview', days:30, profile:null, generation:0, mode:null, activity:null, selectedSystems:new Map(), trainingPreview:null, trainingBusy:false, npcScope:null, npcAuto:true, npcSelected:new Set(), npcTrainingBusy:false, npcSelectionVersion:0, analystLocation:null, analystChoices:new Map(), locationGeneration:0, lastRefreshAttempt:0};
const titles = {battles:['Battle watch','Find fresh combat signals, inspect recent losses, and follow the evidence.'],'system-activity':['System activity','Reported ESI activity beside locally collected killmail evidence.'],npc:['NPC intelligence','Source-timestamped ratting activity and evaluated count forecasts.'],overview:['Operational overview','Understand the activity. Follow the evidence.'],profiles:['Entity intelligence','Observed patterns, recurring associates, and supporting evidence.'],doctrines:['Doctrine analysis','Discover recurring ship and fitting patterns in recorded losses.'],forecasts:['Activity forecasts','Measured expectations, grounded in historical observations.'],analyst:['Your intel analyst','Turn collected evidence into a useful briefing.']};
function node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined&&text!==null)el.textContent=String(text);if(cls)el.className=cls;return el;}
function clear(id){const el=typeof id==='string'?$(id):id;el.replaceChildren();return el;}
function number(value){return Number.isFinite(Number(value))&&value!==null&&value!==undefined?Number(value).toLocaleString(undefined,{maximumFractionDigits:0}):'—';}
function isk(value){if(value===null||value===undefined||!Number.isFinite(Number(value)))return '—';return new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:2}).format(Number(value))+' ISK';}
function date(value){if(!value)return 'Not yet observed';const d=new Date(value);return Number.isNaN(d.valueOf())?'Unknown time':d.toISOString().slice(0,16).replace('T',' ')+' UTC';}
function empty(container,message='No observations available for this window.'){clear(container).append(node('div',message,'empty'));}
async function api(path,body){const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});let data;try{data=await response.json();}catch{throw new Error('The server returned an unreadable response. Check that the API is running.');}if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:`Request failed (${response.status}). Please check the service logs.`);return data;}
function error(message){$('global-error').textContent=message||'';$('global-error').hidden=!message;}
function limitations(id,items){const el=clear(id);(items||[]).forEach(item=>el.append(node('p',item)));el.hidden=!items?.length;}
function metric(label,value,caption){const el=node('article',null,'metric');el.append(node('div',label,'metric-label'),node('div',value,'metric-value'),node('div',caption,'metric-caption'));return el;}
function activity(container,items){const root=clear(container);if(!items?.length||!items.some(i=>i.kills>0)){empty(root);return;}const byHour=new Map(items.map(i=>[Number(i.hour),Number(i.kills)||0]));const max=Math.max(...byHour.values(),1);const bars=node('div',null,'bars');bars.setAttribute('role','img');bars.setAttribute('aria-label','UTC hourly killmail counts: '+Array.from({length:24},(_,h)=>`${h}:00 ${byHour.get(h)||0}`).join(', '));for(let h=0;h<24;h++){const slot=node('div',null,'bar-slot');const value=byHour.get(h)||0;slot.title=`${String(h).padStart(2,'0')}:00 UTC · ${number(value)} killmails`;const bar=node('div',null,'bar');bar.style.height=`${Math.max(0,value/max*100)}%`;slot.append(bar);bars.append(slot);}const labels=node('div',null,'hour-labels');['00:00','06:00','12:00','18:00','23:00'].forEach(x=>labels.append(node('span',x)));root.append(bars,labels);}
const tableSorts = new Map();
function sortCell(display,value){return {display,sortValue:value};}
function cellSortValue(value){
 if(value&&typeof value==='object'&&'sortValue' in value)return value.sortValue;
 if(value instanceof Node){
  if(value.dataset.sortValue!==undefined)return value.dataset.sortValue;
  return value.querySelector('strong')?.textContent||value.textContent;
 }
 if(value===null||value===undefined||['—','Not yet observed','Unknown time'].includes(value))return null;
 const text=String(value).trim();
 // Displayed counts, percentages, and compact ISK amounts sort by magnitude.
 const numeric=text.replaceAll(',','').match(/^(-?\d+(?:\.\d+)?)\s*([KMBT])?\s*(?:ISK|%|min)?$/i);
 if(numeric)return Number(numeric[1])*({K:1e3,M:1e6,B:1e9,T:1e12}[numeric[2]?.toUpperCase()]||1);
 return text;
}
function table(container,headers,rows){
 const el=clear(container);if(!rows.length){empty(el);return;}
 const key=el.id||headers.join('|');
 const t=node('table'),head=node('thead'),hr=node('tr'),body=node('tbody');
 const entries=rows.map((row,index)=>{
  const tr=node('tr');row.forEach(value=>{const td=node('td');const display=value&&typeof value==='object'&&'display' in value?value.display:value;if(display instanceof Node)td.append(display);else td.textContent=display??'—';tr.append(td);});
  return {tr,index,values:row.map(cellSortValue)};
 });
 const apply=()=>{
  const sort=tableSorts.get(key);
  const ordered=[...entries];
  if(sort)ordered.sort((a,b)=>{
   const value=entry=>typeof entry.values[sort.column]==='function'?entry.values[sort.column]():entry.values[sort.column];
   const x=value(a),y=value(b);
   if(x==null||y==null)return x==null?(y==null?a.index-b.index:1):-1;
   const result=typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y),undefined,{numeric:true,sensitivity:'base'});
   return result*(sort.descending?-1:1)||a.index-b.index;
  });
  body.replaceChildren(...ordered.map(e=>e.tr));
  [...hr.children].forEach((th,column)=>{const active=sort?.column===column;th.setAttribute('aria-sort',active?(sort.descending?'descending':'ascending'):'none');th.firstChild.textContent=headers[column]+(active?(sort.descending?' ↓':' ↑'):' ↕');});
 };
 headers.forEach((h,column)=>{const th=node('th');th.scope='col';const button=node('button',h,'sort-heading');button.type='button';button.addEventListener('click',()=>{const previous=tableSorts.get(key);tableSorts.set(key,{column,descending:previous?.column===column?!previous.descending:false});apply();});th.append(button);hr.append(th);});
 head.append(hr);t.append(head,body);t.addEventListener('change',apply);el.append(t);apply();
}

function externalLink(label,url,description){const a=node('a',label,'context-link');a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.setAttribute('aria-label',description||label+' (opens in a new tab)');return a;}
function resolvedName(name){return typeof name==='string'&&name.length>0&&!/^(resolving|unknown|unresolved)|name unavailable$/i.test(name);}
function systemCell(id,name,synthetic=false){
 const root=node('div',null,'system-cell');root.append(node('strong',name||'Resolving system name…'));
 const ask=node('button','Ask analyst','copy-action');ask.type='button';ask.setAttribute('aria-label',`Ask analyst about ${name||'this system'}`);ask.addEventListener('click',()=>openSystemAnalyst(id,name));
 if(state.mode==='demo'||synthetic){root.append(ask);return root;}
 const actions=node('div',null,'context-actions');actions.append(ask);
 if(Number.isSafeInteger(Number(id))&&Number(id)>0)actions.append(externalLink('zKill ↗',`https://zkillboard.com/system/${Number(id)}/`,`Open ${name||'system'} on zKillboard`));
 if(resolvedName(name)){
  actions.append(externalLink('Dotlan ↗','https://evemaps.dotlan.net/system/'+encodeURIComponent(name.replaceAll(' ','_')),`Open ${name} on Dotlan`));
  const copy=node('button','Copy','copy-action');copy.type='button';copy.setAttribute('aria-label','Copy '+name+' system name');
  copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(name);copy.textContent='Copied';setTimeout(()=>copy.textContent='Copy',1600);}catch{error('Clipboard unavailable. Select and copy the system name.');}});actions.append(copy);
 }
 root.append(actions);return root;
}
function badge(value){return node('span',value||'Unknown','badge '+(value==='fresh'?'good':value==='stale'?'warn':'neutral'));}
function evidenceLink(item){const id=String(item.killmail_id||'');if(!/^\d+$/.test(id))return node('span','Invalid evidence ID');if(state.mode==='demo'||item.synthetic)return node('span','Synthetic evidence');const a=node('a','View killmail ↗');a.dataset.sortValue=id;a.href=`https://zkillboard.com/kill/${id}/`;a.target='_blank';a.rel='noopener noreferrer';a.setAttribute('aria-label','Open killmail on zKillboard');return a;}
function evidence(container,items){const list=node('div',null,'evidence');(items||[]).slice(0,12).forEach(item=>list.append(evidenceLink(item)));container.append(list);}
function rankings(container,items,valueKey='kills'){const root=clear(container);if(!items?.length){empty(root);return;}items.slice(0,6).forEach((item,index)=>{const row=node('div',null,'rank-row');const name=systemCell(item.id,item.name);if(item.value!==undefined)name.append(node('small',isk(item.value)));row.append(node('span',String(index+1).padStart(2,'0'),'rank-index'),name,node('span',number(item[valueKey]),'rank-number'));root.append(row);});}
async function status(){const data=await api('/api/status');const previousMode=state.mode;state.mode=data.data_mode;if(data.data_mode==='demo'&&previousMode!=='demo')$('training-mode').value='live';$('demo-banner').hidden=data.data_mode!=='demo';$('data-status').textContent=`${data.data_mode||'Unknown'} dataset`;$('ai-status').textContent=data.llm?.enabled?`Local ${data.llm.agent_enabled?'agent':'AI'} · ${data.llm.model||'Configured'}`:'Local AI disabled';$('name-status').textContent=data.names?.last_error|| (data.names?.pending?`${number(data.names.pending)} names awaiting resolution${data.names.enabled?'':' · configure USER_AGENT to enable lookups'}`:'Names cached locally');$('token-status').textContent=`Daily inference tokens: ${number(data.llm?.tokens_used)} / ${number(data.llm?.daily_budget)}`;return data;}
function overview(data){state.mode=data.data_mode;$('demo-banner').hidden=data.data_mode!=='demo';const m=clear('metrics');m.append(metric('Killmails in window',number(data.total_kills),data.scope_filtered?'Selected time window · configured systems':'Selected time window · all systems'),metric('Reported value',isk(data.total_value),data.analysis_truncated?'Newest analyzed killmails only':'Killboard estimated value'),metric('Active systems',number(data.active_systems),data.analysis_truncated?'In the analysis sample':'With recorded combat'),metric('Observed pilots',number(data.active_pilots),data.analysis_truncated?'Distinct pilots in the sample':'Distinct combat participants'));$('analysis-coverage').textContent=`Stored in database: ${number(data.stored_kills)} killmails (all dates and systems). Analyzed: ${number(data.analyzed_kills)} of ${number(data.total_kills)} matching this window${data.scope_filtered?' and configured systems':''}. `+(data.analysis_truncated?`Analysis limit reached: the newest ${number(data.analysis_limit)} records drive value, system/pilot counts, rankings and hourly activity. This limit does not stop collection. Narrow the observation window to reduce truncation.`:'All matching killmails are included in this analysis.');activity('activity',data.activity);rankings('systems',data.systems);table('recent',['Time (UTC)','System','Victim','Ship','Value','Evidence'],(data.recent||[]).map(k=>[date(k.time).replace(' UTC',''),systemCell(k.system_id,k.system_name),k.victim_name||'Unresolved pilot',k.victim_ship||'Unresolved ship',sortCell(isk(k.value),k.value),evidenceLink(k)]));$('last-observed').textContent=`Last received: ${date(data.last_observed_at)}`;$('collector-status').textContent=`Collector ${data.collector?.enabled?'enabled':'disabled'}${data.collector?.last_error?' · '+data.collector.last_error:''}`;limitations('overview-limitations',data.limitations);}
function profile(data){state.profile={kind:data.kind,id:data.id};const root=clear('profile-result');const heading=node('article',null,'panel');heading.append(node('span',String(data.kind||'entity').toUpperCase(),'eyebrow'),node('h2',data.name||'Resolving entity name…'));if(state.mode!=='demo')heading.append(externalLink('View on zKillboard ↗',`https://zkillboard.com/${data.kind}/${Number(data.id)}/`));const m=node('div',null,'metrics');m.append(metric('Kill appearances',number(data.kills),'Recorded attacker participation'),metric('Losses',number(data.losses),'Recorded victim appearances'));heading.append(m);root.append(heading);const grid=node('div',null,'grid-two');const a=node('article',null,'panel');a.append(node('h2','Observed combat hours'));const chart=node('div',null,'activity');activity(chart,data.activity);a.append(chart);const b=node('article',null,'panel');b.append(node('h2','Operating systems'));const list=node('div');rankings(list,data.systems,'count');b.append(list);grid.append(a,b);root.append(grid);const ships=node('article',null,'panel');ships.append(node('h2','Observed ships'));const st=node('div',null,'table-wrap');table(st,['Ship','Appearances'],(data.ships||[]).map(s=>[s.name||'Resolving ship name…',number(s.count)]));ships.append(st);root.append(ships);const associates=node('article',null,'panel');associates.append(node('h2','Recurring associates'),node('p','Shared combat appearances are not proof of shared ownership or permanent cooperation.','panel-description'));const at=node('div',null,'table-wrap');table(at,['Pilot','Shared appearances','Weighted score'],(data.associates||[]).map(a=>[a.name||'Resolving pilot name…',number(a.count),Number.isFinite(Number(a.weighted_score))?Number(a.weighted_score).toFixed(3):'—']));associates.append(at);evidence(associates,data.evidence);root.append(associates);const lim=node('div',null,'limitations');(data.limitations||[]).forEach(l=>lim.append(node('p',l)));root.append(lim);}
function doctrines(data){const root=clear('doctrine-results');if(!data.items?.length)empty(root,'No fitting patterns found. Collect killmails containing lost-ship fittings.');(data.items||[]).forEach(item=>{const card=node('article',null,'panel');card.append(node('span',`${number(item.losses)} RECORDED LOSSES`,'eyebrow'),node('h2',item.ship_name||'Resolving ship name…'),node('p','Matching fitted modules','panel-description'));const modules=node('ul',null,'module-list');(item.modules||[]).forEach(m=>modules.append(node('li',typeof m==='string'?m:JSON.stringify(m))));card.append(modules);evidence(card,item.evidence);root.append(card);});limitations('doctrine-limitations',data.limitations);}
function metrics(container,values){const root=clear(container);Object.entries(values||{}).forEach(([key,value])=>{const el=node('div',key.replaceAll('_',' '),'detail');el.append(node('b',typeof value==='number'?Number(value.toFixed(4)):typeof value==='object'?JSON.stringify(value):value));root.append(el);});}
function trainingSummary(){
 const preview=state.trainingPreview, selected=[...state.selectedSystems.keys()];
 const counts=new Map((preview?.items||[]).map(i=>[i.system_id,i.records]));
 const total=selected.reduce((sum,id)=>sum+(counts.get(id)||0),0);
 const missing=selected.some(id=>!counts.has(id));
 let issue=preview?.unavailable_reason||'';
 if(!preview)issue='Loading training record counts…';
 else if(!issue&&!selected.length)issue='Select one or more systems below.';
 else if(selected.length>preview.max_systems)issue=`Select at most ${preview.max_systems} systems per training run.`;
 else if(!issue&&missing)issue='Some selected systems have no records in this training window. Remove them or change the training data.';
 else if(!issue&&preview.record_cap&&total>=preview.record_cap)issue='The selection reaches the 50,000-record cap. Select fewer systems.';
 $('training-selection').textContent=`${number(selected.length)} systems selected · ${preview&&!preview.unavailable_reason?number(total):'Unknown'} records · 0 LLM tokens. `+(selected.length?'Selected: '+[...state.selectedSystems.values()].join(', ')+'. ':'')+issue;
 $('training-window').textContent=preview?.window?`Record window: ${preview.window}. Counts refresh with the page; eligibility is checked again at training time.`:'';
 $('train').disabled=state.trainingBusy||!!issue;
 $('clear-training').disabled=state.trainingBusy||!selected.length;
}
function forecasts(data){
 state.trainingPreview=data.training;
 $('model-status').textContent=`Status: ${data.model?.status||'Unknown'} · ${data.model?.training_mode==='historical'?'Historical retrospective evaluation':'Live first-seen evaluation'}${data.model?.trained_at?' · Trained '+date(data.model.trained_at):''}. ${data.model?.message||''} Estimates use the method shown for each system.`;
 metrics('model-metrics',data.model?.metrics);
 const counts=new Map((data.training?.items||[]).map(i=>[i.system_id,i.records]));
 const candidates=new Map((data.items||[]).map(i=>[i.system_id,i]));
 for(const item of data.training?.items||[])if(!candidates.has(item.system_id))candidates.set(item.system_id,{system_id:item.system_id,system_name:item.system_name||`System ${item.system_id} · name unavailable`,method:'No displayed estimate',confidence:'—'});
 table('forecast-results',['Train','System','Records in training window','LLM tokens','Activity probability','Expected kills','History (hours)','Method','Confidence'],[...candidates.values()].map(i=>{
  const checkbox=node('input');checkbox.type='checkbox';checkbox.checked=state.selectedSystems.has(i.system_id);checkbox.disabled=state.trainingBusy;checkbox.setAttribute('aria-label',`Train on ${i.system_name} (${i.system_id})`);
  checkbox.addEventListener('change',()=>{if(checkbox.checked)state.selectedSystems.set(i.system_id,i.system_name+' ('+i.system_id+')');else state.selectedSystems.delete(i.system_id);trainingSummary();});
  return [sortCell(checkbox,()=>checkbox.checked?1:0),systemCell(i.system_id,i.system_name),sortCell(counts.has(i.system_id)?number(counts.get(i.system_id)):data.training?.unavailable_reason?'Unavailable':'0',counts.get(i.system_id)??null),'0',sortCell(i.probability!=null?(Number(i.probability)*100).toFixed(1)+'%':'—',i.probability),sortCell(i.expected_kills!=null?Number(i.expected_kills).toFixed(2):'—',i.expected_kills),number(i.history_hours),i.method,i.confidence];
 }));
 trainingSummary();limitations('forecast-limitations',data.limitations);
}

function saveNpcSelection(){try{localStorage.setItem('watcher-npc-selection',JSON.stringify({auto:state.npcAuto,ids:[...state.npcSelected]}));}catch{}}
function npcSelection(){return state.npcAuto?{auto_select:true}:{system_ids:[...state.npcSelected]};}
function npcSelectionIssue(){
 const scope=state.npcScope;if(!scope)return 'System scope is unavailable.';
 const ids=state.npcAuto?(scope.auto_system_ids||[]):[...state.npcSelected];
 const count=state.npcAuto?Number(scope.auto_records||0):(scope.items||[]).filter(i=>state.npcSelected.has(i.system_id)).reduce((sum,i)=>sum+Number(i.records||0),0);
 if(!ids.length)return scope.unavailable_reason||'Select at least one system with recorded NPC history.';
 if(ids.length>scope.max_systems)return `Select no more than ${number(scope.max_systems)} systems.`;
 if(count>scope.max_records)return `Selection exceeds the ${number(scope.max_records)}-record cap. Choose fewer systems.`;
 return '';
}
function npcSelectionSummary(){
 const scope=state.npcScope,ids=state.npcAuto?(scope?.auto_system_ids||[]):[...state.npcSelected];
 const selected=(scope?.items||[]).filter(i=>ids.includes(i.system_id));
 const count=state.npcAuto?Number(scope?.auto_records||0):selected.reduce((sum,i)=>sum+Number(i.records||0),0);
 const issue=npcSelectionIssue();
 $('npc-selection-status').textContent=scope?`${state.npcAuto?'Automatic selection':'Manual selection'} · ${number(ids.length)} / ${number(scope.max_systems)} systems · ${number(count)} / ${number(scope.max_records)} records · 0 LLM tokens. ${issue}`:'Loading NPC system scope…';
 $('npc-selection-detail').textContent=scope?`Selected: ${selected.length?selected.map(i=>`${i.system_name||'System '+i.system_id} (${number(i.records)} records)`).join(', '):'none'}. ${state.npcAuto?'Automatic selection includes whole system histories that fit the cap; it may leave room unused. ':''}${scope.unavailable_reason||''}`:'';
 $('npc-train').disabled=state.npcTrainingBusy||!!issue;
 $('npc-apply').disabled=state.npcTrainingBusy||!!issue;
 $('npc-auto').disabled=state.npcTrainingBusy||!scope;
}
function npcSelectionChanged(){state.npcSelectionVersion++;saveNpcSelection();npcSelectionSummary();clear('npc-model-metrics');limitations('npc-limitations',[]);empty('npc-results','Selection changed. Apply it to load estimates for these systems.');$('npc-model-status').textContent='Apply the current selection to load its estimates.';}
function renderNpcScope(scope){
 state.npcScope=scope;
 const available=new Set((scope.items||[]).map(i=>i.system_id));
 state.npcSelected=new Set([...state.npcSelected].filter(id=>available.has(id)));
 $('npc-auto').checked=state.npcAuto;
 const window=scope.window;
 $('npc-scope-window').textContent=`Record window: ${window?.from?date(window.from)+' to '+date(window.through):'available observations'}. Counts include complete histories per system and refresh from the server; training checks eligibility again.`;
 const q=$('npc-system-search').value.trim().toLowerCase();
 const items=(scope.items||[]).filter(i=>`${i.system_name||''} ${i.system_id}`.toLowerCase().includes(q));
 const rows=items.map(i=>{
  const checkbox=node('input');checkbox.type='checkbox';checkbox.checked=state.npcAuto?(scope.auto_system_ids||[]).includes(i.system_id):state.npcSelected.has(i.system_id);checkbox.disabled=state.npcAuto||state.npcTrainingBusy;checkbox.setAttribute('aria-label',`Select ${i.system_name||'system '+i.system_id} (${number(i.records)} records)`);
  checkbox.addEventListener('change',()=>{if(checkbox.checked)state.npcSelected.add(i.system_id);else state.npcSelected.delete(i.system_id);npcSelectionChanged();});
  return [sortCell(checkbox,()=>checkbox.checked?1:0),systemCell(i.system_id,i.system_name),sortCell(number(i.records),i.records),date(i.first_observed_at),date(i.last_observed_at),i.history_ready?'21+ days observed':'History not yet ready'];
 });
 table('npc-scope-results',['Select','System','NPC records','First received (UTC)','Last received (UTC)','Training history'],rows);
 if(!rows.length)empty('npc-scope-results',q?'No systems match this search.':'No systems have recorded NPC history in this scope.');
 npcSelectionSummary();
}
function npcSource(source){const totals=source.sources||[];$('npc-source-status').textContent=`${source.data_mode} NPC dataset · `+totals.map(x=>`${x.quality}: ${number(x.snapshots)} snapshots, ${number(x.observations)} rows`).join(' · ')+(source.last_error?' · '+source.last_error:'');}
function npcForecast(forecast){$('npc-model-status').textContent=`Model: ${forecast.model?.status||'not_trained'} · ${forecast.model?.message||'Persistence baseline until evaluated.'} · ${forecast.stale_systems||0} systems withheld (stale or target period ended)`;metrics('npc-model-metrics',forecast.model?.metrics);table('npc-results',['System','Latest NPCs','Estimated NPCs','Target end (UTC)','Coverage 24h','Method'],(forecast.items||[]).map(i=>[systemCell(i.system_id,i.system_name),number(i.latest_npc_kills),sortCell(number(i.expected_npc_kills),i.expected_npc_kills),date(i.target_end),(Number(i.coverage_24h)*100).toFixed(0)+'%',i.method]));limitations('npc-limitations',forecast.limitations||[]);}
async function loadNpc(generation){
 const [scopeResult,sourceResult]=await Promise.allSettled([api('/api/npc/training-scope'),api('/api/npc/status')]);
 if(generation!==state.generation)return;
 if(sourceResult.status==='fulfilled')npcSource(sourceResult.value);else $('npc-source-status').textContent='NPC source status unavailable: '+sourceResult.reason.message;
 if(scopeResult.status==='rejected'){state.npcScope=null;npcSelectionSummary();empty('npc-scope-results','NPC system scope unavailable: '+scopeResult.reason.message);empty('npc-results','Forecast unavailable while system scope cannot be loaded.');return;}
 renderNpcScope(scopeResult.value);
 const issue=npcSelectionIssue();
 if(issue){clear('npc-model-metrics');empty('npc-results',issue);$('npc-model-status').textContent='Choose a valid NPC system scope to load estimates.';return;}
 const version=state.npcSelectionVersion;
 const query=state.npcAuto?'auto_select=true':'system_ids='+encodeURIComponent([...state.npcSelected].join(','));
 try{const forecast=await api('/api/npc/forecast?'+query);if(generation===state.generation&&version===state.npcSelectionVersion)npcForecast(forecast);}
 catch(err){if(generation===state.generation&&version===state.npcSelectionVersion){clear('npc-model-metrics');empty('npc-results','Forecast unavailable: '+err.message);$('npc-model-status').textContent='Forecast unavailable: '+err.message;limitations('npc-limitations',[]);}}
}
function systemActivity(data){
 state.activity=data;
 const label=data.data_mode==='demo'?'Synthetic demo':'ESI';
 $('system-activity-status').textContent=`As of ${date(data.as_of)} · ${number(data.total_systems)} systems · ${number(data.reported_killmails)} recorded killmails · ${label} collector ${data.esi_collector_enabled?'enabled':'disabled'} · zKill collector ${data.collector?.enabled?'enabled':'disabled'}${data.esi_last_error?' · '+data.esi_last_error:''}${data.collector?.last_error?' · '+data.collector.last_error:''}`;
 table('system-activity-results',['System','Latest NPCs','Latest ships','Latest pods','Killmails in window','Report end (UTC)','First received (UTC)','Report age','Freshness','Sampled UTC hours'],filteredSystems(data.items).map(i=>[systemCell(i.system_id,i.system_name),number(i.npc_kills),number(i.ship_kills),number(i.pod_kills),number(i.reported_killmails),date(i.source_end),date(i.observed_at),sortCell(i.source_age_seconds===null?'—':number(i.source_age_seconds/60)+' min',i.source_age_seconds),badge(i.freshness),number(i.sampled_hours)]));
 table('system-activity-recent',['Event (UTC)','First received (UTC)','System','Source','Evidence'],data.recent.map(k=>[date(k.time),date(k.observed_at),systemCell(k.system_id,k.system_name),k.source,evidenceLink(k)]));
 limitations('system-activity-limitations',data.limitations);
}
function age(seconds){
 if(seconds===null||seconds===undefined||!Number.isFinite(Number(seconds)))return 'Unknown';
 const value=Math.max(0,Math.floor(Number(seconds)));
 if(value<60)return `${value}s`;
 if(value<3600)return `${Math.floor(value/60)}m ${value%60}s`;
 return `${Math.floor(value/3600)}h ${Math.floor(value%3600/60)}m`;
}
function spaceBadge(item){
 const el=node('span',item.space_label||'Unknown space','badge space-badge space-'+(item.space_type||'unknown'));
 el.title=item.security_status!=null?`Security status: ${Number(item.security_status).toFixed(4)}`:item.space_type==='wormhole'?'Wormhole space':'Security metadata is unavailable; background ESI lookups supply it.';
 return el;
}
function battles(data){
 state.mode=data.data_mode;const fresh=data.mode==='fresh';
 $('battle-freshness-control').hidden=!fresh;
 $('battle-summary').textContent=`${number(data.items?.length||0)} of ${number(data.total_windows||0)} ${fresh?'fresh combat signals':'observed combat groups'} · ${fresh?`Events and reports within ${data.fresh_minutes} minutes`:`Last ${data.hours} hours`} · As of ${date(data.as_of)}`;
 const collector=data.collector||{};
 $('battle-feed-status').textContent=data.data_mode==='demo'?'SYNTHETIC DEMO · Fictional combat observations.':(collector.last_success?`Last recorded ingestion: ${date(collector.last_success)}.`:'No live ingestion heartbeat recorded. Fresh signals require incoming killmail reports.')+(collector.wait?' Feed is waiting for an unavailable sequence; observations may be delayed.':'')+(collector.last_error?' Feed error: '+collector.last_error:'');
 const expanded=new Set([...$('battle-results').querySelectorAll('details[open]')].map(el=>el.dataset.key));
 const root=clear('battle-results');
 if(!data.items?.length)empty(root,fresh?'No qualifying fresh PvP reports in this scope. Try a longer event age or another space filter; new signals appear as reports arrive.':'No multi-kill engagements observed in this history window.');
 (data.items||[]).forEach(b=>{
  const row=node('article',null,'panel battle-row');row.dataset.signal=b.signal||'';row.dataset.space=b.space_type||'unknown';
  const main=node('div',null,'battle-row-main');
  const identity=node('div',null,'battle-row-identity');identity.append(node('strong',b.system_name||'System name unavailable'));
  const tags=node('div',null,'battle-tags');tags.append(spaceBadge(b),node('span',fresh?b.status_label:'Observed engagement','badge signal-'+(fresh?b.signal:'history')));identity.append(tags);main.append(identity);
  const timing=node('div',null,'battle-row-timing');timing.append(node('strong',`Latest loss ${age(b.latest_event_age_seconds)} ago`),node('span',`Received ${age(b.latest_received_age_seconds)} ago`));main.append(timing);
  const stats=node('dl',null,'battle-row-stats');
  for(const [label,value,title] of [['Losses',number(b.kills),`${number(b.fresh_kills)} event + report within ${data.fresh_minutes}m`],['Pilots',number(b.observed_pilots),'Identified in this group'],['Value',isk(b.reported_value_isk),`${number(b.value_known_kills)} valued killmails`]]){
   const item=node('div');item.title=title;item.append(node('dt',label),node('dd',value));stats.append(item);
  }
  main.append(stats);
  const actions=node('div',null,'context-actions battle-row-actions');
  if(state.mode!=='demo'&&b.battle_report_url)actions.append(externalLink('WarBeacon ↗',b.battle_report_url));
  if(state.mode!=='demo'&&b.eve_tools_url)actions.append(externalLink('EVE Tools ↗',b.eve_tools_url));
  if(fresh&&state.mode!=='demo'&&b.evidence?.[0]&&!b.synthetic){const latest=evidenceLink(b.evidence[0]);latest.textContent='Latest loss ↗';latest.classList.add('context-link');actions.append(latest);}
  const ask=node('button','Ask analyst','copy-action');ask.type='button';ask.setAttribute('aria-label',`Ask analyst about ${b.system_name||'this system'}`);ask.addEventListener('click',()=>openSystemAnalyst(b.system_id,b.system_name));actions.append(ask);main.append(actions);row.append(main);
  const detail=node('details',null,'battle-row-details');detail.dataset.key=`${b.system_id}:${b.start}`;detail.open=expanded.has(detail.dataset.key);detail.append(node('summary','Details & evidence'));
  const facts=node('div',null,'battle-row-facts');facts.append(node('span',`Recorded window: ${date(b.start)} → ${date(b.end)}`),node('span',`First recorded loss ${age(b.first_event_age_seconds)} ago`),node('span',`Latest loss report delay: ${age(b.report_delay_seconds)}`));detail.append(facts);
  if(fresh&&b.signal==='early_signal')detail.append(node('p','One loss is an early signal. More evidence is needed to establish a multi-kill engagement.','footnote'));
  if(state.mode!=='demo'&&!b.synthetic){const links=systemCell(b.system_id,b.system_name);links.querySelector('button[aria-label^="Ask analyst"]')?.remove();detail.append(links);}
  const ships=node('div',null,'table-wrap');table(ships,['Lost ship','Losses'],(b.ships||[]).map(x=>[x.ship_name,number(x.losses)]));detail.append(ships);evidence(detail,b.evidence);if(b.kills>12)detail.append(node('p',`Showing up to 12 of ${number(b.kills)} locally observed killmails${fresh?', newest first':''}. External reports may include different evidence.`,'footnote'));row.append(detail);root.append(row);
 });
 limitations('battle-limitations',data.limitations);
}

function filteredSystems(items){
 const q=($('system-search')?.value||'').trim().toLowerCase(),sort=$('system-sort')?.value||'kills';
 return items.filter(i=>String(i.system_name).toLowerCase().includes(q)).sort((a,b)=>sort==='name'?a.system_name.localeCompare(b.system_name):sort==='npc'?(b.npc_kills??-1)-(a.npc_kills??-1):b.reported_killmails-a.reported_killmails);
}
async function load(){state.lastRefreshAttempt=Date.now();const generation=++state.generation;const view=state.view;$('refresh').disabled=true;error('');$(view).classList.add('loading');$(view).setAttribute('aria-busy','true');try{if(view==='npc'){status().catch(()=>{});await loadNpc(generation);return;}await status();let data;if(view==='battles')data=await api('/api/battles?'+new URLSearchParams({hours:$('battle-hours').value,mode:$('battle-mode').value,fresh_minutes:$('battle-fresh-minutes').value,...($('battle-space').value?{space:$('battle-space').value}:{})}));else if(view==='system-activity')data=await api(`/api/system-activity?hours=${$('activity-hours').value}`);else if(view==='overview')data=await api(`/api/overview?days=${state.days}`);else if(view==='doctrines')data=await api(`/api/doctrines?days=${state.days}`);else if(view==='forecasts'){const [forecast,training]=await Promise.all([api('/api/forecast?days=90'),api('/api/training-scope?mode='+encodeURIComponent($('training-mode').value))]);data={...forecast,training};}else if(view==='profiles'&&state.profile)data=await api(`/api/profile/${state.profile.kind}/${state.profile.id}?days=${state.days}`);if(generation!==state.generation)return;if(data)({battles,'system-activity':systemActivity,overview,doctrines,forecasts,profiles:profile})[view](data);}catch(err){if(generation===state.generation)error(err.message);}finally{$(view).classList.remove('loading');$(view).setAttribute('aria-busy','false');if(generation===state.generation)$('refresh').disabled=false;}}
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{state.view=button.dataset.view;try{localStorage.setItem('watcher-view',state.view);}catch{}document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==state.view);document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b===button);if(b===button)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});$('page-title').replaceChildren(document.createTextNode(titles[state.view][0]),node('span','.'));$('page-description').textContent=titles[state.view][1];$('breadcrumb-view').textContent=state.view.toUpperCase();$('analyst-profile').disabled=!state.profile||$('analyst-area').value!=='auto';analystScopeStatus();$('window-control').hidden=['analyst','forecasts','npc','system-activity','battles'].includes(state.view);load();}));
$('refresh').addEventListener('click',load);$('days').addEventListener('change',()=>{state.days=Number($('days').value);load();});
$('profile-form').addEventListener('submit',async event=>{event.preventDefault();const id=Number($('entity-id').value);if(!Number.isSafeInteger(id)||id<=0){error('Enter a valid positive EVE entity ID.');return;}state.profile={kind:$('entity-kind').value,id};await load();});
$('search-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;empty('search-results','Searching cached entities…');try{const data=await api('/api/entities?q='+encodeURIComponent($('entity-search').value.trim()));const results=clear('search-results');if(!data.items?.length)empty(results,'No cached names match. You can still enter an entity ID directly.');(data.items||[]).forEach(item=>{const kind=String(item.category||'').replace(/s$/,'');if(!['character','corporation','alliance'].includes(kind))return;const b=node('button',`${item.name} · ${kind}`,'search-hit');b.type='button';b.addEventListener('click',()=>{$('entity-kind').value=kind;$('entity-id').value=item.id;$('profile-form').requestSubmit();});results.append(b);});}catch(err){empty('search-results',err.message);}finally{button.disabled=false;}});
$('train').addEventListener('click',async()=>{
 if($('train').disabled)return;
 state.trainingBusy=true;trainingSummary();$('training-mode').disabled=true;document.querySelectorAll('#forecast-results input').forEach(el=>el.disabled=true);
 $('train-status').textContent='Training and evaluating selected systems on chronological observations…';
 try{const data=await api('/api/train?mode='+encodeURIComponent($('training-mode').value),{system_ids:[...state.selectedSystems.keys()]});$('train-status').textContent=data.message||data.status;await load();}
 catch(err){$('train-status').textContent=err.message;}
 finally{state.trainingBusy=false;$('training-mode').disabled=false;document.querySelectorAll('#forecast-results input').forEach(el=>el.disabled=false);trainingSummary();}
});
$('training-mode').addEventListener('change',()=>{state.trainingPreview=null;trainingSummary();load();});
$('clear-training').addEventListener('click',()=>{state.selectedSystems.clear();document.querySelectorAll('#forecast-results input').forEach(el=>el.checked=false);document.querySelector('#forecast-results table')?.dispatchEvent(new Event('change'));trainingSummary();});

document.querySelectorAll('[data-question]').forEach(b=>b.addEventListener('click',()=>{$('question').value=b.dataset.question;$('question').focus();}));
$('chat-form').addEventListener('submit',async event=>{event.preventDefault();const question=$('question').value.trim();if(!question)return;const conversation=$('conversation');conversation.querySelector('.empty')?.remove();const user=node('div',null,'message user');user.append(node('strong','YOU','message-label'),document.createTextNode(question));conversation.append(user);const reply=node('div',null,'message');reply.append(node('strong','INTEL ANALYST','message-label'),node('span','Examining the available evidence…'));conversation.append(reply);$('ask').disabled=true;$('question').value='';try{const body={question};
 if($('analyst-area').value!=='auto'){
  let location=state.analystLocation;
  if(!location){await lookupAnalystLocations();location=matchAnalystLocation();}
  if(!location)throw new Error('Choose a matching location from the local suggestions, or switch Area to From question.');
  body.location={kind:location.kind,id:location.id};
 }else if(state.profile&&$('analyst-profile').checked){body.entity_kind=state.profile.kind;body.entity_id=state.profile.id;}
 if(!$('analyst-hours').disabled&&$('analyst-hours').value)body.hours=Number($('analyst-hours').value);
 const data=await api('/api/chat',body);const mode={retrieval:'DATA SUMMARY',local_llm:'LOCAL MODEL',local_agent:'LOCAL AGENT'}[data.mode]||'LOCAL';reply.replaceChildren(node('strong',`INTEL ANALYST · ${mode}`,'message-label'),document.createTextNode(data.answer));if(data.tokens_used||data.tools_used?.length)reply.append(node('p',`${number(data.tokens_used)} inference tokens${data.tools_used?.length?' · Queries: '+data.tools_used.join(', '):''}`,'footnote'));evidence(reply,data.evidence);await status();}catch(err){reply.replaceChildren(node('strong','REQUEST COULD NOT COMPLETE','message-label'),document.createTextNode(err.message));$('question').value=question;}finally{$('ask').disabled=false;conversation.scrollTop=conversation.scrollHeight;}});
try{const saved=JSON.parse(localStorage.getItem('watcher-npc-selection')||'null');if(saved&&typeof saved.auto==='boolean'){state.npcAuto=saved.auto;state.npcSelected=new Set((Array.isArray(saved.ids)?saved.ids:[]).filter(id=>Number.isSafeInteger(id)&&id>0));}}catch{}
$('npc-auto').checked=state.npcAuto;
let rememberedView;try{rememberedView=localStorage.getItem('watcher-view');}catch{}
const rememberedButton=Array.from(document.querySelectorAll('[data-view]')).find(b=>b.dataset.view===rememberedView);
if(rememberedButton&&rememberedView!=='overview')rememberedButton.click();else load();setInterval(()=>{const interval=state.view==='battles'&&$('battle-mode').value==='fresh'?15000:60000;if(!document.hidden&&state.view!=='analyst'&&!state.npcTrainingBusy&&!$('refresh').disabled&&Date.now()-state.lastRefreshAttempt>=interval)load();},1000);

$('npc-auto').addEventListener('change',()=>{state.npcAuto=$('npc-auto').checked;npcSelectionChanged();if(state.npcScope)renderNpcScope(state.npcScope);});
$('npc-system-search').addEventListener('input',()=>{if(state.npcScope)renderNpcScope(state.npcScope);});
$('npc-apply').addEventListener('click',()=>{if(!npcSelectionIssue())load();});
$('npc-train').addEventListener('click',async()=>{
 if(npcSelectionIssue()||state.npcTrainingBusy)return;
 state.npcTrainingBusy=true;state.npcSelectionVersion++;npcSelectionSummary();
 document.querySelectorAll('#npc-scope-results input').forEach(el=>el.disabled=true);
 $('npc-train-status').textContent='Evaluating selected NPC systems against chronological baselines…';
 try{const result=await api('/api/npc/train',npcSelection());const names=new Map((state.npcScope?.items||[]).map(i=>[i.system_id,i.system_name||'System '+i.system_id]));$('npc-train-status').textContent=`${result.message||result.status} Trained on ${number(result.systems?.length)} systems and ${number(result.records)} records: ${(result.systems||[]).map(id=>`${names.get(id)||'System '+id} (${id})`).join(', ')||'none'}. 0 LLM tokens.`;}
 catch(e){$('npc-train-status').textContent=e.message;}
 finally{state.npcTrainingBusy=false;await load();}
});

$('activity-hours').addEventListener('change',load);

$('system-search')?.addEventListener('input',()=>{if(state.activity)systemActivity(state.activity);});
$('system-sort')?.addEventListener('change',()=>{if(state.activity)systemActivity(state.activity);});
$('battle-hours')?.addEventListener('change',load);

function analystScopeStatus(){
 const profileSelected=$('analyst-profile').checked&&!$('analyst-profile').disabled;
 $('analyst-hours').disabled=profileSelected;
 if(profileSelected){$('analyst-scope-status').textContent='Using the loaded entity profile over the last 30 days. Uncheck profile context to ask about an area and choose another time window.';return;}
 const location=state.analystLocation;
 const window=$('analyst-hours').selectedOptions[0].textContent;
 $('analyst-scope-status').textContent=(location?`${location.kind}: ${location.name}. `:$('analyst-area').value==='auto'?'Area comes from your question. ':'Choose a matching cached location. ')+`Evidence window: ${window}. Region and constellation results cover mapped systems only.`;
}
function matchAnalystLocation(){
 const value=$('analyst-location').value.trim();
 const exact=state.analystChoices.get(value);
 if(exact)return exact;
 const matches=[...state.analystChoices.values()].filter(item=>item.name.toLocaleLowerCase()===value.toLocaleLowerCase());
 return matches.length===1?matches[0]:null;
}
async function lookupAnalystLocations(){
 const kind=$('analyst-area').value;
 if(kind==='auto')return;
 const generation=++state.locationGeneration;
 const text=$('analyst-location').value.trim();
 // A selected datalist label includes an ID only to disambiguate equal names.
 const query=state.analystLocation&&text===state.analystLocation.label?state.analystLocation.name:text;
 try{
  const data=await api('/api/analyst/locations?kind='+encodeURIComponent(kind)+'&q='+encodeURIComponent(query.slice(0,100)));
  if(generation!==state.locationGeneration||kind!==$('analyst-area').value)return;
  state.analystChoices=new Map();const options=clear('analyst-location-options');
  for(const item of data.items){const label=`${item.name} · ${item.id}`;state.analystChoices.set(label,{...item,label});const option=node('option');option.value=label;options.append(option);}
  state.analystLocation=matchAnalystLocation();analystScopeStatus();
  if(!data.items.length)$('analyst-scope-status').textContent='No matching cached '+kind+' names. Try a different name or ask about a named system. Geography choices appear as background lookups complete.';
 }catch(err){if(generation===state.locationGeneration)$('analyst-scope-status').textContent=err.message;}
}
function openSystemAnalyst(id,name){
 $('analyst-area').value='system';$('analyst-location').disabled=false;
 const location={kind:'system',id:Number(id),name:name||'System name unavailable',label:`${name||'System name unavailable'} · ${id}`};
 state.locationGeneration++;state.analystLocation=location;state.analystChoices=new Map([[location.label,location]]);$('analyst-location').value=location.label;
 $('analyst-profile').checked=false;$('analyst-profile').disabled=true;
 $('question').value='Who has recorded combat activity in this system?';
 document.querySelector('[data-view="analyst"]').click();analystScopeStatus();$('question').focus();
}
let analystSearchTimer;
$('analyst-area').addEventListener('change',()=>{
 state.locationGeneration++;state.analystLocation=null;state.analystChoices.clear();$('analyst-location').value='';clear('analyst-location-options');
 const automatic=$('analyst-area').value==='auto';$('analyst-location').disabled=automatic;$('analyst-location').placeholder=automatic?'Name an area in your question':'Search cached '+$('analyst-area').value+' names…';
 $('analyst-profile').checked=false;$('analyst-profile').disabled=!automatic||!state.profile;analystScopeStatus();if(!automatic)lookupAnalystLocations();
});
$('analyst-location').addEventListener('input',()=>{state.locationGeneration++;state.analystLocation=matchAnalystLocation();analystScopeStatus();clearTimeout(analystSearchTimer);analystSearchTimer=setTimeout(lookupAnalystLocations,200);});
$('analyst-hours').addEventListener('change',analystScopeStatus);

$('analyst-profile').addEventListener('change',analystScopeStatus);

for(const id of ['battle-mode','battle-fresh-minutes','battle-space'])$(id).addEventListener('change',load);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.view==='battles'&&$('battle-mode').value==='fresh'&&!$('refresh').disabled)load();});
