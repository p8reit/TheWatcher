from contextlib import closing
from datetime import datetime,timedelta,timezone
import json
import pytest
from fastapi.testclient import TestClient
from app import store,predictor,historical_predictor as history
from app.main import app


@pytest.fixture
def archive(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'history.db'))
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS','')
    store.init_db()
    now=datetime.now(timezone.utc).replace(microsecond=0)
    end=now.replace(hour=0,minute=0,second=0)
    first=end-timedelta(days=42)
    rows=[]
    for day in range(42):
        date=first+timedelta(days=day)
        store.set_state('archive_'+date.strftime('%Y%m%d'),{'records':10,'completed_at':now.isoformat()})
        for h in (1,5,9,13,17):
            for sid in (30000142,30000144):
                kid=len(rows)+1
                raw={'killmail_id':kid,'killmail_time':(date+timedelta(hours=h,minutes=15)).isoformat(),
                     'solar_system_id':sid,'victim':{'ship_type_id':587},'attackers':[],'zkb':{'npc':False}}
                rows.append((kid,raw['killmail_time'].replace('+00:00','Z'),now.isoformat().replace('+00:00','Z'),'archive',json.dumps(raw)))
    with closing(store.connect()) as c,c: c.executemany('INSERT INTO killmails VALUES (?,?,?,?,?)',rows)
    store.set_state('data_mode','live')
    return now,rows


def test_historical_import_trains_without_falsifying_receipt_or_replacing_live_model(archive):
    now,rows=archive
    with pytest.raises(ValueError,match='first-seen'): predictor.train(now)
    store.set_state('activity_model',{'status':'baseline_retained','message':'Existing live model'})
    live=store.get_state('activity_model')
    result=history.train(now)
    assert result['training_mode']=='historical'
    assert result['status'] in ('experimental','baseline_retained')
    assert result['metrics']['archive_days']==42
    assert result['metrics']['train_samples']==1260
    assert result['metrics']['test_samples']==420
    assert 'publication/receipt delays are unknown' in result['metrics']['evaluation_basis']
    assert store.get_state('activity_model')==live
    with closing(store.connect()) as c: assert [tuple(r) for r in c.execute('SELECT * FROM killmails ORDER BY id')]==rows


def test_historical_features_exclude_target_hour_and_future_events():
    cutoff=datetime(2026,10,1,12,tzinfo=timezone.utc)
    counts={cutoff-timedelta(hours=1):2,cutoff-timedelta(hours=24):3,cutoff:999,cutoff+timedelta(hours=1):999}
    assert history.features(counts,cutoff)[4:]==[2,2,5,5,3]


def test_selection_ignores_holdout_only_system_and_scope_applies(archive,monkeypatch):
    now,rows=archive
    with closing(store.connect()) as c,c:
        raw=json.loads(rows[-1][-1]);raw['solar_system_id']=30000146
        c.execute('INSERT INTO killmails VALUES (?,?,?,?,?)',(9999,*rows[-1][1:4],json.dumps(raw)))
    history.train(now)
    assert 30000146 not in store.get_state('activity_historical_model')['systems']
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS','30000142')
    history.train(now)
    assert store.get_state('activity_historical_model')['systems']==[30000142]


def test_only_completed_contiguous_archives_count(archive):
    now,_=archive
    yesterday=now.date()-timedelta(days=1)
    # A gap yesterday splits a short, recent run from older completed history.
    key='archive_'+(yesterday-timedelta(days=10)).strftime('%Y%m%d')
    with closing(store.connect()) as c,c: c.execute('DELETE FROM state WHERE key=?',(key,))
    with pytest.raises(ValueError,match='consecutive completed'): history.train(now)


def test_historical_training_does_not_use_display_record_cap(archive,monkeypatch):
    now,rows=archive
    monkeypatch.setattr(predictor,'LIMIT',1)
    # More than 50,000 real rows may occupy a single source hour; no list cap.
    with closing(store.connect()) as c,c:
        c.executemany('INSERT INTO killmails VALUES (?,?,?,?,?)',
            [(i,*rows[100][1:4],json.dumps(dict(json.loads(rows[100][-1]),killmail_id=i))) for i in range(10000,60500)])
    assert history.train(now)['metrics']['archive_days']==42


def test_live_deployment_features_keep_receipt_time_filter_and_ignore_npc(archive):
    now,_=archive
    cutoff=now.replace(minute=30)
    def add(kid,offset,received,npc=False):
        when=cutoff-timedelta(minutes=offset)
        raw={'killmail_id':kid,'killmail_time':when.isoformat(),'solar_system_id':30000142,
             'victim':{'ship_type_id':587},'attackers':[],'zkb':{'npc':npc}}
        store.ingest_killmail(raw,observed_at=received.isoformat())
    add(10000,10,cutoff-timedelta(minutes=1))
    add(10001,10,cutoff+timedelta(minutes=1))
    add(10002,-10,cutoff-timedelta(minutes=1))
    add(10003,10,cutoff-timedelta(minutes=1),True)
    assert history.current_features([30000142],cutoff)[30000142][4]==1


def test_historical_api_mode_is_explicit_and_validated(archive):
    with TestClient(app) as client:
        assert client.post('/api/train',json={}).status_code==400
        assert client.post('/api/train?mode=anything',json={}).status_code==422
        assert client.post('/api/train?mode=historical&days=20',json={}).status_code==422
        response=client.post('/api/train?mode=historical&days=42',json={})
        assert response.status_code==200,response.text
        assert response.json()['training_mode']=='historical'
        data=client.get('/api/forecast').json()
        assert data['model']['training_mode']=='historical'
        assert data['models']['live']['status']=='not_trained'
        assert any('Historical models assume' in x for x in data['limitations'])


def test_21_completed_days_are_sufficient_with_enough_system_hours(archive):
    now,_=archive
    assert history.train(now,days=21)['metrics']['archive_days']==21


def test_historical_forecasts_are_labeled_and_live_model_takes_precedence(archive):
    now,_=archive
    model={'status':'experimental','training_mode':'historical','data_mode':'live','trained_at':now.isoformat(),
           'systems':[30000142],'mean':[0]*9,'scale':[1]*9,'coef':[0]*9,'intercept':0,'metrics':{},'message':'Retrospective only'}
    store.set_state('activity_historical_model',model)
    def item(): return next(i for i in predictor.forecast(now=now)['items'] if i['system_id']==30000142)
    assert item()['method']=='Experimental historical model (retrospective)'
    assert 'live accuracy unverified' in item()['confidence']
    store.set_state('activity_model',dict(model,training_mode='live'))
    assert item()['method']=='Experimental logistic model'
    store.set_state('activity_model',dict(model,training_mode='live',trained_at=(now-timedelta(days=8)).isoformat()))
    assert item()['method']=='Experimental historical model (retrospective)'
    store.set_state('activity_historical_model',dict(model,trained_at=(now-timedelta(days=8)).isoformat()))
    assert item()['method']=='Historical frequency baseline'
