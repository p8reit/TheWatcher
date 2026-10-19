from datetime import datetime, timedelta, timezone

import pytest

from app import analyst_activity, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'activity.db'))
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '')
    store.init_db()
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    with store.connect() as c, c:
        c.execute('''INSERT INTO geography_systems VALUES (?,?,?,?,?,?,?)''',
                  (90000001, 'Sample System', 91000001, 'Sample Constellation', 92000001, 'Sample Region', now.isoformat()))
        c.execute('''INSERT INTO geography_systems VALUES (?,?,?,?,?,?,?)''',
                  (90000002, 'Other Sample System', 91000001, 'Sample Constellation', 92000001, 'Sample Region', now.isoformat()))
    store.cache_entities([
        {'id': 90000001, 'name': 'Sample System', 'category': 'system'},
        {'id': 90000002, 'name': 'Other Sample System', 'category': 'system'},
        {'id': 91000001, 'name': 'Sample Constellation', 'category': 'constellation'},
        {'id': 92000001, 'name': 'Sample Region', 'category': 'region'},
        {'id': 10000001, 'name': 'Sample Pilot One', 'category': 'pilot'},
        {'id': 10000002, 'name': 'Sample Pilot Two', 'category': 'pilot'},
        {'id': 20000001, 'name': 'Sample Corporation', 'category': 'corporation'},
        {'id': 30000001, 'name': 'Sample Alliance', 'category': 'alliance'},
        {'id': 40000001, 'name': 'Sample Ship', 'category': 'ship'},
    ])
    return now


def kill(now, kid, *, sid=90000001, age=1, observed_age=0.5, attackers=None, victim=None):
    payload = {'killmail_id': kid, 'solar_system_id': sid,
               'killmail_time': (now - timedelta(hours=age)).isoformat(),
               'victim': victim or {'character_id': 10000002, 'corporation_id': 20000001,
                                    'alliance_id': 30000001, 'ship_type_id': 40000001},
               'attackers': attackers if attackers is not None else [
                   {'character_id': 10000001, 'corporation_id': 20000001,
                    'alliance_id': 30000001, 'ship_type_id': 40000001} ]}
    store.ingest_killmail(payload, observed_at=(now - timedelta(hours=observed_age)).isoformat())


def test_geography_scope_dedup_event_receipt_and_future_exclusion(db):
    kill(db, 1, attackers=[
        {'character_id': 10000001, 'corporation_id': 20000001, 'alliance_id': 30000001, 'ship_type_id': 40000001},
        {'character_id': 10000002, 'corporation_id': 20000001, 'alliance_id': 30000001, 'ship_type_id': 40000001},
    ])
    kill(db, 2, sid=90000002, age=3)
    kill(db, 3, age=0, observed_age=-1)  # receipt is in the future
    kill(db, 4, age=-1)  # event is in the future
    context, evidence, summary = analyst_activity.retrieve('Who was active in Sample Region in the last 6 hours?', now=db)
    assert context['recorded_killmails'] == 2
    assert context['corporations'][0]['involvement'] == 2
    assert context['corporations'][0]['kills'] == 2
    assert context['corporations'][0]['losses'] == 2
    assert len(evidence) == 2 and 'not a list' in summary
    assert context['last_event_at'] == (db - timedelta(hours=1)).isoformat()
    system, _, _ = analyst_activity.retrieve('Who was active in Sample System?', now=db)
    assert system['recorded_killmails'] == 1


def test_explicit_scope_intersects_configured_scope_and_cap_is_sql_bounded(db, monkeypatch):
    kill(db, 1, sid=90000001)
    kill(db, 2, sid=90000002)
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '90000002')
    context, _, _ = analyst_activity.retrieve('Who is active in Sample Region?', now=db)
    assert context['recorded_killmails'] == 1
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '')
    monkeypatch.setattr(analyst_activity, 'LIMIT', 1)
    with pytest.raises(ValueError, match='cap'):
        analyst_activity.retrieve('Who is active in Sample Region?', now=db)


def test_no_scope_unknown_ambiguous_and_non_activity_routing(db, monkeypatch):
    assert analyst_activity.retrieve('What fits are in Sample System?', now=db) is None
    assert analyst_activity.retrieve('What is the forecast for Sample System?', now=db) is None
    ctx, _, _ = analyst_activity.retrieve('Who is active?', now=db)
    assert not ctx.get('needs_clarification')
    for q in ('Who is active in this system?', 'Who is active in Missing Sample?', 'Who is active in Sample System?'):
        # Explicit location disambiguates via API; this loop checks absent scope is safe where phrase is deictic/unknown.
        ctx, _, _ = analyst_activity.retrieve(q, now=db)
        if 'Sample System?' in q:
            assert not ctx.get('needs_clarification')
        else:
            assert ctx['needs_clarification']
    # The same label on another cached entity must request a choice.
    with store.connect() as c, c:
        c.execute('''INSERT INTO geography_systems VALUES (?,?,?,?,?,?,?)''',
                  (90000003, 'Sample Region', 91000001, 'Sample Constellation', 92000001, 'Sample Region', db.isoformat()))
    ctx, _, _ = analyst_activity.retrieve('Who is active in Sample Region?', now=db)
    assert ctx['needs_clarification']


def test_time_windows_and_region_coverage(db):
    assert analyst_activity.retrieve('Who was active in the last 2 days?', now=db)[0]['hours'] == 48
    assert analyst_activity.retrieve('Who was active today?', now=db)[0]['hours'] == 12
    with pytest.raises(ValueError, match='1 to'):
        analyst_activity.retrieve('Who was active in the last 91 days?', now=db)
    with pytest.raises(ValueError, match='1 to'):
        analyst_activity.retrieve('Who was active?', hours=0, now=db)
    ctx, _, summary = analyst_activity.retrieve('Who is active in Sample Region?', location={'kind': 'region', 'id': 99999999}, now=db)
    assert ctx['needs_clarification'] and 'cached' in summary.lower()


def test_region_mapping_coverage_is_disclosed(db):
    kill(db, 1)
    context, _, _ = analyst_activity.retrieve('Who is active in Sample Region?', now=db)
    assert context['geography_coverage'] == 'partial'
    assert any('mappings may be incomplete' in item for item in context['limitations'])


def test_latest_receipt_is_maximum_and_partial_mapping_caveat_is_in_summary(db):
    kill(db, 1, age=1, observed_age=0.1)
    kill(db, 2, age=3, observed_age=0.01)
    context, _, summary = analyst_activity.retrieve('Who is active in Sample Region?', now=db)
    assert datetime.fromisoformat(context['last_observed_at'].replace('Z', '+00:00')) == db - timedelta(hours=0.01)
    assert 'mappings may be incomplete' in summary


def test_relative_hour_day_and_unscoped_here_clarify(db):
    for question, hours in [('Who was active in the last hour?', 1),
                            ('Who was active over the past day?', 24)]:
        context, _, _ = analyst_activity.retrieve(question, now=db)
        assert context['hours'] == hours
    for question in ('Who is active here?', 'Who is active in this region?'):
        context, _, _ = analyst_activity.retrieve(question, now=db)
        assert context['needs_clarification']


def test_multiple_unrelated_cached_location_names_are_ambiguous(db):
    store.cache_entities([
        {'id': 93000001, 'name': 'Synthetic A', 'category': 'region'},
        {'id': 93000002, 'name': 'Synthetic Longer B', 'category': 'constellation'},
    ])
    context, _, _ = analyst_activity.retrieve(
        'Who is active in Synthetic A and Synthetic Longer B?', now=db)
    assert context['needs_clarification']


def test_npc_killmails_and_noncharacter_actor_details_do_not_count(db):
    excluded = {
        'killmail_id': 1, 'solar_system_id': 90000001,
        'killmail_time': (db - timedelta(hours=1)).isoformat(),
        'victim': {'character_id': 10000002, 'corporation_id': 20000001,
                   'alliance_id': 30000001, 'ship_type_id': 40000001},
        'attackers': [{'character_id': 10000001, 'corporation_id': 20000001,
                       'alliance_id': 30000001, 'ship_type_id': 40000001}],
        'zkb': {'npc': True},
    }
    store.ingest_killmail(excluded, observed_at=(db - timedelta(minutes=30)).isoformat())
    kill(db, 2, attackers=[{'corporation_id': 29999999, 'alliance_id': 39999999, 'ship_type_id': 49999999}])
    context, _, _ = analyst_activity.retrieve('Who is active in Sample System?', now=db)
    assert context['recorded_killmails'] == 1
    assert [x['id'] for x in context['pilots']] == [10000002]
    assert [x['id'] for x in context['corporations']] == [20000001]
    assert [x['id'] for x in context['alliances']] == [30000001]
    assert [x['id'] for x in context['ships']] == [40000001]


def test_selected_system_excluded_by_config_is_explicit(db, monkeypatch):
    monkeypatch.setenv('ANALYSIS_SYSTEM_IDS', '90000002')
    context, _, summary = analyst_activity.retrieve('Who is active?',
        location={'kind': 'system', 'id': 90000001}, now=db)
    assert context['scope_excluded']
    assert 'outside the systems allowed' in summary


def test_temporal_only_in_phrase_does_not_become_unknown_location(db):
    context, _, _ = analyst_activity.retrieve('Who was active in the last 6 hours?', now=db)
    assert not context.get('needs_clarification')
    assert context['hours'] == 6


@pytest.mark.parametrize(('question', 'expected'), [
    ('Who was active in the last 90 minutes?', 1.5),
    ('Who was active in the past 1.5 hours?', 1.5),
    ('Who was active in the last1.5 hours?', 1.5),
    ('Who was active in the last two days?', 48),
    ('Who was active in the past 2 weeks?', 336),
])
def test_supported_natural_duration_forms(db, question, expected):
    context, _, _ = analyst_activity.retrieve(question, now=db)
    assert context['hours'] == expected


@pytest.mark.parametrize('question', [
    'Who was active in the last 2 months?',
    'Who was active in the last 1.5 minutes?',
    'Who was active in the last ninety minutes?',
])
def test_unsupported_or_too_short_duration_is_rejected(db, question):
    with pytest.raises(ValueError):
        analyst_activity.retrieve(question, now=db)
