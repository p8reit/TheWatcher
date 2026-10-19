"""Observed combat windows, never inferred fleets, sides, or live engagements."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import os
from urllib.parse import quote
from . import analytics, store

KILL_LIMIT = 50000
DISPLAY_LIMIT = 40
GAP = timedelta(minutes=30)
MAX_SPAN = timedelta(hours=2)


def report_links(system_id, start, synthetic=False):
    """Same related-report contract used by zKill's templates/related.pug.

    WarBeacon consumes the UTC YYYYMMDDHH00 anchor; EVE Tools consumes Unix
    seconds (classes/RelatedReport.php). These are contextual report entry
    points, not a claim to publish a persisted report or preserve our end time.
    """
    if synthetic:
        return {'battle_report_url': None, 'eve_tools_url': None, 'report_anchor': None}
    if isinstance(system_id, bool) or not isinstance(system_id, int) or system_id <= 0:
        raise ValueError('Invalid system ID')
    if start.tzinfo is None:
        raise ValueError('Report time must include timezone')
    anchor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    sid = quote(str(system_id), safe='')
    return {'battle_report_url': f'https://warbeacon.net/br/related/{sid}/{anchor:%Y%m%d%H}00/',
            'eve_tools_url': f'https://br.evetools.org/related/{sid}/{int(anchor.timestamp())}/',
            'report_anchor': anchor.isoformat()}


def recent(hours=6, now=None, mode='history', fresh_minutes=5, space=None):
    if hours not in (6, 24):
        raise ValueError('Choose a 6 or 24 hour combat window.')
    if mode not in ('history', 'fresh'):
        raise ValueError('Choose history or fresh combat mode.')
    if isinstance(fresh_minutes, bool) or not isinstance(fresh_minutes, int) or not 1 <= fresh_minutes <= 30:
        raise ValueError('Fresh minutes must be between 1 and 30.')
    valid_spaces = {'highsec', 'lowsec', 'nullsec', 'wormhole', 'unknown'}
    if space is not None and space not in valid_spaces:
        raise ValueError('Choose a valid space: highsec, lowsec, nullsec, wormhole, or unknown.')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Current time must include timezone')
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    scope = sorted({int(x.strip()) for x in os.getenv('ANALYSIS_SYSTEM_IDS', '').split(',') if x.strip()})
    clause = " AND json_extract(raw,'$.solar_system_id') IN (" + ','.join('?' for _ in scope) + ')' if scope else ''
    fresh_params = []
    if mode == 'fresh':
        fresh_clause = ''' AND json_extract(raw,'$.solar_system_id') IN (
            SELECT DISTINCT json_extract(k.raw,'$.solar_system_id') FROM killmails k
            WHERE k.killmail_time>=? AND k.killmail_time<?
              AND julianday(k.killmail_time)>=julianday(?) AND julianday(k.killmail_time)<=julianday(?)
              AND julianday(k.observed_at)>=julianday(k.killmail_time)
              AND julianday(k.observed_at)<=julianday(?)
              AND COALESCE(json_extract(k.raw,'$.zkb.npc'),0)=0
              AND EXISTS (SELECT 1 FROM json_each(k.raw,'$.attackers') a
                          WHERE CAST(json_extract(a.value,'$.character_id') AS INTEGER)>0)'''
        if scope:
            fresh_clause += " AND json_extract(k.raw,'$.solar_system_id') IN (" + ','.join('?' for _ in scope) + ')'
        fresh_clause += ')'
        clause += fresh_clause
        fresh_cutoff = now-timedelta(minutes=fresh_minutes)
        fresh_params = [fresh_cutoff.strftime('%Y-%m-%dT%H:%M:%S'),
                        (now+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S'),
                        fresh_cutoff.isoformat(), now.isoformat(), now.isoformat(), *scope]
    with closing(store.connect()) as db:
        rows = db.execute('''SELECT * FROM killmails WHERE killmail_time>=? AND killmail_time<?
            AND julianday(killmail_time)>julianday(?) AND julianday(killmail_time)<=julianday(?)
            AND julianday(observed_at)<=julianday(?)''' + clause +
            ' ORDER BY julianday(killmail_time),id LIMIT ?',
            (cutoff.strftime('%Y-%m-%dT%H:%M:%S'), (now+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S'),
             cutoff.isoformat(), now.isoformat(), now.isoformat(), *scope, *fresh_params, KILL_LIMIT+1)).fetchall()
    if len(rows) > KILL_LIMIT:
        raise ValueError('Combat analysis cap reached. Narrow ANALYSIS_SYSTEM_IDS or select 6 hours.')
    grouped = defaultdict(list)
    for row in rows:
        k = dict(json.loads(row['raw']), source=row['source'], observed_at=row['observed_at'])
        # An identified player attacker provides positive PvP evidence; missing
        # NPC flags alone are insufficient. NPC losses never form combat windows.
        if k.get('zkb', {}).get('npc') or not any(a.get('character_id') for a in k['attackers']):
            continue
        grouped[k['solar_system_id']].append(k)
    data_mode = store.get_state('data_mode', 'empty')
    windows = []
    for sid, kills in grouped.items():
        cluster = []
        for k in kills:
            event = analytics.dt(k['killmail_time'])
            if cluster and (event-analytics.dt(cluster[-1]['killmail_time']) > GAP or
                            event-analytics.dt(cluster[0]['killmail_time']) > MAX_SPAN):
                if len(cluster) >= 2 or mode == 'fresh': windows.append((sid, cluster))
                cluster = []
            cluster.append(k)
        if len(cluster) >= 2 or mode == 'fresh': windows.append((sid, cluster))
    windows.sort(key=lambda pair: analytics.dt(pair[1][-1]['killmail_time']), reverse=True)
    items = []
    fresh_delta = timedelta(minutes=fresh_minutes)
    eligible = []
    for sid, kills in windows:
        qualifying = []
        for k in kills:
            event, receipt = analytics.dt(k['killmail_time']), analytics.dt(k['observed_at'])
            if receipt >= event and now-event <= fresh_delta and now-receipt <= fresh_delta:
                qualifying.append(k)
        if mode == 'fresh' and not qualifying:
            continue
        eligible.append((sid, kills, qualifying))
    # Geography is optional at import time: the companion setup can land this helper independently.
    from . import geography
    spaced = []
    for sid, kills, qualifying in eligible:
        details = geography.space_details(sid)
        if space is not None and details.get('space_type') != space:
            continue
        spaced.append((sid, kills, qualifying, details))
    for sid, kills, qualifying, details in spaced[:DISPLAY_LIMIT]:
        pilots = {a['character_id'] for k in kills for a in [k['victim'], *k['attackers']] if a.get('character_id')}
        ships = Counter(k['victim']['ship_type_id'] for k in kills)
        values = [k.get('zkb', {}).get('totalValue') for k in kills]
        values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0]
        start, end = (analytics.dt(kills[i]['killmail_time']) for i in (0, -1))
        synthetic = data_mode == 'demo' or any(k['source'] == 'demo' or k.get('zkb', {}).get('synthetic') for k in kills)
        event_times = [analytics.dt(k['killmail_time']) for k in kills]
        receipts = [analytics.dt(k['observed_at']) for k in kills]
        newest = max(range(len(kills)), key=lambda i: event_times[i])
        latest_event, latest_receipt = event_times[newest], max(receipts)
        first_event = min(event_times)
        if len(kills) == 1:
            signal, label = 'early_signal', 'One recent PvP loss'
        elif len(qualifying) >= 2 and now-first_event <= timedelta(minutes=15):
            signal, label = 'forming', 'Possible forming fight'
        else:
            signal, label = 'recent_combat', 'Recent combat'
        evidence_kills = sorted(kills, key=lambda k: analytics.dt(k['killmail_time']), reverse=True) if mode == 'fresh' else kills
        items.append({'system_id': sid, 'system_name': store.entity_name(sid, 'system'),
                      'start': start.isoformat(), 'end': end.isoformat(), 'kills': len(kills),
                      'signal': signal, 'status_label': label,
                      'latest_event_at': latest_event.isoformat(), 'latest_event_end': latest_event.isoformat(),
                      'latest_received_at': latest_receipt.isoformat(),
                      'latest_event_age_seconds': max(0, (now-latest_event).total_seconds()),
                      'latest_received_age_seconds': max(0, (now-latest_receipt).total_seconds()),
                      'first_event_age_seconds': max(0, (now-first_event).total_seconds()),
                      'report_delay_seconds': ((analytics.dt(kills[newest]['observed_at'])-latest_event).total_seconds()
                                               if analytics.dt(kills[newest]['observed_at']) >= latest_event else None),
                      'fresh_kills': len(qualifying),
                      'space_type': details.get('space_type', 'unknown'),
                      'space_label': details.get('space_label', 'Unknown'),
                      'security_status': details.get('security_status'),
                      'observed_pilots': len(pilots), 'reported_value_isk': sum(values) if values else None,
                      'value_known_kills': len(values), 'synthetic': synthetic,
                      'sources': sorted({k['source'] for k in kills}),
                      'ships': [{'ship_type_id': ship, 'ship_name': store.entity_name(ship, 'ship'), 'losses': count}
                                for ship, count in ships.most_common()],
                      'evidence': [analytics.evidence(k) for k in evidence_kills[:20]],
                      'evidence_truncated': len(kills) > 20,
                      **report_links(sid, start, synthetic)})
    collector = analytics.collector_status()
    source_counts = dict(Counter(k['source'] for _, kills, _, _ in spaced for k in kills))
    return {'data_mode': data_mode, 'mode': mode, 'fresh_minutes': fresh_minutes, 'hours': hours, 'as_of': now.isoformat(), 'window_start': cutoff.isoformat(),
            'items': items, 'total_windows': len(spaced),
            'total_fresh_signals': sum(bool(qualifying) for _, _, qualifying, _ in spaced),
            'display_truncated': len(spaced) > DISPLAY_LIMIT, 'sources': source_counts, 'collector': collector,
            'analysis_limit': KILL_LIMIT, 'scope': scope or None,
            'limitations': [('Fresh mode can include a single observed PvP loss when its event and receipt are both within '
                            f'{fresh_minutes} minutes and the receipt is not before the event; history requires at least two PvP killmails. '
                            if mode == 'fresh' else 'History requires at least two PvP killmails in a system. ')
                           + 'Groups use one system with gaps of at most 30 minutes, up to two hours total.',
                           'Windows may combine unrelated losses or split a longer fight. They do not identify sides, winners, fleets, or ongoing activity.',
                           'Coverage is partial and late arrivals can change a window. Pilots count only identified characters on these killmails.',
                           'ISK sums reported killmail values where available; ship counts are observed losses, not fleet composition.',
                           'Battle report links open the system and starting UTC hour. Review the external time window and sides; it may differ from this local cluster.']}
