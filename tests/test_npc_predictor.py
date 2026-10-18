from datetime import datetime, timedelta, timezone
import json
import random

import pytest

from app import npc_predictor as predictor, npc_store, store

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


@pytest.fixture
def history(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'npc.db'))
    store.init_db()
    npc_store.init_db()
    data = {'verified': [], 'synthetic': []}
    monkeypatch.setattr(npc_store, 'observations', lambda days=90, quality='verified', **kwargs: data[quality])
    return data


def samples(hours=720, seasonal=False, sid=30000001):
    rng = random.Random(42)
    rows = []
    for index in range(hours):
        end = NOW - timedelta(hours=hours - index - 1, minutes=20)
        count = (index % 24) * 10 if seasonal else max(0, 100 + rng.randint(-70, 70))
        rows.append({'source': 'esi', 'quality': 'verified', 'system_id': sid,
                     'source_end': end.isoformat(), 'observed_at': (end + timedelta(minutes=5)).isoformat(),
                     'npc_kills': count, 'ship_kills': 0, 'pod_kills': 0})
    return rows


def test_real_model_promotes_on_chronological_holdout(history):
    history['verified'] = samples()
    result = predictor.train(NOW)
    assert result['status'] == 'experimental'
    metrics = result['metrics']
    assert metrics['mae'] <= metrics['baseline_mae'] * .98
    assert metrics['train_last_target_end'] < metrics['split_at'] <= metrics['test_first_origin']
    assert metrics['train_samples'] >= 300 and metrics['test_samples'] >= 100
    saved = store.get_state('npc_model')
    json.dumps(saved, allow_nan=False)
    assert len(saved['coef']) == len(predictor.FEATURES)
    forecast = predictor.forecast(NOW)
    assert forecast['items'][0]['method'] == 'experimental_ridge'
    assert forecast['items'][0]['expected_npc_kills'] >= 0


def test_perfect_seasonal_baseline_is_retained(history):
    history['verified'] = samples(seasonal=True)
    result = predictor.train(NOW)
    assert result['status'] == 'baseline_retained'
    assert result['metrics']['baseline'] == 'seasonal_24h'
    assert result['metrics']['baseline_mae'] == 0
    assert predictor.forecast(NOW)['items'][0]['method'] == 'seasonal_24h'


def test_new_import_and_legacy_are_not_training_history(history):
    history['verified'] = samples(120)
    with pytest.raises(ValueError, match='21 days'):
        predictor.train(NOW)
    history['verified'] = []
    with pytest.raises(ValueError, match='Legacy imports'):
        predictor.train(NOW)


def test_mixed_demo_refused_and_demo_requires_explicit_mode(history):
    history['synthetic'] = [{**r, 'source': 'demo', 'quality': 'synthetic'} for r in samples(10)]
    with pytest.raises(ValueError, match='explicit demo'):
        predictor.forecast(NOW)
    store.set_state('npc_data_mode', 'demo')
    assert predictor.forecast(NOW)['data_mode'] == 'demo'
    history['verified'] = samples(10)
    with pytest.raises(ValueError, match='Mixed'):
        predictor.train(NOW)


def test_features_cannot_see_late_or_future_reports(history):
    history['verified'] = samples(48)
    grouped, _ = predictor._load(NOW)
    rows = grouped[30000001]
    latest = rows[-2]
    origin = latest['_received']
    before = predictor.features(rows, latest, origin)
    changed = [{**r, 'npc_kills': 9999999} if r['_received'] > origin else r for r in rows]
    assert before == predictor.features(changed, latest, origin)
    # A past source epoch imported later is unavailable at this historical origin.
    delayed = {**rows[0], 'npc_kills': 9999999, '_received': NOW + timedelta(days=1)}
    assert before == predictor.features(rows + [delayed], latest, origin)


def test_gaps_and_delayed_reports_do_not_make_zero_labels(history):
    history['verified'] = samples(5)
    grouped, _ = predictor._load(NOW + timedelta(hours=3))
    rows = grouped[30000001]
    assert len(predictor._pairs(rows, NOW + timedelta(hours=3))) == 4
    assert len(predictor._pairs([rows[0], rows[2]], NOW + timedelta(hours=3))) == 0
    delayed = {**rows[0], '_received': rows[1]['_end'] + timedelta(minutes=1)}
    assert predictor._pairs([delayed, rows[1]], NOW + timedelta(hours=3)) == []


def test_forecast_freshness_expiry_and_missingness(history):
    history['verified'] = samples(21)[::2]
    result = predictor.forecast(NOW)
    item = result['items'][0]
    assert item['coverage_24h'] < 1
    assert item['report_delay_seconds'] == 300
    assert item['target_already_started']
    assert predictor.forecast(NOW + timedelta(hours=3))['items'] == []
    history['verified'] = samples()
    predictor.train(NOW)
    saved = store.get_state('npc_model')
    saved['trained_at'] = (NOW - timedelta(days=8)).isoformat()
    store.set_state('npc_model', saved)
    expired = predictor.forecast(NOW)
    assert not expired['model']['usable']
    assert expired['items'][0]['method'] == 'persistence'


def test_cap_errors_propagate(history, monkeypatch):
    def capped(**kwargs):
        raise ValueError('NPC observation cap reached')
    monkeypatch.setattr(npc_store, 'observations', capped)
    with pytest.raises(ValueError, match='cap reached'):
        predictor.train(NOW)
