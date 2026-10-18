from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import importlib.util
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app import npc_store, npc_import, store
from app.npc_collector import Collector
from app.main import app

@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'npc.db'))
    monkeypatch.setenv('NPC_SYSTEM_IDS','')
    monkeypatch.setenv('NPC_COLLECTOR_ENABLED','false')
    monkeypatch.setenv('LLM_ENABLED','false')
    npc_store.init_db()
    return tmp_path


def sample():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now-timedelta(minutes=10), now, [{'system_id':30000142,'npc_kills':20,'ship_kills':1,'pod_kills':0}]


def test_replay_preserves_receipt_and_conflict_rolls_back(db):
    end,now,rows=sample()
    assert npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),rows)
    assert not npc_store.ingest_snapshot('esi',end.isoformat(),(now+timedelta(seconds=1)).isoformat(),rows)
    with pytest.raises(ValueError,match='Conflicting'):
        npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),[dict(rows[0],npc_kills=99)])
    observations=npc_store.observations()
    assert len(observations)==1 and observations[0]['observed_at']==now.isoformat()
    assert observations[0]['npc_kills']==20


def test_invalid_batch_is_atomic_and_missing_not_zero(db):
    end,now,rows=sample()
    with pytest.raises(ValueError): npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),rows+[dict(rows[0],system_id=2,npc_kills=-1)])
    assert npc_store.observations()==[]
    npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),rows)
    assert {r['system_id'] for r in npc_store.observations()}=={30000142}
    with pytest.raises(ValueError): npc_store.ingest_snapshot('esi',end.isoformat(),end.replace(tzinfo=None).isoformat(),rows)
    with pytest.raises(ValueError): npc_store.ingest_snapshot('esi',now.isoformat(),end.isoformat(),rows)


def test_npc_demo_cannot_mix_with_live_killmails(db):
    end,now,rows=sample()
    npc_store.ingest_snapshot('demo',end.isoformat(),now.isoformat(),rows,quality='synthetic')
    with pytest.raises(ValueError): npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),rows)
    payload={'killmail_id':1,'killmail_time':end.isoformat(),'solar_system_id':30000142,'victim':{'ship_type_id':100},'attackers':[]}
    with pytest.raises(ValueError,match='Synthetic'): store.ingest_killmail(payload)


def test_legacy_import_audits_without_writing_and_remains_ineligible(db):
    end,now,rows=sample()
    path=db/'legacy.jsonl'
    path.write_text('\n'.join(json.dumps({'system_id':30000142+i,'observed_at':now.isoformat(),'npc_kills':20}) for i in range(2)))
    result=npc_import.import_file(path)
    assert not result['applied'] and not result['legacy_training_eligible']
    assert npc_store.status()['sources']==[]
    assert npc_import.import_file(path,True)['inserted_snapshots']==1
    assert npc_store.observations()==[]
    assert len(npc_store.observations(quality='legacy'))==2
    assert npc_import.import_file(path,True)['inserted_snapshots']==0


def test_native_import_preserves_source_and_receipt(db):
    end,now,rows=sample()
    path=db/'native.json'
    path.write_text(json.dumps({'version':1,'source':'esi','quality':'verified','source_end':end.isoformat(),'observed_at':now.isoformat(),'rows':rows}))
    assert npc_import.import_file(path,True)['inserted_snapshots']==1
    assert npc_store.observations()[0]['observed_at']==now.isoformat()
    from app import demo
    with pytest.raises(ValueError,match='live NPC'): demo.seed()
    assert store.get_state('data_mode') is None


def test_collector_commits_before_etag_and_304_no_observation(db,monkeypatch):
    monkeypatch.setenv('NPC_COLLECTOR_ENABLED','true');monkeypatch.setenv('USER_AGENT','Intel (test@example.invalid)')
    end,now,rows=sample(); requests=[]
    def response(req):
        requests.append(req)
        if len(requests)>1:
            assert req.headers['If-None-Match']=='"v1"'
            return httpx.Response(304,headers={'Expires':format_datetime(now+timedelta(hours=1),usegmt=True)})
        return httpx.Response(200,json=rows,headers={'Last-Modified':format_datetime(end,usegmt=True),'ETag':'"v1"'})
    collector=Collector(httpx.Client(transport=httpx.MockTransport(response)))
    collector.poll(now);assert collector.poll(now)==3600
    assert len(npc_store.observations())==1


def test_rejected_snapshot_never_advances_etag_and_retry_after(db,monkeypatch):
    monkeypatch.setenv('NPC_COLLECTOR_ENABLED','true');monkeypatch.setenv('USER_AGENT','Intel (test@example.invalid)')
    collector=Collector(httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=[],headers={'ETag':'bad'}))))
    with pytest.raises(ValueError): collector.poll()
    assert collector.etag is None
    collector.client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'7200'})))
    assert collector.poll()==7200


def test_scope_cap_and_api_boundaries(db,monkeypatch):
    end,now,rows=sample()
    npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),rows+[dict(rows[0],system_id=30000144)])
    with pytest.raises(ValueError,match='cap'): npc_store.observations(limit=1)
    monkeypatch.setenv('NPC_SYSTEM_IDS','30000142')
    assert len(npc_store.observations(limit=1))==1
    with TestClient(app) as c:
        assert c.get('/api/npc/status').json()['sources'][0]['quality']=='verified'
        assert len(c.get('/api/npc/forecast').json()['items'])==1
        assert c.post('/api/npc/train',json={}).status_code==400
        assert c.post('/api/npc/train',json={},headers={'Origin':'https://evil.example'}).status_code==403
        assert 'NPC intelligence' in c.get('/').text
        answer=c.post('/api/chat',json={'question':'Show NPC ratting activity'}).json()
        assert 'NPC source-report' in answer['answer']
