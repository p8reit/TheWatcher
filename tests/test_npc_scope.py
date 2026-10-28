"""Scope preview and bounded training remain usable above the global NPC cap."""
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import npc_predictor, npc_store, store
from app.main import app

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SYSTEMS = [930001, 930002, 930003]


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'scope.db'))
    monkeypatch.setenv('NPC_SYSTEM_IDS', '')
    monkeypatch.setenv('LLM_ENABLED', 'false')
    monkeypatch.setenv('NPC_COLLECTOR_ENABLED', 'false')
    npc_store.init_db()
    store.set_state('npc_data_mode', 'live')
    store.cache_entities([{'id': sid, 'category': 'solar_system', 'name': f'TEST System {sid}'} for sid in SYSTEMS])
    with closing(store.connect()) as conn, conn:
        for index in range(600):
            end = NOW - timedelta(hours=599-index, minutes=20)
            received = end + timedelta(minutes=5)
            batch = conn.execute('INSERT INTO npc_batches(source,source_end,observed_at,quality,metadata) VALUES (?,?,?,?,?)',
                                 ('esi', end.isoformat(), received.isoformat(), 'verified', '{}')).lastrowid
            conn.executemany('INSERT INTO npc_observations VALUES (?,?,?,?,?)',
                             [(batch, sid, 100+(index*31 % 70), 0, 0) for sid in SYSTEMS])
    monkeypatch.setattr(npc_store, 'MAX_ROWS', 1200)
    return monkeypatch


def test_preview_counts_all_systems_without_loading_capped_rows(history):
    from app import npc_scope
    history.setattr(npc_store, 'observations', lambda **kwargs: pytest.fail('Preview loaded raw observations'))
    preview = npc_scope.preview(NOW)
    assert {item['system_id']: item['records'] for item in preview['items']} == dict.fromkeys(SYSTEMS, 600)
    assert all(item['history_ready'] for item in preview['items'])
    assert preview['max_records'] == 1200
    assert preview['auto_system_ids'] == SYSTEMS[:2]
    assert preview['auto_records'] == 1200
    assert all(item['system_name'] for item in preview['items'])


def test_manual_scope_applied_before_cap_and_auto_keeps_whole_histories(history):
    result = npc_predictor.forecast(NOW, system_ids=[SYSTEMS[2]])
    assert [item['system_id'] for item in result['items']] == [SYSTEMS[2]]
    assert result['items'][0]['history_observations'] == 600
    automatic = npc_predictor.forecast(NOW, auto_select=True)
    assert {item['system_id'] for item in automatic['items']} == set(SYSTEMS[:2])
    assert all(item['history_observations'] == 600 for item in automatic['items'])
    with pytest.raises(ValueError, match='cap|limit|records'):
        npc_predictor.forecast(NOW, system_ids=SYSTEMS)


def test_training_selection_preserved_with_chronological_evaluation(history):
    npc_predictor.train(NOW, system_ids=[SYSTEMS[2]])
    model = store.get_state('npc_model')
    assert model['systems'] == [SYSTEMS[2]]
    assert model['metrics']['train_last_target_end'] < model['metrics']['split_at'] <= model['metrics']['test_first_origin']
    npc_predictor.train(NOW, auto_select=True)
    assert store.get_state('npc_model')['systems'] == SYSTEMS[:2]


def test_configured_scope_cannot_be_broadened_and_reads_do_not_mutate_collector(history):
    from app import npc_scope
    history.setenv('NPC_SYSTEM_IDS', str(SYSTEMS[1]))
    assert [row['system_id'] for row in npc_scope.preview(NOW)['items']] == [SYSTEMS[1]]
    with pytest.raises(ValueError):
        npc_predictor.forecast(NOW, system_ids=[SYSTEMS[0]])
    import os
    assert os.getenv('NPC_SYSTEM_IDS') == str(SYSTEMS[1])
    assert store.get_state('npc_model') is None


def test_preview_excludes_future_reports_and_legacy_records(history):
    from app import npc_scope
    with closing(store.connect()) as conn, conn:
        for source, quality, end in [('esi', 'verified', NOW+timedelta(hours=1)),
                                     ('hunter_legacy', 'legacy', NOW-timedelta(minutes=5))]:
            batch = conn.execute('INSERT INTO npc_batches(source,source_end,observed_at,quality,metadata) VALUES (?,?,?,?,?)',
                                 (source, end.isoformat(), (end+timedelta(minutes=1)).isoformat(), quality, '{}')).lastrowid
            conn.execute('INSERT INTO npc_observations VALUES (?,?,?,?,?)', (batch, 930099, 999, 0, 0))
    assert {row['system_id'] for row in npc_scope.preview(NOW)['items']} == set(SYSTEMS)


def test_selected_api_validation_and_preview_available_over_cap(history):
    with TestClient(app) as client:
        preview = client.get('/api/npc/training-scope')
        assert preview.status_code == 200
        assert len(preview.json()['items']) == 3
        assert client.get('/api/npc/forecast', params={'auto_select': 'true'}).status_code == 200
        scoped = client.get('/api/npc/forecast', params={'system_ids': str(SYSTEMS[2])})
        assert scoped.status_code == 200
        assert [item['system_id'] for item in scoped.json()['items']] == [SYSTEMS[2]]
        for body in ({'system_ids': []}, {'system_ids': [True]}, {'system_ids': [-1]},
                     {'system_ids': [SYSTEMS[0], SYSTEMS[0]]},
                     {'system_ids': [SYSTEMS[0]], 'auto_select': True},
                     {'system_ids': [930099]}, {'system_ids': list(range(1, 26))},
                     {'auto_select': 'yes'}):
            response = client.post('/api/npc/train', json=body)
            assert response.status_code in (400, 422), (body, response.text)
        assert client.post('/api/npc/train', json={'system_ids': SYSTEMS}).status_code == 400
        assert store.get_state('npc_model') is None


def test_auto_skips_history_that_cannot_fit_instead_of_truncating(history):
    from app import npc_scope
    # First system has 600 records; later systems each have two valid records
    # spanning enough receipt-time history. The small cap must not slice Alpha.
    with closing(store.connect()) as conn, conn:
        conn.execute('DELETE FROM npc_observations WHERE system_id != ? AND batch_id NOT IN (1,600)', (SYSTEMS[0],))
    history.setattr(npc_store, 'MAX_ROWS', 4)
    preview = npc_scope.preview(NOW)
    assert preview['auto_system_ids'] == SYSTEMS[1:]
    assert preview['auto_records'] == 4
    # Sparse rows still do not promise viable training pairs.
    with pytest.raises(ValueError, match='300 training'):
        npc_predictor.train(NOW, auto_select=True)
    assert store.get_state('npc_model') is None


def test_immature_history_remains_visible_and_can_be_forecast_manually(history):
    from app import npc_scope
    with closing(store.connect()) as conn, conn:
        conn.execute('DELETE FROM npc_observations WHERE batch_id < 590')
    preview = npc_scope.preview(NOW)
    assert len(preview['items']) == 3 and preview['auto_system_ids'] == []
    assert all(not row['history_ready'] for row in preview['items'])
    assert '21 days' in preview['unavailable_reason']
    assert len(npc_predictor.forecast(NOW, system_ids=[SYSTEMS[0]])['items']) == 1
    with pytest.raises(ValueError, match='21 days'):
        npc_predictor.train(NOW, system_ids=[SYSTEMS[0]])


def test_mixed_native_and_synthetic_history_cannot_be_hidden_by_selection(history):
    from app import npc_scope
    with closing(store.connect()) as conn, conn:
        batch = conn.execute('INSERT INTO npc_batches(source,source_end,observed_at,quality,metadata) VALUES (?,?,?,?,?)',
                             ('demo', (NOW-timedelta(hours=1)).isoformat(), NOW.isoformat(), 'synthetic', '{}')).lastrowid
        conn.execute('INSERT INTO npc_observations VALUES (?,?,?,?,?)', (batch, SYSTEMS[0], 1, 0, 0))
    assert 'Mixed' in npc_scope.preview(NOW)['unavailable_reason']
    with pytest.raises(ValueError, match='Mixed'):
        npc_predictor.forecast(NOW, system_ids=[SYSTEMS[2]])
