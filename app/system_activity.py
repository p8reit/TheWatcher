"""Local-only comparison of source reports and individual killmail evidence."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from . import analytics, store

REPORT_LIMIT = 200000
KILL_LIMIT = 50000
DISPLAY_LIMIT = 200


def activity(hours=6, now=None):
    if hours not in (1, 6, 24):
        raise ValueError('Choose a 1, 6 or 24 hour window.')
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    mode = store.get_state('data_mode', store.get_state('npc_data_mode', 'empty'))
    quality = 'synthetic' if mode == 'demo' else 'verified'
    scopes = [{int(x.strip()) for x in os.getenv(key, '').split(',') if x.strip()}
              for key in ('NPC_SYSTEM_IDS', 'ANALYSIS_SYSTEM_IDS')]
    configured = [s for s in scopes if s]
    scope = set.intersection(*configured) if configured else None
    # Both configured scopes apply. An empty intersection must not broaden access.
    clause = '' if scope is None else ' AND o.system_id IN (' + ','.join('?' for _ in scope) + ')'
    kill_clause = '' if scope is None else " AND json_extract(raw,'$.solar_system_id') IN (" + ','.join('?' for _ in scope) + ')'
    params = tuple(sorted(scope)) if scope is not None else ()
    with closing(store.connect()) as c:
        reports = c.execute('''SELECT b.source,b.source_end,b.observed_at,o.* FROM npc_batches b
            JOIN npc_observations o ON b.id=o.batch_id WHERE b.quality=?
            AND b.source_end>? AND b.source_end<=? AND b.observed_at<=?'''+clause+
            ' ORDER BY b.source_end,b.id,o.system_id LIMIT ?',
            (quality, cutoff.isoformat(), now.isoformat(), now.isoformat(), *params, REPORT_LIMIT+1)).fetchall()
        # An indexed coarse range plus parsed bounds handles both whole-second Z
        # timestamps and fractional-second Z timestamps without lexical ambiguity.
        kills = c.execute('''SELECT * FROM killmails WHERE killmail_time>=? AND killmail_time<?
            AND julianday(killmail_time)>julianday(?) AND julianday(killmail_time)<=julianday(?)
            AND julianday(observed_at)<=julianday(?)'''+kill_clause+' ORDER BY julianday(killmail_time) DESC,id DESC LIMIT ?',
            (cutoff.strftime('%Y-%m-%dT%H:%M:%S'), (now+timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S'),
             cutoff.isoformat(), now.isoformat(), now.isoformat(), *params, KILL_LIMIT+1)).fetchall()
        names = {r['id']: r['name'] for r in c.execute('SELECT id,name FROM entities')}
    if len(reports) > REPORT_LIMIT or len(kills) > KILL_LIMIT:
        raise ValueError('System activity analysis cap reached. Narrow NPC_SYSTEM_IDS or ANALYSIS_SYSTEM_IDS.')
    latest, sample_hours = {}, {}
    for r in reports:
        sid = r['system_id']
        latest[sid] = dict(r)
        sample_hours.setdefault(sid, set()).add(analytics.dt(r['source_end']).replace(minute=0,second=0,microsecond=0))
    payloads = [dict(json.loads(k['raw']), source=k['source'], observed_at=k['observed_at']) for k in kills]
    counts = Counter(k['solar_system_id'] for k in payloads)
    items = []
    for sid in latest.keys() | counts.keys():
        r = latest.get(sid)
        age = max(0, (now-analytics.dt(r['source_end'])).total_seconds()) if r else None
        items.append({'system_id':sid, 'system_name':names.get(sid) or store.entity_name(sid, 'system'),
                      'npc_kills':r['npc_kills'] if r else None,
                      'ship_kills':r['ship_kills'] if r else None,
                      'pod_kills':r['pod_kills'] if r else None,
                      'source':r['source'] if r else None, 'quality':quality if r else None,
                      'source_end':r['source_end'] if r else None,
                      'observed_at':r['observed_at'] if r else None,
                      'source_age_seconds':age,
                      'report_delay_seconds':(analytics.dt(r['observed_at'])-analytics.dt(r['source_end'])).total_seconds() if r else None,
                      'freshness':'missing' if r is None else 'stale' if age>7200 else 'recent',
                      'sampled_hours':len(sample_hours.get(sid, set())),
                      'reported_killmails':counts[sid]})
    items.sort(key=lambda r:(-r['reported_killmails'], -(r['ship_kills'] or 0), -(r['npc_kills'] or 0), r['system_id']))
    recent = [dict(analytics.evidence(k), system_id=k['solar_system_id'],
                   system_name=names.get(k['solar_system_id']) or store.entity_name(k['solar_system_id'], 'system'),
                   source=k['source'], observed_at=k['observed_at']) for k in payloads[:30]]
    return {'data_mode':mode, 'hours':hours, 'window_start':cutoff.isoformat(), 'as_of':now.isoformat(),
            'items':items[:DISPLAY_LIMIT], 'total_systems':len(items), 'display_truncated':len(items)>DISPLAY_LIMIT,
            'reported_killmails':len(kills), 'recent':recent, 'collector':analytics.collector_status(),
            'esi_collector_enabled':os.getenv('NPC_COLLECTOR_ENABLED','false').lower()=='true',
            'esi_last_error':store.get_state('npc_collector_error'),
            'scope':sorted(scope) if scope is not None else None,
            'limitations':[
                'ESI columns show the latest reported hourly counts within the selected window; they are not window totals or live positions.',
                'Sampled hours count distinct UTC hours with a report, not continuous coverage. Missing rows remain unknown.',
                'Ship destruction counts can include losses to NPCs. They are not a count of confirmed PvP engagements.',
                'Killmails are counted by event time and may arrive late. Counts are partial and cannot be subtracted from ESI counts to prove missing kills.',
                'Reports older than two hours are marked stale. A recent report is still delayed aggregate data.',
                'Only verified ESI reports (or isolated synthetic demo reports) are shown; legacy snapshots are excluded.',
            ] + ([f'Showing the first {DISPLAY_LIMIT} of {len(items)} systems, ranked by killmails, ship losses, then NPC kills.'] if len(items)>DISPLAY_LIMIT else [])}
