"""Optional, bounded local analyst tool loop. Model actions never become code or SQL."""

import json
import re

import httpx

from llm_training.protocol import SYSTEM_PROMPT, ProtocolError, parse_action

from . import analyst_activity, battles, chat, predictor, store

MAX_CALLS = 3
MAX_TOOL_BYTES = 6000
_BATTLE_SPACES = {
    'highsec': r'\bhigh[ -]?sec\b|\bhigh security\b',
    'lowsec': r'\blow[ -]?sec\b|\blow security\b',
    'nullsec': r'\bnull[ -]?sec\b|\bnull security\b|\bzero[ -]?zero\b',
    'wormhole': r'\bwormholes?\b|\bwh space\b|\bj[ -]?space\b',
    'unknown': r'\bunknown space\b',
}
_MINUTES = re.compile(r'\b(?:last|past|within)\s+(\d+)\s*(?:minutes?|mins?|min)\b', re.I)
_WIDER_TIME = re.compile(r'\b(?:last|past|within)\s+\d+\s*(?:hours?|hrs?|days?|weeks?)\b', re.I)


def _bounded(value):
    """Keep complete JSON objects and the limiting metadata while trimming lists."""
    result = chat.display_context(value)
    if not isinstance(result, dict):
        return result
    while len(json.dumps(result, ensure_ascii=False).encode()) > MAX_TOOL_BYTES:
        lists = [key for key, item in result.items() if isinstance(item, list) and item]
        if not lists:
            break
        key = max(lists, key=lambda item: len(json.dumps(result[item], ensure_ascii=False)))
        result[key] = result[key][:max(0, len(result[key]) // 2)]
    return result


def _same_location(a, b):
    return a is not None and b is not None and a.get('kind') == b.get('kind') and a.get('id') == b.get('id')


def _context_fits(messages):
    # Ollama num_ctx is a token cap. Charging one UTF-8 byte as one token,
    # plus framing and the full output allowance, is deliberately conservative.
    used = sum(len(message['content'].encode('utf-8')) + 64 for message in messages)
    return used + chat.OUTPUT_LIMIT + 256 < chat.CONTEXT_LIMIT


def _direct_limitation(question):
    if re.search(r'\bwhere\s+(?:am\s+i|is\s+my\s+(?:ship|pilot|character)|are\s+we)\b|\bmy\s+current\s+location\b', question, re.I):
        return 'I do not have your current location. Select a system, constellation, or region to analyze locally recorded combat activity there.'
    if re.search(r'\b(?:online|logged\s+in|login\s+status)\b', question, re.I) and re.search(
            r'\b(?:killmails?|who|pilots?|players?|characters?|currently|now)\b', question, re.I):
        return 'Killmails are partial combat observations. They do not provide login status or a reliable current location for a pilot.'
    return None


def _battle_scope(question):
    spaces = [space for space, pattern in _BATTLE_SPACES.items() if re.search(pattern, question, re.I)]
    if len(spaces) > 1:
        raise ProtocolError('the question names multiple space types')
    if _WIDER_TIME.search(question):
        raise ProtocolError('fresh battles cannot preserve a requested hours-or-days window')
    match = _MINUTES.search(question)
    minutes = int(match.group(1)) if match else None
    if minutes is not None and not 1 <= minutes <= 30:
        raise ProtocolError('fresh battles only support the last 1 to 30 minutes')
    named_location, clarification = analyst_activity._resolve(question, None)
    if named_location:
        raise ProtocolError('this tool cannot preserve the named system, constellation, or region')
    if clarification and not spaces:
        raise ProtocolError(clarification)
    return (spaces[0] if spaces else None), minutes


def _dispatch(action, question, initial, location, hours):
    name, args = action['tool'], action['arguments']
    selected = location or initial.get('location')
    selected_hours = hours if hours is not None else initial.get('hours') if 'pilots' in initial else None
    if name == 'activity':
        # The original wording carries named places and relative time windows. It
        # cannot be replaced by a model paraphrase with a wider scope.
        if args['question'] != question:
            raise ProtocolError('activity question changed the user request')
        if selected and 'location' in args and not _same_location(args['location'], selected):
            raise ProtocolError('activity location changed the selected scope')
        if selected_hours is not None and 'hours' in args and args['hours'] != selected_hours:
            raise ProtocolError('activity hours changed the selected window')
        if selected is None and 'location' in args:
            raise ProtocolError('activity location was not selected by the user')
        result = analyst_activity.retrieve(question, location=selected, hours=selected_hours)
        if result is None:
            raise ProtocolError('activity tool requires a combat activity question')
        context, evidence, summary = result
        if context.get('needs_clarification'):
            raise ProtocolError(summary)
    elif name == 'battles':
        if selected or hours is not None:
            raise ProtocolError('fresh battles cannot preserve the selected area or time window')
        requested_space, requested_minutes = _battle_scope(question)
        if requested_space and args.get('space', requested_space) != requested_space:
            raise ProtocolError('battle space changed the requested scope')
        if requested_minutes and args.get('fresh_minutes', requested_minutes) != requested_minutes:
            raise ProtocolError('battle minutes changed the requested window')
        space = requested_space or args.get('space', 'all')
        context = battles.recent(mode='fresh', fresh_minutes=requested_minutes or args.get('fresh_minutes', 5),
                                 space=None if space == 'all' else space)
        evidence = [entry for item in context['items'][:5] for entry in item['evidence'][:3]]
        summary = 'Fresh recorded combat signals: ' + (', '.join(
            f"{item['system_name']} ({item['status_label']}; {item['kills']} recorded killmails)"
            for item in context['items'][:5]) or 'none in the selected window') + '. These signals do not establish an ongoing fight.'
    elif name == 'forecast':
        if selected or hours is not None:
            raise ProtocolError('forecast cannot preserve the selected area or time window')
        requested_space, requested_minutes = _battle_scope(question)
        if requested_space or requested_minutes:
            raise ProtocolError('forecast cannot preserve the requested battle scope')
        context = predictor.forecast()
        evidence = []
        summary = 'Experimental next-hour recorded combat forecast: ' + (', '.join(
            f"{item['system_name']} ({item['probability']:.1%}; {item['confidence']})"
            for item in context['items'][:5]) or 'no eligible forecasts') + '.'
    else:
        raise ProtocolError('unknown tool')
    context['data_mode'] = store.get_state('data_mode', store.get_state('npc_data_mode', 'empty'))
    payload = {'tool': name, 'summary': summary[:1200], 'data': _bounded(context)}
    if len(json.dumps({'tool_result': payload}, ensure_ascii=False).encode()) > MAX_TOOL_BYTES:
        payload['data'] = {'data_mode': context['data_mode'], 'truncated': True,
                           'limitations': ['Tool output exceeded the local context limit.']}
    return payload, evidence


def _fallback(summary, evidence, reason, tokens, tools_used, data_mode):
    return _final(f'{summary}\n\nLocal agent used the calculated summary because {reason}.',
                  'retrieval', evidence, tokens, data_mode, tools_used)


def _final(content, mode, evidence, tokens, data_mode, tools_used):
    if re.search(r'https?://|www\.', content, re.I):
        raise ProtocolError('model answer included an unverified link')
    if mode == 'local_agent' and re.search(r'\b\d{7,}\b', content):
        raise ProtocolError('model answer included a numeric EVE identifier')
    if mode == 'local_agent' and re.search(
            r'\b(?:is|are)\s+(?:currently\s+)?online\b|\b(?:is|are)\s+(?:currently\s+)?(?:in|at)\s+(?:the\s+)?(?:system|region)\b',
            content, re.I):
        raise ProtocolError('model answer claimed live player presence')
    if data_mode == 'demo' and 'SYNTHETIC DEMO' not in content.upper():
        content = 'SYNTHETIC DEMO — not real EVE intelligence.\n' + content
    disclaimer = 'Killmails show partial combat observations, not live locations or login times.'
    if 'not live locations' not in content.lower() and 'not a list of pilots currently online' not in content.lower():
        content += '\n\n' + disclaimer
    return {'answer': content, 'evidence': evidence, 'mode': mode, 'tokens_used': tokens, 'tools_used': tools_used}


def answer(question, context, baseline_evidence, summary, location=None, hours=None):
    """At most three local model calls, each independently reserved and settled."""
    base = chat.local_url()
    model = chat.llm_status()['model']
    if 'cloud' in model.lower():
        raise ValueError('Cloud model tags are disabled; configure a downloaded local Ollama model.')
    tokens = 0
    evidence = list(baseline_evidence)
    grounded_summary = summary
    data_mode = context.get('data_mode')
    tools_used = []
    with httpx.Client(base_url=base, timeout=120, trust_env=False, follow_redirects=False) as client:
        response = client.get('/api/tags')
        response.raise_for_status()
        entry = next((item for item in response.json().get('models', [])
                      if item.get('name') == model or item.get('model') == model), None)
        if not entry:
            raise ValueError(f'Local model {model} is not installed. Pull it in your Ollama server first.')
        if entry.get('remote_host') or entry.get('remote_model') or not entry.get('size', 0):
            raise ValueError('Model must have downloaded local weights; remote-backed models are disabled.')
        user_text = question
        selected = context.get('location') or location
        if selected:
            user_text += f"\nSelected {selected['kind']}: {selected.get('name', 'cached location')}"
        if hours is not None:
            user_text += f'\nSelected time window: {hours} hours'
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_text}]
        for call in range(MAX_CALLS):
            if not _context_fits(messages):
                return _fallback(grounded_summary, evidence, 'the conversation reached the local context limit',
                                 tokens, tools_used, data_mode)
            try:
                key = chat.reserve()
            except chat.BudgetExceeded:
                if call == 0:
                    raise
                return _fallback(grounded_summary, evidence, 'the daily model budget was reached',
                                 tokens, tools_used, data_mode)
            response = client.post('/api/chat', json={
                'model': model, 'stream': False, 'think': False, 'messages': messages,
                'options': {'temperature': 0.1, 'num_ctx': chat.CONTEXT_LIMIT,
                            'num_predict': chat.OUTPUT_LIMIT}, 'keep_alive': '5m'})
            response.raise_for_status()
            result = response.json()
            actual = result.get('prompt_eval_count', 0) + result.get('eval_count', 0)
            if actual:
                chat.settle(key, actual)
            tokens += actual or chat.RESERVATION
            content = result.get('message', {}).get('content', '')
            try:
                action = parse_action(content)
                if 'answer' in action:
                    if not tools_used:
                        limitation = _direct_limitation(question)
                        if limitation is None:
                            raise ProtocolError('the model answered before requesting local evidence')
                        return _final(limitation, 'local_agent', [], tokens, data_mode, tools_used)
                    return _final(action['answer'].strip(), 'local_agent', evidence, tokens,
                                  data_mode, tools_used)
                if action['tool'] in tools_used:
                    raise ProtocolError('the model repeated a tool request')
                tool_result, found = _dispatch(action, question, context, location, hours)
                tools_used.append(action['tool'])
                evidence = found
                grounded_summary = tool_result['summary']
                data_mode = tool_result['data'].get('data_mode', data_mode)
                messages.append({'role': 'assistant', 'content': content})
                messages.append({'role': 'user', 'content': json.dumps({'tool_result': tool_result}, ensure_ascii=False)})
            except (ProtocolError, ValueError, KeyError, TypeError) as exc:
                return _fallback(grounded_summary, evidence, str(exc), tokens, tools_used, data_mode)
    return _fallback(grounded_summary, evidence, 'the three model calls ended without a final answer',
                     tokens, tools_used, data_mode)
