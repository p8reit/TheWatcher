"""Coverage for system-scoped training requests and their scope preview."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from app import demo, historical_predictor, predictor, store
from app.main import app
from app.training import scope_preview


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'training-scope.db'))
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '')
    monkeypatch.setenv('LLM_ENABLED', 'false')
    monkeypatch.setenv('COLLECTOR_ENABLED', 'false')
    with TestClient(app) as c:
        yield c


@pytest.fixture
def archive(client):
    """A compact deterministic archive with two eligible systems."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now.replace(hour=0, minute=0, second=0)
    first = end - timedelta(days=42)
    rows = []
    for day in range(42):
        date = first + timedelta(days=day)
        store.set_state('archive_' + date.strftime('%Y%m%d'),
                        {'records': 10, 'completed_at': now.isoformat()})
        for hour in (1, 5, 9, 13, 17):
            for sid in (30000142, 30000144):
                kid = len(rows) + 1
                event_time = (date + timedelta(hours=hour, minutes=15)).isoformat()
                raw = {'killmail_id': kid, 'killmail_time': event_time,
                       'solar_system_id': sid, 'victim': {'ship_type_id': 587},
                       'attackers': [], 'zkb': {'npc': False}}
                rows.append((kid, event_time.replace('+00:00', 'Z'),
                             now.isoformat().replace('+00:00', 'Z'), 'archive', json.dumps(raw)))
    with closing(store.connect()) as conn, conn:
        conn.executemany('INSERT INTO killmails VALUES (?,?,?,?,?)', rows)
    store.set_state('data_mode', 'live')
    return now


def test_live_preview_counts_exact_configured_scope_and_reports_no_llm(client, monkeypatch):
    now = datetime.now(timezone.utc)
    for kid, sid in ((1, 30000142), (2, 30000142), (3, 30000144), (4, 30000146)):
        store.ingest_killmail({'killmail_id': kid,
            'killmail_time': (now - timedelta(hours=1)).isoformat(), 'solar_system_id': sid,
            'victim': {'ship_type_id': 587}, 'attackers': [], 'zkb': {'npc': False}})
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000144,30000142')
    monkeypatch.setattr('app.training.LIMIT', 1)

    result = client.get('/api/training-scope').json()

    assert {item['system_id']: item['records'] for item in result['items']} == {
        30000142: 2, 30000144: 1}
    assert all(item['llm_tokens'] == 0 for item in result['items'])
    assert result['llm_tokens'] == 0
    assert result['record_cap'] == 1
    assert result['max_systems'] == 12
    assert result['unavailable_reason'] is None


def test_training_api_rejects_bad_or_empty_selection(client, monkeypatch):
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142')
    for body in ({'system_ids': []}, {'system_ids': [0]}, {'system_ids': [30000144]},
                 {'system_ids': [True]}):
        assert client.post('/api/train', json=body).status_code in (400, 422)


def test_training_api_passes_narrowed_scope_and_keeps_empty_body_compatible(client, monkeypatch):
    calls = []
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142,30000144')
    monkeypatch.setattr(predictor, 'train', lambda system_ids=None: calls.append(system_ids) or {'status': 'ok'})

    assert client.post('/api/train', json={'system_ids': [30000144]}).status_code == 200
    assert client.post('/api/train', json={}).status_code == 200
    assert calls == [[30000144], None]


def test_live_trainer_uses_only_selected_demo_system(client):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    demo.seed(now)

    result = predictor.train(now=now, system_ids=[930001])

    assert result['metrics']['system_ids'] == [930001]
    assert store.get_state('activity_model')['systems'] == [930001]


def test_live_record_cap_is_applied_after_system_filter(client, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert demo.seed(now) > 500
    monkeypatch.setattr(predictor, 'LIMIT', 500)

    with pytest.raises(ValueError, match='record cap'):
        predictor.train(now=now)
    result = predictor.train(now=now, system_ids=[930001])

    assert result['metrics']['system_ids'] == [930001]


def test_historical_selection_rejects_out_of_config_and_holdout_only_systems(archive, monkeypatch):
    now = archive
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142')
    with pytest.raises(ValueError, match='within ANALYSIS_SYSTEM_IDS'):
        historical_predictor.train(now=now, days=42, system_ids=[30000144])

    # This system has records only after the chronological training split.
    with closing(store.connect()) as conn, conn:
        raw = {'killmail_id': 99999, 'killmail_time': (now.replace(hour=0) - timedelta(days=1, hours=1)).isoformat(),
               'solar_system_id': 30000146, 'victim': {'ship_type_id': 587},
               'attackers': [], 'zkb': {'npc': False}}
        event_time = raw['killmail_time'].replace('+00:00', 'Z')
        conn.execute('INSERT INTO killmails VALUES (?,?,?,?,?)',
                     (99999, event_time, now.isoformat().replace('+00:00', 'Z'), 'archive', json.dumps(raw)))
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142,30000146')
    with pytest.raises(ValueError, match='training portion'):
        historical_predictor.train(now=now, days=42, system_ids=[30000146])


def test_historical_preview_and_training_are_scoped(archive, monkeypatch):
    now = archive
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142,30000144')

    preview = scope_preview('historical', days=42, now=now)
    result = historical_predictor.train(now=now, days=42, system_ids=[30000144])

    assert {item['system_id']: item['records'] for item in preview['items']} == {
        30000142: 210, 30000144: 210}
    assert preview['llm_tokens'] == 0
    assert preview['record_cap'] is None
    assert 'end exclusive' in preview['window']
    assert result['metrics']['system_ids'] == [30000144]
    assert store.get_state('activity_historical_model')['systems'] == [30000144]
