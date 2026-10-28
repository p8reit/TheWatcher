"""Full-history NPC system scopes shared by preview, training, and forecasts."""
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import npc_store, store

MAX_SYSTEMS = 24
MIN_HISTORY = timedelta(days=21)


def preview(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('NPC scope time must include a timezone.')
    now = now.astimezone(timezone.utc)
    cutoff = (now - timedelta(days=90)).isoformat()
    end = now.isoformat()
    mode = 'demo' if store.get_state('npc_data_mode', store.get_state('data_mode', 'empty')) == 'demo' else 'live'
    source, quality = ('demo', 'synthetic') if mode == 'demo' else ('esi', 'verified')
    configured = npc_store.configured_scope()
    clause = ' AND o.system_id IN (' + ','.join('?' for _ in configured) + ')' if configured else ''
    # The same time and source predicates are used by observations(). The aggregate
    # has no row cap so users can choose a complete history when the database is large.
    with closing(store.connect()) as conn:
        rows = conn.execute('''SELECT o.system_id, e.name AS system_name, COUNT(*) AS records,
            MIN(b.observed_at) AS first_observed_at, MAX(b.observed_at) AS last_observed_at
            FROM npc_batches b JOIN npc_observations o ON o.batch_id=b.id
            LEFT JOIN entities e ON e.id=o.system_id
            WHERE b.source=? AND b.quality=? AND b.source_end>=? AND b.source_end<=?
            AND b.observed_at>=? AND b.observed_at<=? AND b.source_end<=b.observed_at'''
            + clause + ' GROUP BY o.system_id ORDER BY o.system_id',
            (source, quality, cutoff, end, cutoff, end, *configured)).fetchall()
        other_source, other_quality = ('esi', 'verified') if mode == 'demo' else ('demo', 'synthetic')
        mixed = conn.execute('''SELECT 1 FROM npc_batches b JOIN npc_observations o ON o.batch_id=b.id
            WHERE b.source=? AND b.quality=? AND b.source_end>=? AND b.source_end<=?
            AND b.observed_at>=? AND b.observed_at<=? AND b.source_end<=b.observed_at'''
            + clause + ' LIMIT 1',
            (other_source, other_quality, cutoff, end, cutoff, end, *configured)).fetchone() is not None
    items = []
    for row in rows:
        first, last = npc_store.utc(row['first_observed_at']), npc_store.utc(row['last_observed_at'])
        items.append({'system_id': row['system_id'],
                      'system_name': row['system_name'] or f"System {row['system_id']} · name unavailable",
                      'records': row['records'], 'first_observed_at': row['first_observed_at'],
                      'last_observed_at': row['last_observed_at'],
                      'history_ready': last - first >= MIN_HISTORY,
                      'auto_selected': False})
    auto = []
    total = 0
    if not mixed:
        for item in items:
            if item['history_ready'] and len(auto) < MAX_SYSTEMS and total + item['records'] <= npc_store.MAX_ROWS:
                auto.append(item['system_id'])
                total += item['records']
                item['auto_selected'] = True
    reason = None
    if mixed:
        reason = ('Mixed synthetic and verified NPC history is not eligible for forecasting.' if items or mode == 'demo'
                  else 'Synthetic NPC history requires explicit demo data_mode.')
    elif not items:
        reason = 'No eligible verified NPC history. Legacy imports are description-only.' if mode == 'live' else 'No eligible synthetic demo NPC history.'
    elif not auto:
        reason = (f'No system has at least 21 days of receipt-time history within the {npc_store.MAX_ROWS:,}-record cap. '
                  'Select a smaller complete system history or collect more data.')
    return {'items': items, 'max_records': npc_store.MAX_ROWS, 'max_systems': MAX_SYSTEMS,
            'auto_system_ids': auto, 'auto_records': total,
            'window': {'from': cutoff, 'through': end, 'days': 90},
            'data_mode': mode, 'unavailable_reason': reason}


def resolve(system_ids=None, auto_select=False, now=None):
    """Validate a requested scope and return IDs and its exact record count."""
    if system_ids is not None and auto_select:
        raise ValueError('Choose system_ids or auto_select, not both.')
    if system_ids is None and not auto_select:
        return None, None
    result = preview(now)
    if result['unavailable_reason'] and result['unavailable_reason'].startswith(('Mixed', 'Synthetic')):
        raise ValueError(result['unavailable_reason'])
    if auto_select:
        if not result['auto_system_ids']:
            raise ValueError(result['unavailable_reason'])
        return result['auto_system_ids'], result['auto_records']
    if (not isinstance(system_ids, list) or not 1 <= len(system_ids) <= MAX_SYSTEMS or
            any(isinstance(s, bool) or not isinstance(s, int) or s <= 0 for s in system_ids) or
            len(set(system_ids)) != len(system_ids)):
        raise ValueError('Select 1 to 24 unique positive system IDs.')
    counts = {item['system_id']: item['records'] for item in result['items']}
    unknown = sorted(set(system_ids) - counts.keys())
    if unknown:
        raise ValueError('Selected systems must have eligible history within NPC_SYSTEM_IDS: ' + ', '.join(map(str, unknown)))
    total = sum(counts[s] for s in system_ids)
    if total > npc_store.MAX_ROWS:
        raise ValueError(f'Selected systems have {total:,} records; the NPC analysis cap is {npc_store.MAX_ROWS:,}. Select fewer systems.')
    return system_ids, total
