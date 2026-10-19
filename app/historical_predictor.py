"""Retrospective event-time training; archive receipt times are never rewritten."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from . import store


def completed_window(now, days):
    with closing(store.connect()) as c:
        keys = [r[0] for r in c.execute("SELECT key FROM state WHERE key LIKE 'archive_%'")]
    dates = set()
    for key in keys:
        try: day = datetime.strptime(key.removeprefix('archive_'), '%Y%m%d').date()
        except ValueError: continue
        if now.date()-timedelta(days=90) <= day < now.date() and store.get_state(key): dates.add(day)
    if not dates: raise ValueError('Import at least 21 completed archive days first: app.collector --backfill-days 42.')
    last = max(dates)
    first = last
    while (last-first).days+1 < days and first-timedelta(days=1) in dates: first -= timedelta(days=1)
    count = (last-first).days+1
    if count < 21: raise ValueError(f'Only {count} consecutive completed archive days available; historical training needs at least 21. Let the archive import finish.')
    return (datetime.combine(first, datetime.min.time(), timezone.utc),
            datetime.combine(last+timedelta(days=1), datetime.min.time(), timezone.utc), count)


def features(counts, cutoff):
    def total(hours): return sum(counts.get(cutoff-timedelta(hours=i),0) for i in range(1,hours+1))
    return [math.sin(cutoff.hour*math.tau/24), math.cos(cutoff.hour*math.tau/24),
            math.sin(cutoff.weekday()*math.tau/7), math.cos(cutoff.weekday()*math.tau/7),
            total(1),total(6),total(24),total(168),
            sum(counts.get(cutoff-timedelta(hours=i),0) for i in range(24,169,24))]


def train(now=None, days=42, system_ids=None):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, log_loss
    import numpy as np
    if not 21 <= days <= 90: raise ValueError('Historical window must be 21–90 days.')
    if store.get_state('data_mode') != 'live': raise ValueError('Historical archive training requires a live database with real killmails.')
    now = now or datetime.now(timezone.utc)
    first,end,archive_days = completed_window(now,days)
    start = first+timedelta(days=7) # real archive history warms up the seven-day features
    split = (start+(end-start)*.75).replace(minute=0,second=0,microsecond=0)
    scope = store.analysis_scope(system_ids)
    scope_clause = " AND json_extract(raw,'$.solar_system_id') IN ("+','.join('?' for _ in scope)+')' if scope else ''
    base = " FROM killmails WHERE COALESCE(json_extract(raw,'$.zkb.npc'),0)=0 AND killmail_time>=? AND killmail_time<?"
    stamp = lambda d:d.strftime('%Y-%m-%dT%H:%M:%S')
    with closing(store.connect()) as c:
        selected = [r[0] for r in c.execute("SELECT json_extract(raw,'$.solar_system_id') AS sid"+base+scope_clause+
            ' GROUP BY sid ORDER BY COUNT(*) DESC,sid LIMIT 12',(stamp(start),stamp(split),*scope))]
        if system_ids is not None and set(selected) != set(system_ids):
            raise ValueError('Each selected system needs non-NPC killmails in the training portion of completed archives.')
        if not selected: raise ValueError('No non-NPC killmails in the training portion of the completed archives and selected scope.')
        # Aggregate all relevant killmails in SQLite instead of truncating the
        # global archive to the newest 50,000 individual records.
        rows = c.execute("SELECT json_extract(raw,'$.solar_system_id') AS sid,substr(killmail_time,1,13) AS hour,COUNT(*) AS n"+base+
            " AND json_extract(raw,'$.solar_system_id') IN ("+','.join('?' for _ in selected)+') GROUP BY sid,hour',
            (stamp(first),stamp(end),*selected)).fetchall()
    grouped = defaultdict(dict)
    for r in rows: grouped[r['sid']][datetime.fromisoformat(r['hour']+':00:00+00:00')] = r['n']
    X,y,baselines,times = [],[],[],[]
    train_hours = int((split-start).total_seconds()/3600)
    for sid in selected:
        counts = grouped[sid]
        rate = (sum(start<=t<split for t in counts)+1)/(train_hours+2)
        cursor=start
        while cursor<end:
            X.append(features(counts,cursor));y.append(int(counts.get(cursor,0)>0))
            baselines.append(rate);times.append(cursor)
            cursor+=timedelta(hours=1)
    if len(y)<500 or sum(y)<30: raise ValueError('Not enough historical activity: need 500 system-hours and 30 positive hours. Import 42 days or expand the system scope.')
    X,y=np.asarray(X),np.asarray(y)
    mask=np.asarray([t<split for t in times])
    if len(set(y[mask]))<2 or len(set(y[~mask]))<2: raise ValueError('Both historical splits need active and inactive hours.')
    pipe=make_pipeline(StandardScaler(),LogisticRegression(max_iter=300,C=.5,random_state=42))
    pipe.fit(X[mask],y[mask])
    pred=pipe.predict_proba(X[~mask])[:,1]
    brier=float(brier_score_loss(y[~mask],pred))
    baseline=float(brier_score_loss(y[~mask],np.asarray(baselines)[~mask]))
    accepted=brier<baseline*.98
    scaler,clf=pipe.steps[0][1],pipe.steps[1][1]
    metrics={'system_ids': selected, 'llm_tokens': 0, 'brier':round(brier,6),'baseline_brier':round(baseline,6),
             'log_loss':round(float(log_loss(y[~mask],pred)),6),
             'train_samples':int(mask.sum()),'test_samples':int((~mask).sum()),
             'archive_days':archive_days,'archive_start':first.isoformat(),'train_end':split.isoformat(),'test_end':end.isoformat(),
             'target':'At least one archived non-NPC killmail in the next clock hour',
             'evaluation_basis':'Retrospective event time; historical publication/receipt delays are unknown',
             'promotion_rule':'Retrospective holdout Brier at least 2% below system-frequency baseline'}
    model={'status':'experimental' if accepted else 'baseline_retained','trained_at':now.isoformat(),
           'training_mode':'historical','data_mode':'live','metrics':metrics,
           'message':('Retrospective holdout passed; live predictive accuracy is unverified.' if accepted else
                      'Historical training completed; model did not beat baseline by 2%. Baseline retained.'),
           'systems':selected,'mean':scaler.mean_.tolist(),'scale':scaler.scale_.tolist(),
           'coef':clf.coef_[0].tolist(),'intercept':float(clf.intercept_[0])}
    json.dumps(model,allow_nan=False)
    store.set_state('activity_historical_model',model)
    return {k:model[k] for k in ('status','trained_at','training_mode','metrics','message')}


def current_features(systems, now):
    """Full scoped rolling counts for deployment; use actual receipt times here."""
    if not systems: return {}
    bounds=[now-timedelta(hours=h) for h in (1,6,24,168)]
    with closing(store.connect()) as c:
        rows=c.execute("""SELECT json_extract(raw,'$.solar_system_id') AS sid,
            SUM(julianday(killmail_time)>=julianday(?)) AS h1,
            SUM(julianday(killmail_time)>=julianday(?)) AS h6,
            SUM(julianday(killmail_time)>=julianday(?)) AS h24,
            COUNT(*) AS h168,
            SUM(strftime('%H',killmail_time)=?) AS same_hour
            FROM killmails WHERE killmail_time>=? AND killmail_time<?
            AND julianday(killmail_time)>=julianday(?) AND julianday(killmail_time)<julianday(?)
            AND julianday(observed_at)<=julianday(?)
            AND COALESCE(json_extract(raw,'$.zkb.npc'),0)=0
            AND json_extract(raw,'$.solar_system_id') IN ("""+','.join('?' for _ in systems)+') GROUP BY sid',
            (*(b.isoformat() for b in bounds[:3]), f'{now.hour:02d}',
             bounds[3].strftime('%Y-%m-%dT%H:%M:%S'),(now+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S'),
             bounds[3].isoformat(),now.isoformat(),now.isoformat(),*systems)).fetchall()
    cyclical=features({},now)[:4]
    result={sid:cyclical+[0,0,0,0,0] for sid in systems}
    for r in rows: result[r['sid']]=cyclical+[r[k] for k in ('h1','h6','h24','h168','same_hour')]
    return result
