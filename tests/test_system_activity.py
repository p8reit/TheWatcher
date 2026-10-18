from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from app import npc_store, store, system_activity
from app.main import app


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path/'activity.db'))
    monkeypatch.setenv('NPC_SYSTEM_IDS', '')
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '')
    npc_store.init_db()
    return datetime.now(timezone.utc).replace(microsecond=0)-timedelta(minutes=2)


def snapshot(now, hours, sid=90000001, ships=2, source='esi', quality='verified'):
    end=now-timedelta(hours=hours)
    npc_store.ingest_snapshot(source, end.isoformat(), (end+timedelta(minutes=1)).isoformat(),
        [{'system_id':sid,'npc_kills':20,'ship_kills':ships,'pod_kills':1}],quality)


def kill(now, kid, sid=90000001, hours=0.1, receipt=None):
    store.ingest_killmail({'killmail_id':kid,'solar_system_id':sid,
        'killmail_time':(now-timedelta(hours=hours)).isoformat(),
        'victim':{'ship_type_id':100},'attackers':[]},
        observed_at=(receipt or now).isoformat())


def test_window_uses_latest_report_without_summing_and_filters_future_receipts(db):
    snapshot(db, 5, ships=10)
    snapshot(db, .5, ships=2)
    kill(db,1)
    kill(db,2,hours=2)
    kill(db,3,receipt=db+timedelta(minutes=1))
    kill(db,4,hours=-.01)
    one=system_activity.activity(1,now=db)
    six=system_activity.activity(6,now=db)
    assert one['reported_killmails']==1 and six['reported_killmails']==2
    assert one['items'][0]['ship_kills']==six['items'][0]['ship_kills']==2
    assert one['items'][0]['sampled_hours']==1
    assert six['items'][0]['sampled_hours']==2
    assert one['items'][0]['source_age_seconds']==1800
    assert one['items'][0]['report_delay_seconds']==60
    assert one['recent'][0]['killmail_id']==1


def test_stale_missing_and_legacy_exclusion(db):
    snapshot(db,3)
    snapshot(db,.5,sid=90000003,source='hunter_legacy',quality='legacy')
    kill(db,1,sid=90000002)
    data=system_activity.activity(6,now=db)
    by_id={i['system_id']:i for i in data['items']}
    assert set(by_id)=={90000001,90000002}
    assert by_id[90000001]['freshness']=='stale'
    assert by_id[90000002]['freshness']=='missing'
    assert by_id[90000002]['ship_kills'] is None
    assert by_id[90000002]['sampled_hours']==0
    assert system_activity.activity(1,now=db)['items'][0]['system_id']==90000002


def test_scope_intersection_and_caps(db,monkeypatch):
    snapshot(db,.5)
    kill(db,1,sid=90000002)
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS','90000002')
    monkeypatch.setenv('NPC_SYSTEM_IDS','90000001')
    assert system_activity.activity(now=db)['items']==[]
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS','90000001')
    assert len(system_activity.activity(now=db)['items'])==1
    monkeypatch.setattr(system_activity,'REPORT_LIMIT',0)
    with pytest.raises(ValueError,match='cap reached'):
        system_activity.activity(now=db)


def test_api_empty_validation_and_synthetic_isolation(db):
    with TestClient(app) as client:
        assert client.get('/api/system-activity?hours=6').json()['items']==[]
        assert client.get('/api/system-activity?hours=2').status_code==400
        assert client.get('/api/system-activity?hours=no').status_code==422
        snapshot(db,.5,source='demo',quality='synthetic')
        data=client.get('/api/system-activity?hours=1').json()
        assert data['data_mode']=='demo'
        assert data['items'][0]['source']=='demo'
        assert data['items'][0]['quality']=='synthetic'
        assert 'activity-hours' in client.get('/').text
