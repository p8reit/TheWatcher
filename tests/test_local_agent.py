"""The optional agent can only use local, bounded, scoped evidence."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import agent, chat, store

REAL_HTTPX_CLIENT = httpx.Client


@pytest.fixture
def local_agent(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'agent.db'))
    monkeypatch.setenv('LLM_ENABLED', 'true')
    monkeypatch.setenv('LLM_AGENT_ENABLED', 'true')
    monkeypatch.setenv('OLLAMA_MODEL', 'local:test')
    monkeypatch.setenv('OLLAMA_URL', 'http://127.0.0.1:11434')
    monkeypatch.setenv('LLM_DAILY_TOKEN_BUDGET', '50000')
    monkeypatch.delenv('ANALYSIS_SYSTEM_IDS', raising=False)
    store.init_db()
    store.set_state('data_mode', 'demo')
    store.cache_entities([{'id': 930001, 'category': 'solar_system', 'name': 'SYNTHETIC Alpha'},
                          {'id': 900001, 'category': 'character', 'name': 'SYNTHETIC Pilot A'},
                          {'id': 900002, 'category': 'character', 'name': 'SYNTHETIC Pilot B'}])
    now = datetime.now(timezone.utc)
    store.ingest_killmail({
        'killmail_id': 800000001, 'solar_system_id': 930001,
        'killmail_time': (now - timedelta(minutes=10)).isoformat(),
        'victim': {'character_id': 900001, 'ship_type_id': 940001},
        'attackers': [{'character_id': 900002, 'ship_type_id': 940002}],
        'zkb': {'npc': False, 'synthetic': True}}, source='demo')
    return monkeypatch


def model_script(monkeypatch, actions):
    posted = []
    script = iter(actions)

    def respond(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'local:test', 'size': 1000}]})
        assert request.url.path == '/api/chat'
        payload = json.loads(request.content)
        posted.append(payload)
        return httpx.Response(200, json={'message': {'content': next(script)},
                                         'prompt_eval_count': 80, 'eval_count': 20})

    monkeypatch.setattr(agent.httpx, 'Client', lambda **kw: REAL_HTTPX_CLIENT(transport=httpx.MockTransport(respond), **kw))
    return posted


def test_activity_tool_yields_verified_evidence_and_valid_json_context(local_agent):
    question = 'Who was active in the last 6 hours?'
    posted = model_script(local_agent, [
        json.dumps({'tool': 'activity', 'arguments': {'question': question}}),
        json.dumps({'answer': 'SYNTHETIC Pilot B appeared on one recorded killmail.'}),
    ])
    result = chat.answer(question)
    assert result['mode'] == 'local_agent' and result['tools_used'] == ['activity']
    assert result['tokens_used'] == chat.llm_status()['tokens_used'] == 200
    assert result['evidence'][0]['killmail_id'] == 800000001
    assert result['evidence'][0]['url'] is None
    assert 'SYNTHETIC DEMO' in result['answer']
    assert 'not live locations' in result['answer']
    observed = json.loads(posted[1]['messages'][-1]['content'])['tool_result']
    assert observed['tool'] == 'activity'
    assert observed['data']['recorded_killmails'] == 1
    assert 'url' not in json.dumps(observed).lower()


@pytest.mark.parametrize('bad', [
    '```json\n{"tool":"activity","arguments":{}}\n```',
    '{"tool":"activity","arguments":{"question":"Who was active?","hours":true}}',
    '{"tool":"arbitrary_sql","arguments":{}}',
])
def test_malformed_actions_fall_back_without_tool_execution(local_agent, bad):
    model_script(local_agent, [bad])
    called = []
    local_agent.setattr(agent, '_dispatch', lambda *a, **k: called.append(1))
    result = chat.answer('Summarize combat')
    assert result['mode'] == 'retrieval' and result['tools_used'] == []
    assert 'calculated summary' in result['answer'] and called == []
    assert result['tokens_used'] == 100


def test_explicit_location_and_hours_cannot_be_broadened(local_agent):
    question = 'Who was active?'
    model_script(local_agent, [json.dumps({'tool': 'activity', 'arguments': {
        'question': question, 'location': {'kind': 'system', 'id': 930002}, 'hours': 24}})])
    retrieve = agent.analyst_activity.retrieve
    called = []
    def counted(*args, **kwargs):
        called.append(1)
        return retrieve(*args, **kwargs)
    local_agent.setattr(agent.analyst_activity, 'retrieve', counted)
    result = chat.answer(question, location={'kind': 'system', 'id': 930001}, hours=6)
    assert result['mode'] == 'retrieval' and result['tools_used'] == [] and called == [1]
    assert 'SYNTHETIC DEMO' in result['answer']


def test_natural_battle_scope_cannot_be_broadened(local_agent):
    question = 'Any fresh battles in lowsec in the last 5 minutes?'
    posted = model_script(local_agent, [json.dumps({'tool': 'battles', 'arguments': {
        'space': 'all', 'fresh_minutes': 30}})])
    result = chat.answer(question)
    assert len(posted) == 1 and result['mode'] == 'retrieval'
    assert result['tools_used'] == [] and 'battle space changed' in result['answer']


def test_natural_battle_scope_is_supplied_when_model_omits_it(local_agent):
    question = 'Any fresh battles in lowsec in the last 5 minutes?'
    model_script(local_agent, [json.dumps({'tool': 'battles', 'arguments': {}}),
                               json.dumps({'answer': 'No matching fresh signals were found.'})])
    called = []
    def recent(**kwargs):
        called.append(kwargs)
        return {'data_mode': 'demo', 'items': [], 'limitations': []}
    local_agent.setattr(agent.battles, 'recent', recent)
    result = chat.answer(question)
    assert result['mode'] == 'local_agent' and result['tools_used'] == ['battles']
    assert called == [{'mode': 'fresh', 'fresh_minutes': 5, 'space': 'lowsec'}]


def test_j_space_is_wormhole_scope(local_agent):
    question = 'Any fresh battles in J-space?'
    model_script(local_agent, [json.dumps({'tool': 'battles', 'arguments': {}}),
                               json.dumps({'answer': 'No fresh signals in the local record.'})])
    called = []
    def recent(**kwargs):
        called.append(kwargs)
        return {'data_mode': 'demo', 'items': [], 'limitations': []}
    local_agent.setattr(agent.battles, 'recent', recent)
    result = chat.answer(question)
    assert result['mode'] == 'local_agent'
    assert called == [{'mode': 'fresh', 'fresh_minutes': 5, 'space': 'wormhole'}]


@pytest.mark.parametrize('question,expected', [
    ('Where am I right now?', 'do not have your current location'),
    ('Can killmails tell me who is online?', 'do not provide login status'),
])
def test_direct_limitation_answers_are_deterministic(local_agent, question, expected):
    posted = model_script(local_agent, [json.dumps({'answer': 'You are in Jita right now.'})])
    result = chat.answer(question)
    assert len(posted) == 1 and result['mode'] == 'local_agent'
    assert result['tools_used'] == [] and result['evidence'] == []
    assert expected in result['answer'] and 'You are in Jita' not in result['answer']


def test_total_context_limit_stops_before_second_reservation(local_agent):
    question = 'Who was active?'
    posted = model_script(local_agent, [json.dumps({'tool': 'activity', 'arguments': {'question': question}})])
    def huge_result(*args):
        return ({'tool': 'activity', 'summary': 'One observed killmail.',
                 'data': {'data_mode': 'demo', 'records': 'x' * 6500}},
                [{'killmail_id': 800000001, 'url': None}])
    local_agent.setattr(agent, '_dispatch', huge_result)
    result = chat.answer(question)
    assert len(posted) == 1 and result['mode'] == 'retrieval'
    assert result['tokens_used'] == chat.llm_status()['tokens_used'] == 100
    assert result['tools_used'] == ['activity'] and 'context limit' in result['answer']
    assert result['evidence'][0]['killmail_id'] == 800000001


def test_max_three_calls_and_repeated_tool_guard(local_agent):
    question = 'Who was active?'
    tool = json.dumps({'tool': 'activity', 'arguments': {'question': question}})
    posted = model_script(local_agent, [tool, tool])
    result = chat.answer(question)
    assert len(posted) == 2 and result['tools_used'] == ['activity']
    assert result['mode'] == 'retrieval' and 'repeated' in result['answer']
    assert chat.llm_status()['tokens_used'] == 200


def test_fresh_battles_tool_attaches_only_its_observed_killmail(local_agent):
    posted = model_script(local_agent, [
        json.dumps({'tool': 'battles', 'arguments': {'fresh_minutes': 30, 'space': 'all'}}),
        json.dumps({'answer': 'SYNTHETIC Alpha has one recent PvP loss, an early signal.'}),
    ])
    result = chat.answer('Any fresh battles?')
    assert result['mode'] == 'local_agent' and result['tools_used'] == ['battles']
    assert [item['killmail_id'] for item in result['evidence']] == [800000001]
    tool_result = json.loads(posted[1]['messages'][-1]['content'])['tool_result']
    assert tool_result['data']['items'][0]['signal'] == 'early_signal'
    assert 'https://' not in json.dumps(tool_result)


def test_budget_stops_second_call_and_uses_latest_tool_summary(local_agent):
    question = 'Who was active?'
    local_agent.setenv('LLM_DAILY_TOKEN_BUDGET', str(chat.RESERVATION + 99))
    posted = model_script(local_agent, [json.dumps({'tool': 'activity', 'arguments': {'question': question}})])
    result = chat.answer(question)
    assert len(posted) == 1 and result['tools_used'] == ['activity']
    assert result['mode'] == 'retrieval' and result['tokens_used'] == 100
    assert 'SYNTHETIC Pilot B' in result['answer']
    assert result['evidence'][0]['killmail_id'] == 800000001


def test_three_model_call_cap_falls_back_to_latest_tool(local_agent):
    question = 'Who was active?'
    posted = model_script(local_agent, [
        json.dumps({'tool': 'activity', 'arguments': {'question': question}}),
        json.dumps({'tool': 'battles', 'arguments': {}}),
        json.dumps({'tool': 'forecast', 'arguments': {}}),
    ])
    result = chat.answer(question)
    assert len(posted) == agent.MAX_CALLS == 3
    assert result['mode'] == 'retrieval' and result['tools_used'] == ['activity', 'battles', 'forecast']
    assert result['tokens_used'] == chat.llm_status()['tokens_used'] == 300
    assert 'Experimental next-hour' in result['answer']
    assert result['evidence'] == []


def test_unverified_urls_and_live_location_claims_fall_back(local_agent):
    question = 'Who was active?'
    for answer in ('See https://unverified.example/kill/1', 'SYNTHETIC Pilot B is currently online.'):
        model_script(local_agent, [json.dumps({'tool': 'activity', 'arguments': {'question': question}}),
                                   json.dumps({'answer': answer})])
        result = chat.answer(question)
        assert result['mode'] == 'retrieval' and result['tools_used'] == ['activity']
        assert 'https://' not in result['answer'] and 'Pilot B is currently online' not in result['answer']


def test_agent_requires_local_downloaded_model_before_reservation(local_agent):
    local_agent.setenv('OLLAMA_URL', 'https://example.com')
    with pytest.raises(ValueError, match='Remote'):
        chat.answer('Who was active?')
    assert chat.llm_status()['tokens_used'] == 0
