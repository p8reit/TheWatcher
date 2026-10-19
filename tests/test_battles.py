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


def test_fresh_single_and_forming_signals_have_event_and_receipt_ages():
    put(1, 1)
    item = battles.recent(now=NOW, mode='fresh')['items'][0]
    assert item['signal'] == 'early_signal'
    assert item['status_label'] == 'One recent PvP loss'
    assert item['fresh_kills'] == 1
    assert item['latest_event_at'] == (NOW-timedelta(minutes=1)).isoformat()
    assert item['latest_received_at'] == NOW.isoformat()
    assert item['report_delay_seconds'] == 60
    put(2, 2); put(3, 1)
    item = battles.recent(now=NOW, mode='fresh')['items'][0]
    assert item['kills'] == 3 and item['fresh_kills'] == 3
    assert item['signal'] == 'forming'
    assert 'ongoing' not in item['status_label'].lower()


def test_fresh_keeps_old_cluster_as_recent_combat_but_rejects_stale_event_or_receipt():
    put(1, 20); put(2, 19); put(3, 1)
    item = battles.recent(now=NOW, mode='fresh')['items'][0]
    assert item['kills'] == 3 and item['fresh_kills'] == 1
    assert item['signal'] == 'recent_combat'
    put(4, 10, receipt=NOW)
    put(5, 9, receipt=NOW-timedelta(minutes=9))
    assert battles.recent(now=NOW, mode='fresh')['total_fresh_signals'] == 1


def test_fresh_sources_scope_and_space_filter_precede_display_limit(monkeypatch):
    put(1, 2, sid=30000001, source='archive'); put(2, 1, sid=30000001, source='r2z2')
    put(3, 2, sid=30000002); put(4, 1, sid=30000002)
    monkeypatch.setattr(battles, 'DISPLAY_LIMIT', 1)
    from app import geography
    monkeypatch.setattr(geography, 'space_details', lambda sid: {
        'security_status': .8 if sid == 30000002 else .4,
        'space_type': 'highsec' if sid == 30000002 else 'lowsec',
        'space_label': 'High Sec' if sid == 30000002 else 'Low Sec'})
    result = battles.recent(now=NOW, mode='fresh', space='highsec')
    assert result['total_fresh_signals'] == 1
    assert result['total_windows'] == 1 and result['display_truncated'] is False
    assert result['items'][0]['system_id'] == 30000002
    assert result['items'][0]['space_label'] == 'High Sec'
    assert result['sources'] == {'r2z2': 2}
    monkeypatch.delenv('ANALYSIS_SYSTEM_IDS', raising=False)
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000002')
    assert battles.recent(now=NOW, mode='fresh')['scope'] == [30000002]
    with pytest.raises(ValueError): battles.recent(now=NOW, mode='fresh', space='Jita')
    with pytest.raises(ValueError): battles.recent(now=NOW, mode='other')


def test_fresh_candidate_system_filter_precedes_analysis_cap(monkeypatch):
    put(1, 100, sid=30000001); put(2, 99, sid=30000001); put(3, 98, sid=30000001)
    put(4, 1, sid=30000002)
    monkeypatch.setattr(battles, 'KILL_LIMIT', 1)
    assert battles.recent(now=NOW, mode='fresh')['items'][0]['system_id'] == 30000002
    with pytest.raises(ValueError, match='cap reached'):
        battles.recent(now=NOW, mode='history')


def test_report_delay_is_unknown_when_receipt_predates_event():
    put(1, 2, receipt=NOW)
    put(2, 1, receipt=NOW-timedelta(minutes=2))
    row = battles.recent(now=NOW)['items'][0]
    assert row['latest_received_at'] == NOW.isoformat()
    assert row['report_delay_seconds'] is None


def test_fresh_evidence_preview_starts_with_newest_events():
    for kid, minute in enumerate(range(25, 0, -1), 1):
        put(kid, minute)
    item = battles.recent(now=NOW, mode='fresh')['items'][0]
    assert item['evidence_truncated'] is True
    assert item['evidence'][0]['killmail_id'] == 25
    assert item['evidence'][0]['time'] == (NOW-timedelta(minutes=1)).isoformat()
    assert item['evidence'][-1]['killmail_id'] == 6
    history = battles.recent(now=NOW, mode='history')['items'][0]
    assert history['evidence'][0]['killmail_id'] == 1


def test_fresh_api_contract_and_filter_validation():
    from fastapi.testclient import TestClient
    from app.main import app
    now = datetime.now(timezone.utc)
    store.ingest_killmail({'killmail_id': 9000, 'solar_system_id': 31999999,
        'killmail_time': (now-timedelta(seconds=30)).isoformat(),
        'victim': {'character_id': 900001, 'ship_type_id': 940001},
        'attackers': [{'character_id': 900002}], 'zkb': {'npc': False}})
    with TestClient(app) as client:
        result = client.get('/api/battles?mode=fresh&fresh_minutes=5&space=wormhole&hours=6')
        assert result.status_code == 200
        data = result.json()
        assert data['mode'] == 'fresh' and data['items'][0]['signal'] == 'early_signal'
        assert data['items'][0]['space_type'] == 'wormhole'
        assert data['items'][0]['space_label'] == 'J Space (Wormhole)'
        assert client.get('/api/battles?mode=fresh&space=lowsec').json()['items'] == []
        assert client.get('/api/battles').json()['mode'] == 'history'
        for query in ('mode=invalid', 'fresh_minutes=0', 'fresh_minutes=31', 'space=invalid'):
            assert client.get('/api/battles?'+query).status_code == 422
