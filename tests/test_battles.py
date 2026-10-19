from datetime import datetime, timedelta, timezone
import pytest
from app import battles, store

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)

@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'battle.db'))
    monkeypatch.delenv('ANALYSIS_SYSTEM_IDS', raising=False)
    store.init_db()
    store.cache_entities([{'id': 30000001, 'name': 'Test Haven', 'category': 'system'},
                          {'id': 100, 'name': 'Test Frigate', 'category': 'ship'}])


def put(kid, minutes, sid=30000001, npc=False, receipt=None, source='r2z2', value=50):
    store.ingest_killmail({'killmail_id': kid, 'solar_system_id': sid,
        'killmail_time': (NOW-timedelta(minutes=minutes)).isoformat(),
        'victim': {'ship_type_id': 100, 'character_id': 1001},
        'attackers': [{'ship_type_id': 100, 'character_id': 1002}],
        'zkb': {'npc': npc, 'totalValue': value}}, source=source,
        observed_at=(receipt or NOW).isoformat())


def test_cluster_boundaries_and_names():
    for kid, minute in enumerate([200, 170, 139, 138, 137], 1): put(kid, minute)
    rows = battles.recent(now=NOW)['items']
    assert [r['kills'] for r in rows] == [3, 2]
    row = rows[0]
    assert row['system_name'] == 'Test Haven'
    assert row['observed_pilots'] == 2
    assert row['ships'] == [{'ship_type_id': 100, 'ship_name': 'Test Frigate', 'losses': 3}]
    assert row['reported_value_isk'] == 150
    assert row['sources'] == ['r2z2']
    assert row['evidence'][0]['url'] == 'https://zkillboard.com/kill/3/'


def test_span_is_bounded_even_when_gaps_are_short():
    for kid, minute in enumerate(range(200, 19, -20), 1): put(kid, minute)
    rows = battles.recent(now=NOW)['items']
    assert len(rows) == 2
    assert all(datetime.fromisoformat(r['end'])-datetime.fromisoformat(r['start']) <= timedelta(hours=2) for r in rows)


def test_time_receipt_npc_and_scope_filters(monkeypatch):
    put(1, 10); put(2, 9)
    put(3, 8, npc=True); put(4, -1); put(5, 8, receipt=NOW+timedelta(seconds=1))
    put(6, 360); put(7, 361)
    put(8, 10, sid=30000002); put(9, 9, sid=30000002)
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000001')
    result = battles.recent(now=NOW)
    assert result['items'][0]['kills'] == 2
    assert result['total_windows'] == 1
    assert result['scope'] == [30000001]
    assert battles.recent(24, now=NOW)['total_windows'] == 2
    with pytest.raises(ValueError): battles.recent(1, now=NOW)


def test_no_invented_value_or_external_demo_link():
    put(1, 10, source='demo', value=None); put(2, 9, source='demo', value=None)
    row = battles.recent(now=NOW)['items'][0]
    assert row['reported_value_isk'] is None
    assert row['value_known_kills'] == 0
    assert row['battle_report_url'] is None and row['eve_tools_url'] is None
    assert all(e['url'] is None for e in row['evidence'])


def test_report_contract_is_utc_hour_and_epoch_not_raw_date_or_query():
    dt = datetime.fromisoformat('2026-10-19T01:42:59-04:00')
    links = battles.report_links(30000001, dt)
    assert links['battle_report_url'] == 'https://warbeacon.net/br/related/30000001/202610190500/'
    anchor = datetime(2026, 10, 19, 5, tzinfo=timezone.utc)
    assert links['eve_tools_url'] == f'https://br.evetools.org/related/30000001/{int(anchor.timestamp())}/'
    with pytest.raises(ValueError): battles.report_links('30000001/?evil=1', dt)
    with pytest.raises(ValueError): battles.report_links(30000001, datetime(2026, 10, 19))


def test_fractional_now_is_inclusive_and_cap_fails_closed(monkeypatch):
    now = NOW+timedelta(microseconds=500000)
    put(1, 0); put(2, -0.5/60, receipt=now)
    assert battles.recent(now=now)['items'][0]['kills'] == 2
    monkeypatch.setattr(battles, 'KILL_LIMIT', 1)
    with pytest.raises(ValueError, match='cap reached'): battles.recent(now=now)


def test_unidentified_attackers_do_not_establish_pvp_and_synthetic_marker_suppresses_reports():
    put(1, 10); put(2, 9)
    import json
    from contextlib import closing
    with closing(store.connect()) as db, db:
        raw = json.loads(db.execute('SELECT raw FROM killmails WHERE id=2').fetchone()[0])
        raw['attackers'] = [{'ship_type_id': 100}]
        db.execute('UPDATE killmails SET raw=? WHERE id=2', (json.dumps(raw),))
    assert battles.recent(now=NOW)['items'] == []
    put(3, 8)
    with closing(store.connect()) as db, db:
        raw = json.loads(db.execute('SELECT raw FROM killmails WHERE id=3').fetchone()[0])
        raw['zkb']['synthetic'] = True
        db.execute('UPDATE killmails SET raw=? WHERE id=3', (json.dumps(raw),))
    row = battles.recent(now=NOW)['items'][0]
    assert row['synthetic'] is True
    assert row['battle_report_url'] is None and row['eve_tools_url'] is None


def test_api_window_validation():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.get('/api/battles?hours=24')
        assert response.status_code == 200
        assert response.json()['items'] == []
        assert response.json()['hours'] == 24
        assert client.get('/api/battles?hours=1').status_code == 400
        assert client.get('/api/battles?hours=invalid').status_code == 422
