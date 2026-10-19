from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app import store, npc_store, names
from app.main import app


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'names.db'))
    monkeypatch.setenv('USER_AGENT','TheWatcher test https://example.org')
    monkeypatch.setenv('NAME_RESOLVER_ENABLED','true')
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS','')
    monkeypatch.setenv('NPC_SYSTEM_IDS','')
    npc_store.init_db()


def kill(kid=1):
    return {'killmail_id':kid,'killmail_time':store.utcnow(),'solar_system_id':30000142,
            'victim':{'character_id':90000001,'corporation_id':98000001,'alliance_id':99000001,'ship_type_id':587,
                      'items':[{'item_type_id':501,'flag':27,'quantity_destroyed':1,'items':[{'item_type_id':502}]}]},
            'attackers':[{'character_id':90000002,'ship_type_id':588}]}


def resolver(handler):
    return names.Resolver(httpx.Client(transport=httpx.MockTransport(handler)),sleep=lambda _:None)


def answer(req):
    return httpx.Response(200,json=[{'id':i,'name':f'Fictional name {i}','category':'inventory_type'} for i in json.loads(req.content)])


def test_ingest_queues_all_entities_and_nested_items_without_changing_observations(db):
    k=kill();store.ingest_killmail(k)
    with closing(store.connect()) as c:
        before=dict(c.execute('SELECT * FROM killmails').fetchone())
        queued={r[0] for r in c.execute('SELECT id FROM name_queue')}
    assert queued=={30000142,90000001,98000001,99000001,587,501,502,90000002,588}
    assert resolver(answer).once()==len(queued)
    assert store.entity_name(587)=='Fictional name 587'
    with closing(store.connect()) as c:
        assert dict(c.execute('SELECT * FROM killmails').fetchone())==before
    assert resolver(lambda r:pytest.fail('Fresh cache was fetched again')).once()==0


def test_existing_npc_and_kill_history_backfills_and_survives_restart(db):
    store.ingest_killmail(kill())
    now=datetime.now(timezone.utc)
    npc_store.ingest_snapshot('esi',(now-timedelta(minutes=10)).isoformat(),now.isoformat(),
        [{'system_id':30000144,'npc_kills':7,'ship_kills':0,'pod_kills':0}])
    with closing(store.connect()) as c,c:
        c.execute('DELETE FROM name_queue') # simulate pre-upgrade database
    names.discover()
    with closing(store.connect()) as c:
        assert c.execute('SELECT 1 FROM name_queue WHERE id=30000144').fetchone()
        assert c.execute('SELECT 1 FROM name_queue WHERE id=502').fetchone()
    assert resolver(answer).once()==10
    assert resolver(lambda r:pytest.fail('Cache did not survive new resolver')).once()==0


def test_poison_id_does_not_block_other_names_and_retries_are_cached(db):
    with closing(store.connect()) as c,c: store.queue_names(c,{1:'pilot',2:'ship',3:'item'})
    def respond(req):
        ids=json.loads(req.content)
        return httpx.Response(404) if 1 in ids else answer(req)
    assert resolver(respond).once()==2
    assert store.entity_name(1,'pilot')=='Pilot name unavailable'
    assert store.entity_name(2)=='Fictional name 2'
    assert resolver(lambda r:pytest.fail('Unavailable ID retried too soon')).once()==0


def test_rate_limit_applies_global_retry_and_keeps_stale_name(db):
    store.cache_entities([{'id':1,'name':'Known fictional pilot','category':'character'}])
    with closing(store.connect()) as c,c:
        store.queue_names(c,{1:'pilot',2:'ship'})
        c.execute('UPDATE name_queue SET next_attempt=0')
    calls=[]
    def respond(req):
        calls.append(1)
        return httpx.Response(429,headers={'Retry-After':'600'})
    r=resolver(respond)
    assert r.once()==0 and len(calls)==1
    assert store.entity_name(1)=='Known fictional pilot'
    assert 'Resolving' in store.entity_name(3,'system') # new UI request cannot bypass pause
    assert r.once()==0 and len(calls)==1
    assert names.status()['last_error']


def test_partial_response_does_not_invent_name_and_malformed_response_not_cached(db):
    with closing(store.connect()) as c,c: store.queue_names(c,{1:'pilot',2:'ship'})
    r=resolver(lambda req:httpx.Response(200,json=[{'id':2,'name':'Fictional ship','category':'inventory_type'}]))
    assert r.once()==1
    assert store.entity_name(1,'pilot')=='Pilot name unavailable'
    assert store.entity_name(2)=='Fictional ship'
    with closing(store.connect()) as c,c: store.queue_names(c,{3:'system'})
    bad=resolver(lambda req:httpx.Response(200,json=[{'id':999,'name':'Wrong result','category':'character'}]))
    assert bad.once()==0
    with closing(store.connect()) as c: assert not c.execute('SELECT 1 FROM entities WHERE id=999').fetchone()


def test_demo_never_looks_up_synthetic_ids(db):
    store.ingest_killmail(kill(),source='demo')
    assert resolver(lambda r:pytest.fail('Demo made upstream request')).once()==0


def test_api_uses_cached_names_and_friendly_pending_labels(db,monkeypatch):
    monkeypatch.setenv('NAME_RESOLVER_ENABLED','false')
    store.ingest_killmail(kill())
    store.cache_entities([{'id':30000142,'name':'Fictional system','category':'solar_system'},
                          {'id':587,'name':'Fictional ship','category':'inventory_type'},
                          {'id':501,'name':'Fictional module','category':'inventory_type'},
                          {'id':90000001,'name':'Fictional pilot','category':'character'}])
    with TestClient(app) as client:
        overview=client.get('/api/overview').json()
        recent=overview['recent'][0]
        assert recent['system_name']=='Fictional system'
        assert recent['victim_name']=='Fictional pilot'
        assert recent['victim_ship']=='Fictional ship'
        profile=client.get('/api/profile/corporation/98000001').json()
        assert profile['name']=='Resolving corporation name…'
        assert '98000001' not in profile['name']
        assert client.get('/api/system-activity').json()['items'][0]['system_name']=='Fictional system'
        assert client.get('/api/doctrines').json()['items'][0]['modules']==['Fictional module ×1']
        assert client.get('/api/status').json()['names']['pending']>0


def test_expired_names_refresh_and_ui_priority_preserves_failure_backoff(db):
    store.cache_entities([{'id':1,'name':'Old fictional name','category':'character'}])
    with closing(store.connect()) as c,c:
        c.execute("UPDATE entities SET updated_at='2020-01-01T00:00:00Z'")
        store.queue_names(c,{1:'pilot'})
    assert resolver(answer).once()==1
    assert store.entity_name(1)=='Fictional name 1'
    store.entity_name(2,'system')
    resolver(lambda req:httpx.Response(503)).once()
    with closing(store.connect()) as c: before=c.execute('SELECT next_attempt FROM name_queue WHERE id=2').fetchone()[0]
    store.entity_name(2,'system')
    with closing(store.connect()) as c: assert c.execute('SELECT next_attempt FROM name_queue WHERE id=2').fetchone()[0]==before


def test_narrator_context_has_labels_without_numeric_entity_identifiers():
    from app.chat import display_context
    original={'id':10,'name':'Fictional pilot','systems':[{'system_id':20,'system_name':'Fictional system','kills':7}],
              'signature':'501:1','evidence':[{'killmail_id':30,'url':'https://example.org'}]}
    clean=display_context(original)
    assert clean=={'name':'Fictional pilot','systems':[{'system_name':'Fictional system','kills':7}],'evidence':[{}]}
    assert original['id']==10 and original['systems'][0]['system_id']==20


def test_web_lifespan_stops_background_worker(db,monkeypatch):
    import threading
    stop=threading.Event()
    joins=[]
    class Thread:
        def join(self,timeout): joins.append(timeout)
    monkeypatch.setattr(names,'start',lambda:(stop,Thread()))
    with TestClient(app): assert not stop.is_set()
    assert stop.is_set() and joins==[15]
