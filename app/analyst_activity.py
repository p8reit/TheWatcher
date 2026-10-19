"""Local, bounded killmail evidence for activity questions by cached geography."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
import re

from . import analytics, geography, store

MAX_HOURS = 2160
LIMIT = analytics.LIMIT

_ACTIVITY = re.compile(r'\b(activity|active|who|pilots?|players?|corporations?|corps?|alliances?|ships?|killmails?|kills?|losses|loss|combat)\b', re.I)
_EXCLUDED = re.compile(r'\b(fit|fitting|doctrine|npc|ratting|forecast|predict|prediction)\b', re.I)
_NUMBER_WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12}
_AMOUNT_WORDS = '|'.join(_NUMBER_WORDS)
_DURATION = re.compile(r'\b(?:last|past)\s*(?:(\d+(?:\.\d+)?|' + _AMOUNT_WORDS + r')\s*)?(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b', re.I)
_UNSUPPORTED_DURATION = re.compile(r'\b(?:last|past)\s+(?:\d+(?:\.\d+)?|[a-z]+)\s+(minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)\b', re.I)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Time must include a timezone.')
    return value.astimezone(timezone.utc)


def _hours(question, explicit, now):
    if explicit is not None:
        if isinstance(explicit, bool) or not isinstance(explicit, (int, float)) or not 0 < explicit <= MAX_HOURS:
            raise ValueError(f'Choose a time window from 1 to {MAX_HOURS} hours.')
        return float(explicit), now - timedelta(hours=explicit)
    match = _DURATION.search(question)
    if match:
        raw_amount = match.group(1)
        amount = float(_NUMBER_WORDS.get(raw_amount.lower(), raw_amount) if raw_amount else 1)
        unit = match.group(2).lower()
        hours = amount / 60 if unit.startswith(('m',)) else amount * (168 if unit.startswith('w') else 24 if unit.startswith('d') else 1)
        if not 1 <= hours <= MAX_HOURS:
            raise ValueError(f'Choose a time window from 1 to {MAX_HOURS} hours (90 days).')
        return float(hours), now - timedelta(hours=hours)
    unsupported = _UNSUPPORTED_DURATION.search(question)
    if unsupported:
        raise ValueError(f"I couldn't interpret '{unsupported.group(0)}' as a supported time window. Use minutes, hours, days, or weeks.")
    if re.search(r'\btoday\b', question, re.I):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hours = (now - start).total_seconds() / 3600
        return max(hours, 1 / 60), start
    return 24.0, now - timedelta(hours=24)


def _resolve(question, location):
    cached = geography.all_locations()
    if location is not None:
        if not isinstance(location, dict) or location.get('kind') not in ('system', 'constellation', 'region') or type(location.get('id')) is not int:
            raise ValueError('Location must include a cached system, constellation, or region kind and ID.')
        found = [x for x in cached if x['kind'] == location['kind'] and x['id'] == location['id']]
        return (found[0] if found else None), None
    matches = []
    for item in cached:
        name = item['name'].strip()
        if name and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', question, re.I):
            matches.append(item)
    if matches:
        distinct = {(x['kind'], x['id']): x for x in matches}
        values = list(distinct.values())
        if len(values) > 1:
            longest = max(values, key=lambda x: len(x['name']))
            nested = all(x is longest or (x['name'].casefold() != longest['name'].casefold() and re.search(r'(?<!\w)' + re.escape(x['name'].strip()) + r'(?!\w)', longest['name'], re.I)) for x in values)
            if not nested:
                return None, 'The question names multiple cached locations. Please choose one intended location.'
            return longest, None
        return values[0], None
    if re.search(r'\bhere\b|\b(this|current)\s+(system|constellation|region)\b', question, re.I):
        return None, 'Which system, constellation, or region do you mean? This chat has no selected location.'
    # A location phrase that is not in local cache must not fall through to a global query.
    m = re.search(r'\b(?:in|around|near|at|within)\s+(?:the\s+)?([\w][\w\s\-\']{0,60}?)(?=[?.!,;]|\s+(?:over|during|for|in\s+the\s+last)\b|$)', question, re.I)
    if m:
        phrase = m.group(1).strip()
        # Temporal scope can follow "in" and must not be mistaken for a place.
        if _DURATION.fullmatch(phrase):
            return None, None
        if phrase.casefold() not in {'space', 'eve', 'new eden'}:
            return None, f"I don't have {phrase!r} in the cached geography list. Choose a cached system, constellation, or region."
    return None, None


def _configured_scope():
    sets = []
    for key in ('ANALYSIS_SYSTEM_IDS',):
        raw = os.getenv(key, '')
        if raw.strip():
            try:
                vals = {int(x.strip()) for x in raw.split(',') if x.strip()}
            except ValueError as exc:
                raise ValueError(f'{key} must contain positive system IDs.') from exc
            if any(x <= 0 for x in vals):
                raise ValueError(f'{key} must contain positive system IDs.')
            sets.append(vals)
    return set.intersection(*sets) if sets else None


def _involvement(row):
    raw = json.loads(row['raw'])
    victim = raw.get('victim') or {}
    attackers = [a for a in raw.get('attackers', []) if isinstance(a, dict)]
    # NPC-only killmails have no character participants on either side.
    if raw.get('zkb', {}).get('npc') is True:
        return None
    if not victim.get('character_id') and not any(type(a.get('character_id')) is int and a['character_id'] > 0 for a in attackers):
        return None
    return raw, victim, attackers


def retrieve(question, location=None, hours=None, now=None):
    """Return (context, evidence, deterministic summary), or None for other questions."""
    if not isinstance(question, str) or not _ACTIVITY.search(question):
        return None
    if _EXCLUDED.search(question) and not re.search(r'\bwho\s+(?:is\s+)?active\b', question, re.I):
        return None
    current = _utc(now or datetime.now(timezone.utc))
    window_hours, start = _hours(question, hours, current)
    selected, clarification = _resolve(question, location)
    base = {'hours': window_hours, 'window_start': start.isoformat(), 'as_of': current.isoformat(),
            'recorded_killmails': 0, 'pilots': [], 'corporations': [], 'alliances': [], 'ships': [], 'systems': [],
            'evidence': [], 'limitations': list(analytics.LIMITATIONS)}
    if clarification or (location is not None and selected is None):
        base['needs_clarification'] = True
        summary = clarification or 'That location is not present in the cached geography list. Choose a cached location.'
        return base, [], summary
    if selected is None and re.search(r'\b(this|here|current)\s+(system|constellation|region)\b', question, re.I):
        base['needs_clarification'] = True
        return base, [], 'Which system, constellation, or region do you mean? This chat has no selected location.'

    scope = _configured_scope()
    where = ['julianday(killmail_time) > julianday(?)', 'julianday(killmail_time) <= julianday(?)',
             'julianday(observed_at) <= julianday(?)',
             "COALESCE(json_extract(k.raw,'$.zkb.npc'),0) != 1",
             "(json_extract(k.raw,'$.victim.character_id') IS NOT NULL OR EXISTS (SELECT 1 FROM json_each(k.raw,'$.attackers') a WHERE CAST(json_extract(a.value,'$.character_id') AS INTEGER) > 0))"]
    params = [start.isoformat(), current.isoformat(), current.isoformat()]
    joins = ''
    if selected:
        base['location'] = {'id': selected['id'], 'kind': selected['kind'], 'name': selected['name']}
        if selected['kind'] == 'system':
            where.append("json_extract(raw,'$.solar_system_id')=?")
            params.append(selected['id'])
        else:
            joins = ' LEFT JOIN geography_systems g ON g.system_id=json_extract(k.raw,\'$.solar_system_id\')'
            col = 'region_id' if selected['kind'] == 'region' else 'constellation_id'
            where.append(f'g.{col}=?')
            params.append(selected['id'])
    if scope is not None:
        if not scope:
            base['recorded_killmails'] = 0
            base['scope_empty'] = True
            return base, [], 'No configured systems are available for this analysis scope.'
        where.append("json_extract(raw,'$.solar_system_id') IN (" + ','.join('?' for _ in scope) + ')')
        params.extend(sorted(scope))
    sql = 'SELECT k.* FROM killmails k' + joins + ' WHERE ' + ' AND '.join(where) + ' ORDER BY julianday(killmail_time) DESC,k.id DESC LIMIT ?'
    with closing(store.connect()) as c:
        rows = c.execute(sql, (*params, LIMIT + 1)).fetchall()
        geo_available = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='geography_systems'").fetchone()
        mapped_count = 0
        if selected and selected['kind'] != 'system' and geo_available:
            col = 'region_id' if selected['kind'] == 'region' else 'constellation_id'
            mapped_count = c.execute(f'SELECT COUNT(*) FROM geography_systems WHERE {col}=?', (selected['id'],)).fetchone()[0]
    if len(rows) > LIMIT:
        raise ValueError(f'Activity analysis reached the {LIMIT:,} killmail cap. Narrow the time window or location.')
    if selected and selected['kind'] == 'system' and scope is not None and selected['id'] not in scope:
        base['scope_excluded'] = True
        return base, [], f"{selected['name']} is outside the systems allowed by ANALYSIS_SYSTEM_IDS, so I cannot report activity there."
    if selected and selected['kind'] != 'system' and not mapped_count:
        base['geography_coverage'] = 'unavailable'
        base['limitations'].append(f'No cached system mappings are available for this {selected["kind"]}; activity coverage is unavailable.')
        return base, [], f"Cached system mappings for {selected['name']} are unavailable, so I cannot determine activity there."
    base['recorded_killmails'] = 0
    pilots, corps, alliances, ships, systems = (Counter() for _ in range(5))
    pilot_kills, pilot_losses, corp_kills, corp_losses, alliance_kills, alliance_losses = (Counter() for _ in range(6))
    latest_event = latest_observed = None
    evidence = []
    for row in rows:
        parts = _involvement(row)
        if parts is None:
            continue
        base['recorded_killmails'] += 1
        raw, victim, attackers = parts
        latest_event = latest_event or raw.get('killmail_time')
        if latest_observed is None or analytics.dt(row['observed_at']) > analytics.dt(latest_observed):
            latest_observed = row['observed_at']
        sid = int(raw['solar_system_id'])
        systems[sid] += 1
        # Count each participant once per killmail, avoiding multiple attackers from an org inflating its kill count.
        seen_pilots, seen_corps, seen_alliances, seen_ships = set(), set(), set(), set()
        attacker_pilots = set()
        for actor in attackers:
            cid = actor.get('character_id')
            if type(cid) is not int or cid <= 0:
                continue
            seen_pilots.add(cid)
            attacker_pilots.add(cid)
            if actor.get('corporation_id'):
                seen_corps.add(actor['corporation_id'])
            if actor.get('alliance_id'):
                seen_alliances.add(actor['alliance_id'])
            if actor.get('ship_type_id'): seen_ships.add(actor['ship_type_id'])
        if victim.get('character_id'):
            seen_pilots.add(victim['character_id']); pilot_losses[victim['character_id']] += 1
        if victim.get('character_id') and victim.get('corporation_id'):
            seen_corps.add(victim['corporation_id']); corp_losses[victim['corporation_id']] += 1
        if victim.get('character_id') and victim.get('alliance_id'):
            seen_alliances.add(victim['alliance_id']); alliance_losses[victim['alliance_id']] += 1
        if victim.get('character_id') and victim.get('ship_type_id'): seen_ships.add(victim['ship_type_id'])
        for ident in attacker_pilots: pilot_kills[ident] += 1
        for ident in {a.get('corporation_id') for a in attackers if type(a.get('character_id')) is int and a['character_id'] > 0 and a.get('corporation_id')}: corp_kills[ident] += 1
        for ident in {a.get('alliance_id') for a in attackers if type(a.get('character_id')) is int and a['character_id'] > 0 and a.get('alliance_id')}: alliance_kills[ident] += 1
        pilots.update(seen_pilots); corps.update(seen_corps); alliances.update(seen_alliances); ships.update(seen_ships)
        if len(evidence) < 8:
            evidence.append(analytics.evidence(dict(raw, source=row['source'])))
    base['pilots'] = [{'id': i, 'name': analytics.name(i, 'pilot'), 'involvement': n, 'kills': pilot_kills[i], 'losses': pilot_losses[i]} for i, n in pilots.most_common(15)]
    base['corporations'] = [{'id': i, 'name': analytics.name(i, 'corporation'), 'involvement': n, 'kills': corp_kills[i], 'losses': corp_losses[i]} for i, n in corps.most_common(15)]
    base['alliances'] = [{'id': i, 'name': analytics.name(i, 'alliance'), 'involvement': n, 'kills': alliance_kills[i], 'losses': alliance_losses[i]} for i, n in alliances.most_common(15)]
    base['ships'] = [{'id': i, 'name': analytics.name(i, 'ship'), 'appearances': n} for i, n in ships.most_common(15)]
    base['systems'] = [{'id': i, 'name': analytics.name(i, 'system'), 'killmails': n} for i, n in systems.most_common(15)]
    base['last_event_at'] = latest_event
    base['last_observed_at'] = latest_observed
    base['evidence'] = evidence
    if selected and selected['kind'] in ('region', 'constellation'):
        base['geography_coverage'] = 'partial'
        base['limitations'].append('Cached system-to-region and system-to-constellation mappings may be incomplete; this result covers mapped systems only.')
    base['limitations'].append('Killmail involvement is evidence of recorded combat participation, not online status or current position.')
    scope_label = selected['name'] if selected else 'all configured systems' if scope is not None else 'all recorded systems'
    if not base['pilots']:
        summary = f'No eligible character-involved killmails were recorded in {scope_label} during the last {window_hours:g} hours.'
    else:
        summary = f"Recorded activity in {scope_label} over the last {window_hours:g} hours: " + ', '.join(f"{p['name']} ({p['involvement']} killmail{'s' if p['involvement'] != 1 else ''})" for p in base['pilots'][:5]) + '.'
        if base['corporations']:
            summary += '\nCorporations: ' + ', '.join(f"{x['name']} ({x['involvement']})" for x in base['corporations'][:5]) + '.'
        if base['alliances']:
            summary += '\nAlliances: ' + ', '.join(f"{x['name']} ({x['involvement']})" for x in base['alliances'][:5]) + '.'
        if base['ships']:
            summary += '\nShips: ' + ', '.join(f"{x['name']} ({x['appearances']})" for x in base['ships'][:5]) + '.'
    if selected and selected['kind'] in ('region', 'constellation'):
        summary += ' Cached geography mappings may be incomplete, so this covers mapped systems only.'
    stamp = lambda value: analytics.dt(value).strftime('%Y-%m-%d %H:%M UTC') if value else 'none'
    summary += f'\nWindow: {start:%Y-%m-%d %H:%M} to {current:%Y-%m-%d %H:%M} UTC. Newest event: {stamp(latest_event)}; newest receipt: {stamp(latest_observed)}.' 
    summary += '\nThese are recorded combat observations, not a list of pilots currently online or their present locations.'
    return base, evidence, summary
