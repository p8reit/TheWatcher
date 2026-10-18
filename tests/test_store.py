import pytest
from app import store
from app.demo import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test.db'))
    store.init_db()


def kill():
    return {'killmail_id': 1, 'killmail_time': store.utcnow(), 'solar_system_id': 30000142,
            'victim': {'character_id': 42, 'ship_type_id': 587}, 'attackers': [], 'zkb': {'totalValue': 10}}


def test_idempotent_receipt_time(db):
    data = kill()
    assert store.ingest_killmail(data, observed_at='2026-01-01T00:00:00Z')
    assert not store.ingest_killmail(data, observed_at='2026-02-01T00:00:00Z')
    rows = store.list_killmails()
    assert len(rows) == 1
    assert rows[0]['observed_at'] == '2026-01-01T00:00:00Z'


def test_live_demo_separation(db):
    store.ingest_killmail(kill())
    with pytest.raises(ValueError, match='cannot share'):
        store.ingest_killmail(kill(), source='demo')
    with pytest.raises(ValueError, match='empty'):
        seed()


def test_demo_and_name_cache(db):
    assert seed() > 500
    assert store.get_state('data_mode') == 'demo'
    assert store.entity_name(900001).startswith('SYNTHETIC')
    with pytest.raises(ValueError):
        store.ingest_killmail(kill())


def test_reject_partial_records(db):
    with pytest.raises(ValueError):
        store.ingest_killmail({'killmail_id': 1})


@pytest.mark.parametrize('change', [
    {'victim': {}},
    {'attackers': [7]},
    {'attackers': [{'character_id': 'invalid'}]},
    {'victim': {'ship_type_id': 587, 'items': [{'item_type_id': 'invalid'}]}},
    {'victim': {'ship_type_id': 587, 'items': [{'item_type_id': 1, 'flag': 'bad'}]}},
])
def test_reject_malformed_participants_and_items(db, change):
    data = kill()
    data.update(change)
    with pytest.raises(ValueError):
        store.ingest_killmail(data)
