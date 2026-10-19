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


def recent(hours=6, now=None):
    if hours not in (6, 24):
        raise ValueError('Choose a 6 or 24 hour combat window.')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Current time must include timezone')
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    scope = sorted({int(x.strip()) for x in os.getenv('ANALYSIS_SYSTEM_IDS', '').split(',') if x.strip()})
    clause = " AND json_extract(raw,'$.solar_system_id') IN (" + ','.join('?' for _ in scope) + ')' if scope else ''
    with closing(store.connect()) as db:
        rows = db.execute('''SELECT * FROM killmails WHERE killmail_time>=? AND killmail_time<?
            AND julianday(killmail_time)>julianday(?) AND julianday(killmail_time)<=julianday(?)
            AND julianday(observed_at)<=julianday(?)''' + clause +
            ' ORDER BY julianday(killmail_time),id LIMIT ?',
            (cutoff.strftime('%Y-%m-%dT%H:%M:%S'), (now+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S'),
             cutoff.isoformat(), now.isoformat(), now.isoformat(), *scope, KILL_LIMIT+1)).fetchall()
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
    mode = store.get_state('data_mode', 'empty')
    windows = []
    for sid, kills in grouped.items():
        cluster = []
        for k in kills:
            event = analytics.dt(k['killmail_time'])
            if cluster and (event-analytics.dt(cluster[-1]['killmail_time']) > GAP or
                            event-analytics.dt(cluster[0]['killmail_time']) > MAX_SPAN):
                if len(cluster) >= 2: windows.append((sid, cluster))
                cluster = []
            cluster.append(k)
        if len(cluster) >= 2: windows.append((sid, cluster))
    windows.sort(key=lambda pair: analytics.dt(pair[1][-1]['killmail_time']), reverse=True)
    items = []
    for sid, kills in windows[:DISPLAY_LIMIT]:
        pilots = {a['character_id'] for k in kills for a in [k['victim'], *k['attackers']] if a.get('character_id')}
        ships = Counter(k['victim']['ship_type_id'] for k in kills)
        values = [k.get('zkb', {}).get('totalValue') for k in kills]
        values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0]
        start, end = (analytics.dt(kills[i]['killmail_time']) for i in (0, -1))
        synthetic = mode == 'demo' or any(k['source'] == 'demo' or k.get('zkb', {}).get('synthetic') for k in kills)
        items.append({'system_id': sid, 'system_name': store.entity_name(sid, 'system'),
                      'start': start.isoformat(), 'end': end.isoformat(), 'kills': len(kills),
                      'observed_pilots': len(pilots), 'reported_value_isk': sum(values) if values else None,
                      'value_known_kills': len(values), 'synthetic': synthetic,
                      'sources': sorted({k['source'] for k in kills}),
                      'ships': [{'ship_type_id': ship, 'ship_name': store.entity_name(ship, 'ship'), 'losses': count}
                                for ship, count in ships.most_common()],
                      'evidence': [analytics.evidence(k) for k in kills[:20]],
                      'evidence_truncated': len(kills) > 20,
                      **report_links(sid, start, synthetic)})
    return {'data_mode': mode, 'hours': hours, 'as_of': now.isoformat(), 'window_start': cutoff.isoformat(),
            'items': items, 'total_windows': len(windows), 'display_truncated': len(windows) > DISPLAY_LIMIT,
            'analysis_limit': KILL_LIMIT, 'scope': scope or None,
            'limitations': ['Observed combat windows group at least two PvP killmails in one system with gaps of at most 30 minutes, up to two hours total.',
                           'Windows may combine unrelated losses or split a longer fight. They do not identify sides, winners, fleets, or ongoing activity.',
                           'Coverage is partial and late arrivals can change a window. Pilots count only identified characters on these killmails.',
                           'ISK sums reported killmail values where available; ship counts are observed losses, not fleet composition.',
                           'Battle report links open the system and starting UTC hour. Review the external time window and sides; it may differ from this local cluster.']}
