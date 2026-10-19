"""End-to-end analyst routing uses only locally observed, scoped evidence."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import threading

import pytest
from fastapi.testclient import TestClient

from app import chat, geography, names, store
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'analyst.db'))
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '')
    monkeypatch.setenv('LLM_ENABLED', 'false')
    store.init_db()
    now = datetime.now(timezone.utc)
    store.cache_entities([
        {'id': 930001, 'category': 'solar_system', 'name': 'SYNTHETIC Alpha'},
        {'id': 930002, 'category': 'solar_system', 'name': 'SYNTHETIC Beta'},
        {'id': 900001, 'category': 'character', 'name': 'SYNTHETIC Pilot A'},
        {'id': 900002, 'category': 'character', 'name': 'SYNTHETIC Pilot B'},
        {'id': 900003, 'category': 'character', 'name': 'SYNTHETIC Pilot C'},
        {'id': 910001, 'category': 'corporation', 'name': 'SYNTHETIC Corp'},
        {'id': 920001, 'category': 'alliance', 'name': 'SYNTHETIC Alliance'},
        {'id': 940001, 'category': 'inventory_type', 'name': 'SYNTHETIC Hull'},
    ])
    for n, sid in enumerate((930001, 930002), 1):
        store.ingest_killmail({
            'killmail_id': 800000000+n, 'solar_system_id': sid,
            'killmail_time': (now-timedelta(hours=1)).isoformat(),
            'victim': {'character_id': 900001, 'ship_type_id': 940001},
            'attackers': [{'character_id': 900001+n, 'corporation_id': 910001,
                           'alliance_id': 920001, 'ship_type_id': 940001}],
            'zkb': {'npc': False, 'synthetic': True}}, source='demo')
    with closing(store.connect()) as conn, conn:
        conn.executemany('INSERT INTO geography_systems VALUES (?,?,?,?,?,?,?)', [
            (930001, 'SYNTHETIC Alpha', 970001, 'SYNTHETIC Constellation A', 960001, 'SYNTHETIC Region A', now.isoformat()),
            (930002, 'SYNTHETIC Beta', 970002, 'SYNTHETIC Constellation B', 960002, 'SYNTHETIC Region B', now.isoformat())])
    with TestClient(app) as test_client:
        yield test_client


def test_named_system_question_returns_pilots_from_that_area_without_network(client, monkeypatch):
    monkeypatch.setattr(chat.httpx, 'Client', lambda **kwargs: pytest.fail('Question made an upstream request'))
    response = client.post('/api/chat', json={'question': 'Who was active in SYNTHETIC Alpha in the last 6 hours?'})
    assert response.status_code == 200
    result = response.json()
    assert result['mode'] == 'retrieval' and result['tokens_used'] == 0
    assert 'SYNTHETIC DEMO' in result['answer'] and 'SYNTHETIC Pilot B' in result['answer']
    assert 'SYNTHETIC Pilot C' not in result['answer']
    assert '6 hours' in result['answer']
    assert len(result['evidence']) == 1 and result['evidence'][0]['url'] is None


def test_explicit_region_and_hours_override_question_and_profile(client):
    response = client.post('/api/chat', json={
        'question': 'Who is active in SYNTHETIC Beta in the last 24 hours?',
        'location': {'kind': 'region', 'id': 960001}, 'hours': 6,
        'entity_kind': 'character', 'entity_id': 900003})
    assert response.status_code == 200
    text = response.json()['answer']
    assert 'SYNTHETIC Region A' in text and 'SYNTHETIC Pilot B' in text
    assert 'SYNTHETIC Pilot C' not in text and '6 hours' in text
    assert 'mapped systems only' in text


def test_missing_location_clarifies_without_spending_llm_tokens(client, monkeypatch):
    monkeypatch.setenv('LLM_ENABLED', 'true')
    monkeypatch.setattr(chat.httpx, 'Client', lambda **kwargs: pytest.fail('Clarification called LLM'))
    response = client.post('/api/chat', json={'question': 'Who is active in this region?'})
    assert response.status_code == 200
    assert 'Which system' in response.json()['answer']
    assert response.json()['tokens_used'] == 0
    assert response.json()['evidence'] == []


def test_explicit_profile_context_still_works(client):
    result = client.post('/api/chat', json={'question': 'Summarize recorded activity',
                                          'entity_kind': 'character', 'entity_id': 900003}).json()
    assert 'SYNTHETIC Pilot C: 1 recorded kills' in result['answer']
    response = client.post('/api/chat', json={'question': 'Summarize activity', 'entity_id': 900003, 'hours': 6})
    assert response.status_code == 400 and '30 days' in response.json()['detail']


def test_location_lookup_is_cached_and_scope_input_is_validated(client):
    result = client.get('/api/analyst/locations?kind=region&q=region%20a').json()
    assert result['items'] == [{'id': 960001, 'kind': 'region', 'name': 'SYNTHETIC Region A'}]
    for extra in ({'hours': 0}, {'hours': 2161}, {'hours': True},
                  {'location': {'kind': 'region', 'id': 0}},
                  {'location': {'kind': 'planet', 'id': 1}}):
        assert client.post('/api/chat', json={'question': 'Who was active?', **extra}).status_code == 422


def test_unresolved_known_system_can_be_asked_by_explicit_id(client):
    with closing(store.connect()) as conn, conn:
        store.queue_names(conn, {930001: 'system'})
        conn.execute('DELETE FROM entities WHERE id=930001')
        conn.execute('DELETE FROM geography_systems WHERE system_id=930001')
    result = client.post('/api/chat', json={'question': 'Who is active here?',
                                          'location': {'kind': 'system', 'id': 930001}}).json()
    assert 'SYNTHETIC Pilot B' in result['answer']
    assert 'SYNTHETIC Pilot C' not in result['answer']


def test_web_stops_geography_worker(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'lifecycle.db'))
    monkeypatch.setattr(names, 'enabled', lambda: False)
    monkeypatch.setattr(geography, 'enabled', lambda: True)
    stop, joins = threading.Event(), []
    class Worker:
        def join(self, timeout):
            assert stop.is_set()
            joins.append(timeout)
    monkeypatch.setattr(geography, 'start', lambda: (stop, Worker()))
    with TestClient(app):
        assert not stop.is_set()
    assert joins == [15]


def test_existing_standard_demo_gains_fictional_geography_without_reseeding(client):
    with closing(store.connect()) as conn, conn:
        before = [tuple(row) for row in conn.execute('SELECT * FROM killmails ORDER BY id')]
        conn.execute('DELETE FROM geography_systems')
    store.cache_entities([{'id': 930001, 'name': 'SYNTHETIC System 1', 'category': 'solar_system'}])
    with TestClient(app) as restarted:
        choices = restarted.get('/api/analyst/locations?kind=region').json()['items']
        assert choices == [{'id': 960001, 'name': 'SYNTHETIC Region 1', 'kind': 'region'}]
    with closing(store.connect()) as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM killmails ORDER BY id')] == before
