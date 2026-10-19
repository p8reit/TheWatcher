import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from app import analytics, chat, predictor, store
from app.main import app

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'test.db'))
    monkeypatch.setenv('LLM_ENABLED','false')
    monkeypatch.setenv('COLLECTOR_ENABLED','false')
    with TestClient(app) as c: yield c

def kill(kid=1, **overrides):
    data={'killmail_id':kid,'killmail_time':store.utcnow(),'solar_system_id':30000142,
          'victim':{'character_id':10,'corporation_id':20,'ship_type_id':100,
                    'items':[{'item_type_id':501,'flag':27,'quantity_destroyed':1},
                             {'item_type_id':999,'flag':5,'quantity_destroyed':100}]},
          'attackers':[{'character_id':11,'corporation_id':21,'ship_type_id':101},
                       {'character_id':12,'corporation_id':21,'ship_type_id':101}],
          'zkb':{'totalValue':10000000,'npc':False}}
    data.update(overrides);return data

def test_empty_dashboard_and_assets(client):
    assert client.get('/api/health').json()['status']=='ok'
    assert client.get('/api/overview').json()['total_kills']==0
    assert client.get('/api/forecast').json()['items']==[]
    assert client.get('/').status_code==200
    assert client.get('/app.js').status_code==200
    assert client.get('/static/app.js').status_code==200
    assert client.get('/static/styles.css').status_code==200
    assert 'frame-ancestors' in client.get('/').headers['content-security-policy']

def test_profile_counts_sides_and_associates(client):
    store.ingest_killmail(kill())
    p=client.get('/api/profile/character/11').json()
    assert (p['kills'],p['losses'])==(1,0)
    assert [x['id'] for x in p['associates']]==[12]
    assert p['evidence'][0]['url']=='https://zkillboard.com/kill/1/'
    v=client.get('/api/profile/character/10').json()
    assert (v['kills'],v['losses'])==(0,1)
    assert v['associates']==[]
    corp=client.get('/api/profile/corporation/21').json()
    assert corp['kills']==1 # two attackers do not double-count the corporation kill

def test_doctrine_excludes_cargo(client):
    store.ingest_killmail(kill())
    result=client.get('/api/doctrines').json()['items'][0]
    assert result['signature']=='501:1'
    assert all('999' not in m for m in result['modules'])

def test_input_and_origin_boundaries(client):
    assert client.get('/api/overview?days=0').status_code==422
    assert client.get('/api/profile/anything/1').status_code==422
    assert client.post('/api/chat',json={'question':'test'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/chat',content='question=x',headers={'Content-Type':'text/plain'}).status_code==415
    assert client.post('/api/chat',json={'question':'x'*10000}).status_code==413
    assert client.get('/api/status',headers={'Host':'evil.example'}).status_code==400

def test_disabled_chat_is_honest_and_never_calls_llm(client,monkeypatch):
    monkeypatch.setattr(httpx.Client,'post',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected network')))
    # TestClient uses its own post; invoke handler implementation to avoid mocking the test transport.
    result=chat.answer('Summarize combat')
    assert result['mode']=='retrieval'
    assert 'disabled' in result['answer']
    assert result['tokens_used']==0

def test_budget_reservations_are_atomic(client,monkeypatch):
    monkeypatch.setenv('LLM_DAILY_TOKEN_BUDGET',str(chat.RESERVATION))
    def reserve():
        try: return chat.reserve()
        except chat.BudgetExceeded: return None
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(lambda _:reserve(),range(4)))
    assert sum(r is not None for r in results)==1
    assert chat.llm_status()['tokens_used']==chat.RESERVATION
    chat.settle(next(r for r in results if r),123)
    assert chat.llm_status()['tokens_used']==123

def test_remote_models_blocked(client,monkeypatch):
    monkeypatch.setenv('OLLAMA_URL','https://api.example.com')
    with pytest.raises(ValueError,match='Remote'): chat.local_url()
    monkeypatch.setenv('OLLAMA_URL','http://127.0.0.1:11434')
    assert chat.local_url()=='http://127.0.0.1:11434'

def test_feature_builder_excludes_late_and_future_events():
    cutoff=datetime(2026,10,1,12,tzinfo=timezone.utc)
    past=(cutoff-timedelta(minutes=30)).isoformat()
    rows=[kill(killmail_time=past,observed_at=(cutoff-timedelta(minutes=10)).isoformat()),
          kill(2,killmail_time=past,observed_at=(cutoff+timedelta(hours=1)).isoformat()),
          kill(3,killmail_time=(cutoff+timedelta(hours=1)).isoformat(),observed_at=past)]
    f=predictor.features(rows,cutoff)
    assert f[4:8]==[1,1,1,1]

def test_new_backfill_cannot_claim_historical_first_seen(client):
    store.ingest_killmail(kill(killmail_time=(datetime.now(timezone.utc)-timedelta(days=60)).isoformat()),source='archive')
    with pytest.raises(ValueError,match='first-seen'): predictor.train()

def test_training_requires_data(client):
    response=client.post('/api/train',json={})
    assert response.status_code==400
    assert 'Collect' in response.json()['detail']

def test_model_request_and_usage(client,monkeypatch):
    monkeypatch.setenv('LLM_ENABLED','true');monkeypatch.setenv('OLLAMA_MODEL','local:test')
    monkeypatch.setenv('LLM_DAILY_TOKEN_BUDGET','20000')
    monkeypatch.setenv('OLLAMA_URL','http://127.0.0.1:11434')
    real_client=httpx.Client
    requests=[]
    def respond(req):
        requests.append(req)
        if req.url.path=='/api/tags': return httpx.Response(200,json={'models':[{'name':'local:test','size':1000}]})
        payload=json.loads(req.content)
        assert payload['stream'] is False and payload['options']['num_predict']==768
        return httpx.Response(200,json={'message':{'content':'No observed combat data.'},'prompt_eval_count':100,'eval_count':10})
    monkeypatch.setattr(chat.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw))
    result=chat.answer('summarize')
    assert result['mode']=='local_llm' and result['tokens_used']==110
    assert chat.llm_status()['tokens_used']==110
    assert len(requests)==2


def test_demo_training_and_evidence(client):
    from app import demo
    assert demo.seed() > 500
    overview = client.get('/api/overview?days=90').json()
    assert overview['data_mode'] == 'demo'
    assert all(k['url'] is None and k['synthetic'] for k in overview['recent'])
    result = predictor.train()
    assert result['status'] in ('experimental', 'baseline_retained')
    assert result['metrics']['test_samples'] > 0
    now = datetime.now(timezone.utc)
    predictions = predictor.forecast(now=now)['items']
    assert len(predictions) == 4
    assert all(0 <= x['probability'] <= 1 for x in predictions)
    assert all(x['target_start'] == now.isoformat() for x in predictions)


def test_analysis_scope_applies_before_limit(client, monkeypatch):
    store.ingest_killmail(kill(1, solar_system_id=30000142))
    store.ingest_killmail(kill(2, solar_system_id=30000144))
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142')
    assert [k['killmail_id'] for k in store.list_killmails(limit=1)] == [1]


def test_model_failure_keeps_budget_reservation(client, monkeypatch):
    monkeypatch.setenv('LLM_ENABLED', 'true')
    monkeypatch.setenv('OLLAMA_MODEL', 'local:test')
    monkeypatch.setenv('OLLAMA_URL', 'http://127.0.0.1:11434')
    monkeypatch.setenv('LLM_DAILY_TOKEN_BUDGET', '20000')
    real_client = httpx.Client
    def respond(req):
        if req.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'local:test', 'size': 1000}]})
        return httpx.Response(500)
    monkeypatch.setattr(chat.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(httpx.HTTPStatusError):
        chat.answer('summarize')
    assert chat.llm_status()['tokens_used'] == chat.RESERVATION


def test_overview_distinguishes_stored_matching_and_analyzed(client, monkeypatch):
    monkeypatch.setattr(analytics, 'LIMIT', 2)
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '30000142')
    old = (datetime.now(timezone.utc)-timedelta(days=60)).isoformat()
    for kid in range(1, 4):
        store.ingest_killmail(kill(kid))
    store.ingest_killmail(kill(4, solar_system_id=30000143))
    store.ingest_killmail(kill(5, killmail_time=old))
    data = client.get('/api/overview?days=7').json()
    assert data['stored_kills'] == 5
    assert data['total_kills'] == 3
    assert data['analyzed_kills'] == 2
    assert data['analysis_limit'] == 2
    assert data['analysis_truncated'] is True
    assert data['scope_filtered'] is True
    assert data['total_value'] == 20000000
    # At exactly the limit, every matching record is analyzed; do not claim truncation.
    monkeypatch.setattr(analytics, 'LIMIT', 3)
    data = client.get('/api/overview?days=7').json()
    assert data['analysis_truncated'] is False
    assert data['analyzed_kills'] == data['total_kills'] == 3


def test_overview_receipt_time_includes_imports_outside_sample(client, monkeypatch):
    monkeypatch.setattr(analytics, 'LIMIT', 1)
    monkeypatch.delenv('ANALYSIS_SYSTEM_IDS', raising=False)
    now = datetime.now(timezone.utc)
    earlier = (now-timedelta(hours=2)).isoformat()
    latest = now.isoformat()
    store.ingest_killmail(kill(1), observed_at=earlier)
    store.ingest_killmail(kill(2, killmail_time=(now-timedelta(days=2)).isoformat()), observed_at=latest)
    data = client.get('/api/overview?days=7').json()
    assert data['total_kills'] == 2 and data['analyzed_kills'] == 1
    assert analytics.dt(data['last_observed_at']) == now
