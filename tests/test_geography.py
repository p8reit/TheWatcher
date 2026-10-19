from contextlib import closing
import time

import httpx
import pytest

from app import geography, store


def setup_db(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'geography.db'))
    monkeypatch.setenv('USER_AGENT', 'TheWatcher geography tests https://example.org')
    monkeypatch.setenv('NAME_RESOLVER_ENABLED', 'true')
    monkeypatch.setenv('GEOGRAPHY_RESOLVER_ENABLED', 'true')
    monkeypatch.setenv('GEOGRAPHY_RESOLVER_ENABLED', 'true')
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '')
    store.init_db()
    with closing(store.connect()) as c, c:
        c.executescript('''CREATE TABLE IF NOT EXISTS geography_systems (
            system_id INTEGER PRIMARY KEY,system_name TEXT NOT NULL,constellation_id INTEGER NOT NULL,
            constellation_name TEXT NOT NULL,region_id INTEGER NOT NULL,region_name TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS geography_queue (
            system_id INTEGER PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS system_security (
            system_id INTEGER PRIMARY KEY,security_status REAL NOT NULL,updated_at TEXT NOT NULL);''')


def resolver(handler, sleep=lambda _: None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return geography.Resolver(client, sleep=sleep)


def seed_system_queue(system_id=21001):
    with closing(store.connect()) as c, c:
        c.execute('INSERT OR IGNORE INTO name_queue(id,category) VALUES (?,?)', (system_id, 'solar_system'))
    geography.discover()


def test_resolves_and_indexes_geography_without_changing_events(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    seed_system_queue()
    before = store.utcnow()
    calls = []

    def respond(req):
        calls.append(req.url.path)
        if req.url.path.endswith('/systems/21001/'):
            return httpx.Response(200, json={'system_id': 21001, 'name': 'Fictional Reach', 'constellation_id': 22001, 'security_status': 0.7})
        if req.url.path.endswith('/constellations/22001/'):
            return httpx.Response(200, json={'constellation_id': 22001, 'name': 'Fictional Span',
                                             'region_id': 23001, 'systems': [21001]})
        if req.url.path.endswith('/regions/23001/'):
            return httpx.Response(200, json={'region_id': 23001, 'name': 'Fictional Expanse',
                                             'constellations': [22001]})
        raise AssertionError(req.url)

    assert resolver(respond).once() == 1
    assert calls == ['/latest/universe/systems/21001/', '/latest/universe/constellations/22001/', '/latest/universe/regions/23001/']
    with closing(store.connect()) as c:
        row = c.execute('SELECT * FROM geography_systems WHERE system_id=21001').fetchone()
        assert tuple(row[:6]) == (21001, 'Fictional Reach', 22001, 'Fictional Span', 23001, 'Fictional Expanse')
        security = c.execute('SELECT security_status FROM system_security WHERE system_id=21001').fetchone()
        assert security[0] == 0.7
        queued = c.execute('SELECT next_attempt FROM geography_queue WHERE system_id=21001').fetchone()
        assert queued['next_attempt'] > time.time() + 2591900
    assert before <= row['updated_at']
    assert geography.locations('fictional', kind='system') == [{'id': 21001, 'name': 'Fictional Reach', 'kind': 'system'}]
    assert geography.locations('fictional') == [
        {'id': 23001, 'name': 'Fictional Expanse', 'kind': 'region'},
        {'id': 21001, 'name': 'Fictional Reach', 'kind': 'system'},
        {'id': 22001, 'name': 'Fictional Span', 'kind': 'constellation'}]


def test_cached_parent_metadata_avoids_repeated_parent_requests(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with closing(store.connect()) as c, c:
        c.execute('''INSERT INTO geography_systems VALUES (21001,'Fictional One',22001,'Fictional Span',23001,'Fictional Expanse',?)''', (store.utcnow(),))
        c.execute('INSERT INTO name_queue(id,category) VALUES (21002,\'system\')')
    calls = []

    def respond(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={'system_id': 21002, 'name': 'Fictional Two', 'constellation_id': 22001, 'security_status': -0.2})

    assert geography.Resolver(httpx.Client(transport=httpx.MockTransport(respond)), sleep=lambda _: None).once() == 1
    assert calls == ['/latest/universe/systems/21002/']


def test_bad_parent_link_and_http_failure_back_off_without_cache(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    seed_system_queue()
    mismatch = resolver(lambda req: httpx.Response(200, json={'system_id': 21001, 'name': 'Fictional Reach', 'constellation_id': 0}))
    assert mismatch.once() == 0
    assert 'ValueError' in geography.status()['last_error']
    with closing(store.connect()) as c, c:
        assert c.execute('SELECT 1 FROM geography_systems').fetchone() is None
        c.execute('UPDATE geography_queue SET next_attempt=0')
    store.set_state('geography_retry_after', 0)
    rate_limited = resolver(lambda req: httpx.Response(429, headers={'Retry-After': '600'}))
    assert rate_limited.once() == 0
    assert store.get_state('geography_retry_after') >= time.time() + 590
    with closing(store.connect()) as c:
        assert c.execute('SELECT attempts FROM geography_queue WHERE system_id=21001').fetchone()[0] == 2


@pytest.mark.parametrize('bad_response', [
    ('system', {'system_id': 21999, 'name': 'Fictional Reach', 'constellation_id': 22001}),
    ('constellation', {'constellation_id': 22999, 'name': 'Fictional Span', 'region_id': 23001, 'systems': [21001]}),
    ('constellation', {'constellation_id': 22001, 'name': 'Fictional Span', 'region_id': 23001, 'systems': [21002]}),
    ('region', {'region_id': 23999, 'name': 'Fictional Expanse', 'constellations': [22001]}),
    ('region', {'region_id': 23001, 'name': 'Fictional Expanse', 'constellations': [22002]}),
])
def test_rejects_wrong_identity_or_child_membership(tmp_path, monkeypatch, bad_response):
    setup_db(tmp_path, monkeypatch)
    seed_system_queue()
    bad_at, body = bad_response

    def respond(req):
        if req.url.path.endswith('/systems/21001/'):
            if bad_at == 'system':
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={'system_id': 21001, 'name': 'Fictional Reach', 'constellation_id': 22001, 'security_status': 0.4})
        if req.url.path.endswith('/constellations/22001/'):
            if bad_at == 'constellation':
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={'constellation_id': 22001, 'name': 'Fictional Span',
                                             'region_id': 23001, 'systems': [21001]})
        if req.url.path.endswith('/regions/23001/'):
            if bad_at == 'region':
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={'region_id': 23001, 'name': 'Fictional Expanse',
                                             'constellations': [22001]})
        raise AssertionError(req.url)

    assert resolver(respond).once() == 0
    assert 'ValueError' in geography.status()['last_error']
    with closing(store.connect()) as c:
        assert c.execute('SELECT 1 FROM geography_systems').fetchone() is None


def test_demo_disables_discovery_and_resolver_requests(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    store.set_state('data_mode', 'demo')
    seed_system_queue()
    assert not geography.enabled()
    assert resolver(lambda req: (_ for _ in ()).throw(AssertionError('network called'))).once() == 0
    with closing(store.connect()) as c:
        assert c.execute('SELECT COUNT(*) FROM geography_queue').fetchone()[0] == 0


def test_worker_uses_cloud_client_settings_and_makes_no_request_when_disabled(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setenv('USER_AGENT', 'TheWatcher worker tests https://example.org')
    monkeypatch.setattr(geography, 'enabled', lambda: False)
    observed = {}

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            assert seconds == 5
            self.stopped = True

    class Client:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            raise AssertionError('disabled worker made an ESI request')

    monkeypatch.setattr(geography.httpx, 'Client', Client)
    geography.worker(Stop())
    assert observed == {'headers': {'User-Agent': 'TheWatcher worker tests https://example.org',
                                    'Accept-Encoding': 'gzip'},
                        'timeout': 10, 'trust_env': True, 'follow_redirects': False}


def test_location_candidates_merge_entities_and_cache_without_duplicates(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with closing(store.connect()) as c, c:
        c.execute('''INSERT INTO geography_systems VALUES (21001,'Fictional Reach',22001,'Fictional Span',23001,'Fictional Expanse',?)''', (store.utcnow(),))
    store.cache_entities([{'id': 21001, 'name': 'Fictional Reach', 'category': 'solar_system'},
                          {'id': 24001, 'name': 'Fictional Sector', 'category': 'region'}])
    values = geography.all_locations()
    assert len([x for x in values if x['kind'] == 'system' and x['id'] == 21001]) == 1
    assert {'id': 24001, 'name': 'Fictional Sector', 'kind': 'region'} in values


def test_unresolved_known_system_is_available_by_id_without_network(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    with closing(store.connect()) as c, c:
        c.execute("INSERT INTO name_queue(id,category) VALUES (25001,'solar_system')")

    def no_network(*args, **kwargs):
        raise AssertionError('local location lookup attempted network access')

    monkeypatch.setattr(geography.httpx, 'Client', no_network)
    assert geography.locations('25001', kind='system') == [
        {'id': 25001, 'name': 'System name unavailable', 'kind': 'system'}]


@pytest.mark.parametrize('security', [None, True, float('nan'), float('inf'), -1.0001, 1.0001, '0.5'])
def test_invalid_security_is_rejected_without_caching_topology_or_metadata(tmp_path, monkeypatch, security):
    setup_db(tmp_path, monkeypatch)
    seed_system_queue()
    body = {'system_id': 21001, 'name': 'Fictional Reach', 'constellation_id': 22001,
            'security_status': security}
    assert resolver(lambda req: httpx.Response(200, json=body)).once() == 0
    with closing(store.connect()) as c:
        assert c.execute('SELECT 1 FROM geography_systems').fetchone() is None
        assert c.execute('SELECT 1 FROM system_security').fetchone() is None


@pytest.mark.parametrize('security,kind,label', [
    (0.45, 'highsec', 'High Sec'), (0.44999, 'lowsec', 'Low Sec'),
    (0.0, 'nullsec', 'Null Sec'), (-0.8, 'nullsec', 'Null Sec')])
def test_space_details_classifies_cached_security_boundaries(tmp_path, monkeypatch, security, kind, label):
    setup_db(tmp_path, monkeypatch)
    with closing(store.connect()) as c, c:
        c.execute('INSERT INTO system_security VALUES (21001,?,?)', (security, store.utcnow()))
    assert geography.space_details(21001) == {
        'security_status': security, 'space_type': kind, 'space_label': label}


def test_space_details_keeps_missing_security_unknown_and_uses_wormhole_id_range(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    assert geography.space_details(21001) == {
        'security_status': None, 'space_type': 'unknown', 'space_label': 'Unknown space'}
    assert geography.space_details(31000000) == {
        'security_status': None, 'space_type': 'wormhole', 'space_label': 'J Space (Wormhole)'}


def test_security_upgrade_migration_queues_missing_metadata_without_resetting_backoff(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    future = time.time() + 3600
    with closing(store.connect()) as c, c:
        c.execute('''INSERT INTO geography_systems VALUES
            (21001,'Fictional One',22001,'Fictional Span',23001,'Fictional Expanse',?)''', (store.utcnow(),))
        c.execute('''INSERT INTO geography_systems VALUES
            (21002,'Fictional Two',22001,'Fictional Span',23001,'Fictional Expanse',?)''', (store.utcnow(),))
        c.execute('INSERT INTO geography_queue VALUES (21001,0,?)', (future,))
        c.execute('INSERT INTO geography_queue VALUES (21002,1,?)', (future,))

    store.init_db()

    with closing(store.connect()) as c:
        rows = {row['system_id']: row for row in c.execute(
            'SELECT system_id,attempts,next_attempt FROM geography_queue WHERE system_id IN (21001,21002)')}
    assert rows[21001]['attempts'] == 0
    assert rows[21001]['next_attempt'] == 0
    assert rows[21002]['attempts'] == 1
    assert rows[21002]['next_attempt'] == future
