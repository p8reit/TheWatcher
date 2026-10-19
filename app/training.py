"""Exact scoped record counts, independent of the forecast display cap."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from . import store, historical_predictor
from .analytics import LIMIT


def scope_preview(mode='live', days=42, now=None):
    now = now or datetime.now(timezone.utc)
    scope = store.analysis_scope()
    result = {'items': [], 'llm_tokens': 0, 'max_systems': 12,
              'record_cap': LIMIT if mode == 'live' else None, 'unavailable_reason': None}
    if mode == 'historical':
        if store.get_state('data_mode') != 'live':
            result['unavailable_reason'] = 'Historical archive training requires a live database.'
            return result
        try:
            first, end, _ = historical_predictor.completed_window(now, days)
        except ValueError as exc:
            result['unavailable_reason'] = str(exc)
            return result
        # Match the historical trainer's event-time SQL bounds and NPC exclusion.
        clause = "killmail_time>=? AND killmail_time<? AND COALESCE(json_extract(raw,'$.zkb.npc'),0)=0"
        params = [first.strftime('%Y-%m-%dT%H:%M:%S'), end.strftime('%Y-%m-%dT%H:%M:%S')]
        result['window'] = f'{first.date()} to {end.date()} (end exclusive)'
    else:
        clause = 'killmail_time>=?'
        params = [(now-timedelta(days=90)).isoformat().replace('+00:00', 'Z')]
        result['window'] = 'Last 90 days; includes NPC records checked before PvP filtering'
    if scope:
        clause += " AND json_extract(raw,'$.solar_system_id') IN (" + ','.join('?' for _ in scope) + ')'
        params.extend(scope)
    with closing(store.connect()) as conn:
        rows = conn.execute("SELECT counts.sid, counts.n, entities.name AS system_name FROM ("
                            "SELECT json_extract(raw,'$.solar_system_id') AS sid, COUNT(*) AS n "
                            'FROM killmails WHERE ' + clause + ' GROUP BY sid) AS counts '
                            'LEFT JOIN entities ON entities.id=counts.sid', params).fetchall()
    result['items'] = [{'system_id': r['sid'], 'system_name': r['system_name'],
                        'records': r['n'], 'llm_tokens': 0} for r in rows]
    return result
