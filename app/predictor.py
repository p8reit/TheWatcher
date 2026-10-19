"""Chronological activity model with observation-time-safe features and baseline gate."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import math
from . import store
from .analytics import records, dt, name, limitations, LIMIT

FEATURES = ['hour_sin', 'hour_cos', 'weekday_sin', 'weekday_cos', 'known_1h', 'known_6h', 'known_24h', 'known_7d', 'known_same_hour_7d']

def hourly(t):
    return t.replace(minute=0, second=0, microsecond=0)

def features(rows, cutoff):
    # A report received after cutoff cannot affect a simulated prediction.
    known = [dt(k['killmail_time']) for k in rows if dt(k['observed_at']) <= cutoff and dt(k['killmail_time']) < cutoff]
    def count(hours): return sum(cutoff - timedelta(hours=hours) <= t for t in known)
    return [math.sin(cutoff.hour * math.tau / 24), math.cos(cutoff.hour * math.tau / 24),
            math.sin(cutoff.weekday() * math.tau / 7), math.cos(cutoff.weekday() * math.tau / 7),
            count(1), count(6), count(24), count(168),
            sum(cutoff - timedelta(days=7) <= t and t.hour == cutoff.hour for t in known)]

def model_status(mode="live"):
    m = store.get_state('activity_historical_model' if mode=="historical" else 'activity_model', {})
    return {k: m[k] for k in ('status', 'trained_at', 'metrics', 'message', 'training_mode') if k in m} or {'status': 'not_trained', 'metrics': {}}

def train(now=None, system_ids=None):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, log_loss
    import numpy as np
    now = now or datetime.now(timezone.utc)
    raw = records(90, system_ids=system_ids)
    if len(raw) >= LIMIT:
        raise ValueError('Training refused: the analysis record cap was reached. Select fewer systems in Activity forecasts or narrow ANALYSIS_SYSTEM_IDS before training.')
    rows = [k for k in raw if not k.get('zkb', {}).get('npc', False)]
    # Mature labels by one day. This does not guarantee all killmails were reported.
    end = hourly(now - timedelta(days=1))
    if not rows: raise ValueError('Collect combat data before training.')
    first = min(dt(k['observed_at']) for k in rows)
    start = hourly(max(first + timedelta(days=7), end - timedelta(days=42)))
    if (end - start).days < 14:
        raise ValueError('Need at least 21 days of first-seen observation history. A new historical import does not satisfy this requirement.')
    split = hourly(start + (end-start)*0.75)
    # Select systems using training-period observations only, never the holdout.
    counts = Counter(k['solar_system_id'] for k in rows if dt(k['observed_at']) < split and dt(k['killmail_time']) < split)
    selected = [s for s, _ in counts.most_common(12)]
    if system_ids is not None and set(selected) != set(system_ids):
        raise ValueError('Each selected system needs observations in the training period, before the holdout.')
    grouped = {s: [k for k in rows if k['solar_system_id'] == s] for s in selected}
    X, y, baseline, times = [], [], [], []
    for sid, kr in grouped.items():
        hits = Counter(hourly(dt(k['killmail_time'])) for k in kr)
        train_hours = max(1, int((split-start).total_seconds()/3600))
        positives = sum(start <= t < split for t in hits)
        base = (positives + 1) / (train_hours + 2)
        cursor = start
        while cursor < end:
            X.append(features(kr, cursor)); y.append(int(hits[cursor] > 0)); baseline.append(base); times.append(cursor)
            cursor += timedelta(hours=1)
    if len(y) < 500 or sum(y) < 30: raise ValueError('Not enough labeled activity: need 500 system-hours and 30 positive hours.')
    X, y = np.asarray(X), np.asarray(y)
    mask = np.asarray([t < split for t in times])
    if len(set(y[mask])) < 2 or len(set(y[~mask])) < 2: raise ValueError('Both chronological splits need active and inactive hours.')
    pipe = make_pipeline(StandardScaler(), LogisticRegression(max_iter=300, C=0.5, random_state=42))
    pipe.fit(X[mask], y[mask])
    pred = pipe.predict_proba(X[~mask])[:,1]
    bp = np.asarray(baseline)[~mask]
    brier = float(brier_score_loss(y[~mask], pred)); base_brier = float(brier_score_loss(y[~mask], bp))
    accepted = brier < base_brier * 0.98
    metrics = {'system_ids': selected, 'llm_tokens': 0, 'brier': round(brier,6), 'baseline_brier': round(base_brier,6),
               'log_loss': round(float(log_loss(y[~mask], pred)),6), 'train_samples': int(mask.sum()),
               'test_samples': int((~mask).sum()), 'train_end': split.isoformat(), 'test_end': end.isoformat(),
               'target': 'At least one recorded PvP killmail in the next clock hour', 'promotion_rule': 'Holdout Brier at least 2% below system-frequency baseline'}
    scaler, clf = pipe.steps[0][1], pipe.steps[1][1]
    result = {'status': 'experimental' if accepted else 'baseline_retained', 'trained_at': now.isoformat(),
              'metrics': metrics, 'message': 'Chronological holdout passed; experimental and uncalibrated.' if accepted else 'Model did not beat baseline by 2%; baseline remains active.',
              'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(), 'coef': clf.coef_[0].tolist(),
              'intercept': float(clf.intercept_[0]), 'systems': selected, 'features': FEATURES,
              'data_mode': store.get_state('data_mode', 'empty'), 'training_mode':'live'}
    store.set_state('activity_model', result)
    return {k: result[k] for k in ('status','trained_at','metrics','message','training_mode')}

def forecast(days=90, now=None):
    now = now or datetime.now(timezone.utc)
    target = now
    rows = [k for k in records(days) if not k.get('zkb', {}).get('npc', False) and dt(k['killmail_time']) < now]
    bysystem = defaultdict(list)
    for k in rows: bysystem[k['solar_system_id']].append(k)
    model = store.get_state('activity_model', {})
    historical = store.get_state('activity_historical_model', {})
    def fresh(candidate):
        return (candidate.get('status')=='experimental' and candidate.get('data_mode')==store.get_state('data_mode')
                and timedelta(0)<=now-dt(candidate['trained_at'])<timedelta(days=7))
    if not fresh(model) and fresh(historical): model = historical
    summary_mode = 'historical' if model.get('training_mode')=='historical' else 'live'
    if not model and historical: summary_mode='historical'
    historical_features = {}
    if model.get('training_mode')=='historical' and fresh(model):
        from .historical_predictor import current_features
        historical_features = current_features(model['systems'],now)
    items = []
    for sid, kr in bysystem.items():
        first = hourly(min(dt(k['killmail_time']) for k in kr))
        elapsed = max(1, int((hourly(now)-first).total_seconds()/3600))
        closed = [k for k in kr if dt(k['killmail_time']) < hourly(now)]
        occupied = len({hourly(dt(k['killmail_time'])) for k in closed})
        prob = (occupied + 1) / (elapsed + 2)
        method = 'Historical frequency baseline'
        usable = fresh(model) and sid in model.get('systems', [])
        if usable:
            # Rolling sixty-minute horizon; evaluation uses hourly origins.
            f = historical_features[sid] if sid in historical_features else features(kr, now)
            z = model['intercept'] + sum(c*(v-m)/s for c,v,m,s in zip(model['coef'],f,model['mean'],model['scale']))
            prob = 1/(1+math.exp(-max(-40,min(40,z))))
            method = 'Experimental historical model (retrospective)' if model.get('training_mode')=='historical' else 'Experimental logistic model'
        items.append({'system_id': sid, 'system_name': name(sid, 'system'), 'probability': round(prob,4),
                      'expected_kills': round(len(closed)/elapsed,3), 'history_hours': elapsed,
                      'method': method, 'confidence': 'retrospective / live accuracy unverified' if usable and model.get('training_mode')=='historical' else 'limited / uncalibrated', 'target_start': target.isoformat(),
                      'target_end': (target+timedelta(hours=1)).isoformat()})
    return {'items': sorted(items,key=lambda x:x['probability'],reverse=True)[:30], 'model': model_status(summary_mode), 'models': {'live':model_status(), 'historical':model_status('historical')},
            'limitations': limitations(rows)+['Forecasts concern recorded PvP activity, not target availability or safe travel.',
            'Baseline assumes observed event span represents coverage; imports and missing reports bias it.',
            'Probability estimates are not calibrated guarantees. Expected kills is a separate historical hourly average.',
            'Predictions cover the next 60 minutes; holdout evaluation uses predictions at clock-hour boundaries.',
            'Historical models assume past killmails were available by event time; their scores do not validate live prediction accuracy.',
            'Only already-observed systems are ranked. Models expire after 7 days; train manually to refresh.']}
