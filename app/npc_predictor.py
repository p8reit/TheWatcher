"""Receipt-time-safe NPC count forecasts. No inferred ship types are labels."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math

from . import store

FEATURES = ['latest_count', 'mean_3h', 'mean_6h', 'mean_24h',
            'coverage_3h', 'coverage_6h', 'coverage_24h', 'seasonal_24h',
            'seasonal_available', 'hour_sin', 'hour_cos', 'weekday_sin', 'weekday_cos']
LIMITATIONS = [
    'Predicts the next source-reported hourly NPC count, not a ship identity or current target availability.',
    'Source reporting delays mean the target period may already have begun when its forecast is issued.',
    'Absent systems and reporting gaps are missing observations, never zero kills.',
    'Counts measure reported NPC activity; they do not identify pilots or distinguish ratting methods.',
    'Models expire after seven days; stale system observations older than two hours are not forecast.',
]


def _dt(value):
    t = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('NPC timestamps must include a timezone.')
    return t.astimezone(timezone.utc)


def _now(value):
    return _dt(value) if value is not None else datetime.now(timezone.utc)


def _load(now):
    from . import npc_store
    live = npc_store.observations(days=90, quality='verified')
    demo = npc_store.observations(days=90, quality='synthetic')
    if live and demo:
        raise ValueError('Mixed synthetic and verified NPC history is not eligible for forecasting.')
    mode = 'demo' if store.get_state('npc_data_mode', store.get_state('data_mode', 'empty')) == 'demo' else 'live'
    if demo and mode != 'demo':
        raise ValueError('Synthetic NPC history requires explicit demo data_mode.')
    rows = demo if mode == 'demo' else live
    grouped = defaultdict(list)
    for row in rows:
        if row['quality'] != ('synthetic' if mode == 'demo' else 'verified'):
            raise ValueError('Unverified NPC history cannot be used as training labels.')
        if row['source'] != ('demo' if mode == 'demo' else 'esi'):
            raise ValueError('Only native ESI observations or explicit synthetic demo observations are eligible.')
        end, received = _dt(row['source_end']), _dt(row['observed_at'])
        count = row['npc_kills']
        if received < end or not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError('Invalid NPC count or observation timing.')
        if received <= now and end <= now and received >= now - timedelta(days=90):
            grouped[row['system_id']].append({**row, '_end': end, '_received': received})
    for sid in grouped:
        grouped[sid].sort(key=lambda r: r['_end'])
    return grouped, mode


def features(rows, latest, origin):
    """All historical values must have been received by the simulated origin."""
    origin = _dt(origin)
    end = latest['_end']
    start = end - timedelta(hours=24)
    known = [r for r in rows if r['_received'] <= origin and
             start < r['_end'] <= end]
    values, coverage = [], []
    for hours in (3, 6, 24):
        sample = [r['npc_kills'] for r in known if end - timedelta(hours=hours) < r['_end'] <= end]
        values.append(sum(sample) / len(sample) if sample else latest['npc_kills'])
        coverage.append(min(1.0, len(sample) / hours))
    target = end + timedelta(hours=1)
    seasonal_rows = [r for r in known if abs((r['_end'] - (target - timedelta(days=1))).total_seconds()) <= 300]
    seasonal = seasonal_rows[-1]['npc_kills'] if seasonal_rows else latest['npc_kills']
    hour = target.hour + target.minute / 60
    return ([float(latest['npc_kills']), *values, *coverage, float(seasonal), float(bool(seasonal_rows)),
             math.sin(hour * math.tau / 24), math.cos(hour * math.tau / 24),
             math.sin(target.weekday() * math.tau / 7), math.cos(target.weekday() * math.tau / 7)],
            float(seasonal), bool(seasonal_rows))


def _pairs(rows, now):
    pairs = []
    for latest, label in zip(rows, rows[1:]):
        minutes = (label['_end'] - latest['_end']).total_seconds() / 60
        origin = latest['_received']
        if not 45 <= minutes <= 75:
            continue
        if not origin < label['_end'] or not origin < label['_received'] <= now - timedelta(hours=2):
            continue
        # Labels are exactly the next observed report, never invented empty hours.
        f, seasonal, available = features(rows, latest, origin)
        pairs.append({'x': f, 'y': label['npc_kills'], 'origin': origin,
                      'target_end': label['_end'], 'matured': label['_received'],
                      'persistence': latest['npc_kills'], 'seasonal': seasonal,
                      'seasonal_available': available})
    return pairs


def model_status():
    model = store.get_state('npc_model', {})
    return {key: model[key] for key in ('status', 'trained_at', 'metrics', 'message', 'data_mode') if key in model} or {
        'status': 'not_trained', 'metrics': {}}


def train(now=None):
    import numpy as np
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    now = _now(now)
    grouped, mode = _load(now)
    all_rows = [r for rows in grouped.values() for r in rows]
    if not all_rows:
        raise ValueError('No eligible verified NPC history. Legacy imports are description-only.')
    first = min(r['_received'] for r in all_rows)
    last = max(r['_received'] for r in all_rows)
    if last - first < timedelta(days=21):
        raise ValueError('Need at least 21 days of verified receipt-time NPC history.')
    split = first + (last - first) * .75
    # Select systems using only observations that had arrived before the shared split.
    available = [sid for sid in grouped if any(r['_received'] < split for r in grouped[sid])]
    selected = sorted(available, key=lambda sid: sum(r['_received'] < split for r in grouped[sid]), reverse=True)[:24]
    training, testing = [], []
    for sid in selected:
        for pair in _pairs(grouped[sid], now):
            if pair['target_end'] < split and pair['matured'] < split:
                training.append(pair)
            elif pair['origin'] >= split:
                testing.append(pair)
            # Purge crossing targets and labels unavailable at the split.
    if len(training) < 300 or len(testing) < 100:
        raise ValueError('Need at least 300 training and 100 held-out hourly report pairs without gaps.')
    xtrain = np.asarray([p['x'] for p in training], dtype=float)
    xtest = np.asarray([p['x'] for p in testing], dtype=float)
    ytrain = np.asarray([p['y'] for p in training], dtype=float)
    ytest = np.asarray([p['y'] for p in testing], dtype=float)
    scaler = StandardScaler().fit(xtrain)
    estimator = Ridge(alpha=10.0).fit(scaler.transform(xtrain), ytrain)
    predictions = np.maximum(0, estimator.predict(scaler.transform(xtest)))
    if not np.isfinite(predictions).all():
        raise ValueError('NPC training produced non-finite predictions.')
    mae = float(np.mean(np.abs(ytest - predictions)))
    baseline_mae = float(np.mean(np.abs(ytest - np.asarray([p['persistence'] for p in testing]))))
    baselines = {'persistence': baseline_mae}
    seasonal_coverage = sum(p['seasonal_available'] for p in testing) / len(testing)
    train_seasonal_coverage = sum(p['seasonal_available'] for p in training) / len(training)
    if min(seasonal_coverage, train_seasonal_coverage) >= .8:
        baselines['seasonal_24h'] = float(np.mean(np.abs(ytest - np.asarray([p['seasonal'] for p in testing]))))
    strongest = min(baselines, key=baselines.get)
    accepted = baselines[strongest] > 0 and mae <= baselines[strongest] * .98
    metrics = {'mae': mae, 'baseline_mae': baselines[strongest], 'baseline': strongest,
               'baselines': baselines, 'train_samples': len(training), 'test_samples': len(testing),
               'split_at': split.isoformat(), 'train_last_target_end': max(p['target_end'] for p in training).isoformat(),
               'test_first_origin': min(p['origin'] for p in testing).isoformat(),
               'seasonal_coverage': seasonal_coverage, 'target': 'Next source-reported hourly NPC count',
               'promotion_rule': 'At least 2% lower chronological holdout MAE than the strongest evaluated baseline'}
    model = {'status': 'experimental' if accepted else 'baseline_retained', 'trained_at': now.isoformat(),
             'data_mode': mode, 'systems': selected, 'features': FEATURES, 'metrics': metrics,
             'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
             'coef': estimator.coef_.tolist(), 'intercept': float(estimator.intercept_),
             'message': 'Experimental model passed the holdout gate; estimates remain uncalibrated.' if accepted else
                        'The model did not improve MAE by 2%; the strongest baseline remains active.'}
    json.dumps(model, allow_nan=False)
    store.set_state('npc_model', model)
    return model_status()


def forecast(now=None):
    now = _now(now)
    grouped, mode = _load(now)
    model = store.get_state('npc_model', {})
    valid_model = (model.get('data_mode') == mode and model.get('trained_at') is not None and
                   timedelta(0) <= now - _dt(model['trained_at']) < timedelta(days=7))
    items, stale = [], 0
    for sid, rows in grouped.items():
        latest = rows[-1]
        age = now - latest['_end']
        target_end = latest['_end'] + timedelta(hours=1)
        if age > timedelta(hours=2):
            stale += 1
            continue
        if target_end <= now:
            stale += 1
            continue
        f, seasonal, seasonal_available = features(rows, latest, now)
        estimate, method = float(latest['npc_kills']), 'persistence'
        eligible = valid_model and sid in model.get('systems', [])
        if eligible and model.get('metrics', {}).get('baseline') == 'seasonal_24h' and seasonal_available:
            estimate, method = seasonal, 'seasonal_24h'
        if eligible and model.get('status') == 'experimental':
            estimate = max(0.0, model['intercept'] + sum(c * (v - m) / s for c, v, m, s in
                           zip(model['coef'], f, model['mean'], model['scale'])))
            method = 'experimental_ridge'
        if not math.isfinite(estimate):
            raise ValueError('NPC forecast produced a non-finite estimate.')
        items.append({'system_id': sid, 'system_name': store.entity_name(sid),
                      'expected_npc_kills': round(estimate, 2), 'method': method,
                      'latest_npc_kills': latest['npc_kills'], 'source_end': latest['_end'].isoformat(),
                      'observed_at': latest['_received'].isoformat(), 'age_seconds': int(age.total_seconds()),
                      'report_delay_seconds': int((latest['_received'] - latest['_end']).total_seconds()),
                      'target_start': latest['_end'].isoformat(), 'target_end': target_end.isoformat(),
                      'target_already_started': now > latest['_end'], 'history_observations': len(rows),
                      'coverage_24h': f[6], 'confidence': 'experimental / uncalibrated'})
    return {'items': sorted(items, key=lambda item: item['expected_npc_kills'], reverse=True),
            'model': {**model_status(), 'usable': bool(valid_model)}, 'data_mode': mode,
            'stale_systems': stale, 'limitations': LIMITATIONS}
