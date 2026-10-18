import httpx
import pytest
from app import store
from app.collector import Collector, FeedGap


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('USER_AGENT', 'NewEdenIntel test https://example.org')
    store.init_db()


def payload():
    return {'killmail_id': 1, 'killmail_time': store.utcnow(), 'solar_system_id': 30000142,
            'victim': {'ship_type_id': 587}, 'attackers': []}


def client(handler, delays=None):
    return Collector(httpx.Client(transport=httpx.MockTransport(handler)), sleep=(delays.append if delays is not None else lambda _: None))


def test_cursor_survives_replay(db):
    def respond(request):
        return httpx.Response(200, json={'sequence': 10} if request.url.path.endswith('sequence.json') else dict(payload(), sequence=10))
    c = client(respond)
    assert c.once()
    assert store.get_state('r2z2_sequence') == 11
    store.set_state('r2z2_sequence', 10)
    assert c.once()
    assert len(store.list_killmails()) == 1


def test_gap_does_not_skip(db):
    store.set_state('r2z2_sequence', 10)
    def respond(request):
        return httpx.Response(200, json={'sequence': 99}) if request.url.path.endswith('sequence.json') else httpx.Response(404)
    delays = []
    with pytest.raises(FeedGap):
        client(respond, delays).once()
    assert 6 in delays
    assert store.get_state('r2z2_sequence') == 10
    assert 'gap' in store.get_state('collector_last_error')


def test_poll_at_tip_does_not_advance(db):
    store.set_state('r2z2_sequence', 10)
    def respond(request):
        return httpx.Response(200, json={'sequence': 10}) if request.url.path.endswith('sequence.json') else httpx.Response(404)
    assert not client(respond).once()
    assert store.get_state('r2z2_sequence') == 10


def test_bad_data_does_not_advance(db):
    store.set_state('r2z2_sequence', 10)
    with pytest.raises(RuntimeError, match='Upstream error'):
        client(lambda r: httpx.Response(200, json={'error': 'malformed'})).once()
    assert store.get_state('r2z2_sequence') == 10


def test_retry_bounded(db):
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):
        client(respond).request('GET', 'https://example.org')
    assert len(calls) == 4


def test_archive_resume_and_dedupe(db):
    c = client(lambda r: httpx.Response(200, json=[payload()]))
    assert c.backfill(1) == 1
    assert c.backfill(1) == 0
    assert store.get_state('data_mode') == 'live'


def test_missing_archive_fails(db):
    with pytest.raises(FeedGap):
        client(lambda r: httpx.Response(404)).backfill(1)


def test_manual_reset_audits_gap(db):
    store.set_state('r2z2_sequence', 10)
    c = client(lambda r: httpx.Response(200, json={'sequence': 50}))
    gap = c.reset_to_current()
    assert gap['skipped_sequence_start'] == 10
    assert gap['skipped_sequence_end'] == 49
    assert store.get_state('r2z2_sequence') == 50
    assert store.get_state('collector_last_manual_reset') == gap


def test_refresh_archives_refetches_completed_dates(db):
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(200, json=[payload()])
    c = client(respond)
    c.backfill(1)
    c.backfill(1)
    c.backfill(1, refresh=True)
    assert len(calls) == 2
    assert len(store.list_killmails()) == 1


def test_unknown_archive_wrapper_fails(db):
    with pytest.raises(ValueError, match='Unknown archive wrapper'):
        client(lambda r: httpx.Response(200, json={'unknown': [payload()]})).backfill(1)


def test_retry_after_bounded(db):
    delays = []
    c = client(lambda r: httpx.Response(429, headers={'Retry-After': '99999'}), delays)
    with pytest.raises(httpx.HTTPStatusError):
        c.request('GET', 'https://example.org')
    assert max(delays) == 60


def test_backfill_cli_exits_without_live_poll(db, monkeypatch):
    from app import collector
    monkeypatch.setenv('COLLECTOR_ENABLED', 'true')
    monkeypatch.setattr('sys.argv', ['collector', '--backfill-days', '1', '--refresh-archives'])
    calls = []
    fake_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(Collector, '__init__', lambda self: setattr(self, 'client', fake_client))
    monkeypatch.setattr(Collector, 'backfill', lambda self, days, refresh=False: calls.append((days, refresh)) or 0)
    def unexpected(self):
        pytest.fail('Backfill unexpectedly started live polling')
    monkeypatch.setattr(Collector, 'once', unexpected)
    collector.main()
    assert calls == [(1, True)]
