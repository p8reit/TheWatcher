"""Deterministic intelligence calculations; every inference keeps its evidence."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import os
from . import store

LIMIT = 50000
LIMITATIONS = [
    'Killmails are partial combat observations, not login or current-location telemetry.',
    'Missing or delayed reports and collection gaps can bias these results.',
    'Shared killmails indicate association, not shared account ownership.',
]

def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)

def records(days=30):
    return store.list_killmails(days=days, limit=LIMIT)

def name(entity_id, category="entity"):
    return store.entity_name(entity_id, category)

def evidence(k):
    synthetic = k.get('source') == 'demo' or k.get('zkb', {}).get('synthetic', False)
    return {'killmail_id': k['killmail_id'], 'url': None if synthetic else f"https://zkillboard.com/kill/{k['killmail_id']}/", 'time': k['killmail_time'], 'synthetic': bool(synthetic)}

def limitations(rows):
    return LIMITATIONS + ([f'Analysis is capped at the newest {LIMIT:,} records; narrow the time window.'] if len(rows) >= LIMIT else [])

def collector_status():
    return {'enabled': os.getenv('COLLECTOR_ENABLED', 'false').lower() == 'true',
            'cursor': store.get_state('r2z2_sequence'),
            'last_error': store.get_state('collector_last_error'),
            'last_success': store.get_state('collector_last_success')}

def overview(days=30):
    rows = records(days)
    systems, values, activity = Counter(), Counter(), Counter()
    pilots = set()
    for k in rows:
        sid = k['solar_system_id']
        systems[sid] += 1
        values[sid] += float(k.get('zkb', {}).get('totalValue', 0) or 0)
        activity[dt(k['killmail_time']).hour] += 1
        for p in [k['victim']] + k.get('attackers', []):
            if p.get('character_id'): pilots.add(p['character_id'])
    return {
        'total_kills': len(rows), 'total_value': sum(values.values()),
        'active_systems': len(systems), 'active_pilots': len(pilots),
        'last_observed_at': max((k['observed_at'] for k in rows), default=None),
        'data_mode': store.get_state('data_mode', store.get_state('npc_data_mode', 'empty')),
        'activity': [{'hour': h, 'kills': activity[h]} for h in range(24)],
        'systems': [{'id': s, 'name': name(s, "system"), 'kills': n, 'value': values[s]} for s, n in systems.most_common(20)],
        'recent': [dict(evidence(k), system_id=k['solar_system_id'], system_name=name(k['solar_system_id'], 'system'),
                        victim_ship=name(k['victim'].get('ship_type_id'), 'ship'), victim_name=name(k['victim'].get('character_id'), 'pilot'),
                        value=float(k.get('zkb', {}).get('totalValue', 0) or 0)) for k in rows[:30]],
        'collector': collector_status(), 'limitations': limitations(rows),
    }

def profile(kind, entity_id, days=30):
    field = {'character': 'character_id', 'corporation': 'corporation_id', 'alliance': 'alliance_id'}[kind]
    rows = records(days)
    kills = losses = 0
    ships, systems, activity, associates, weights = Counter(), Counter(), Counter(), Counter(), Counter()
    links = []
    for k in rows:
        attackers = k.get('attackers', [])
        own = [p for p in attackers if p.get(field) == entity_id]
        lost = k['victim'].get(field) == entity_id
        if not own and not lost: continue
        kills += bool(own)
        losses += lost
        systems[k['solar_system_id']] += 1
        activity[dt(k['killmail_time']).hour] += 1
        for p in own + ([k['victim']] if lost else []):
            if p.get('ship_type_id'): ships[p['ship_type_id']] += 1
        # Same-side co-occurrence only; never classify the victim as an associate.
        if own:
            others = {p['character_id'] for p in attackers if p.get('character_id') and p.get(field) != entity_id}
            fleet_size = len({p.get('character_id') for p in attackers if p.get('character_id')})
            for cid in others:
                associates[cid] += 1
                weights[cid] += 1 / max(1, fleet_size - 1)
        if len(links) < 30: links.append(evidence(k))
    return {'id': entity_id, 'name': name(entity_id, "pilot" if kind=="character" else kind), 'kind': kind, 'kills': kills, 'losses': losses,
            'activity': [{'hour': h, 'kills': activity[h]} for h in range(24)],
            'ships': [{'id': i, 'name': name(i, 'ship'), 'count': n} for i, n in ships.most_common(15)],
            'systems': [{'id': i, 'name': name(i, 'system'), 'count': n} for i, n in systems.most_common(15)],
            'associates': [{'id': i, 'name': name(i, 'pilot'), 'count': associates[i], 'weighted_score': round(w, 3)} for i, w in weights.most_common(15)],
            'evidence': links, 'limitations': limitations(rows) + ['Hours are UTC combat observations. Ship counts are participant appearances.', 'Associations are downweighted for large attacker groups; repeated kills in one battle are still correlated.']}

def doctrines(days=30):
    groups = {}
    rows = records(days)
    for k in rows:
        # ESI flags: low/medium/high/rig/subsystem slots. Cargo/drones excluded.
        modules = Counter()
        for item in k['victim'].get('items', []):
            flag = item.get('flag', 0)
            if 11 <= flag <= 34 or 92 <= flag <= 99 or 125 <= flag <= 132:
                modules[item['item_type_id']] += int(item.get('quantity_destroyed', 0) or 0) + int(item.get('quantity_dropped', 0) or 0)
        if not modules: continue
        ship = k['victim']['ship_type_id']
        signature = '|'.join(f'{i}:{q}' for i, q in sorted(modules.items()))
        key = ship, signature
        if key not in groups:
            groups[key] = {'ship_id': ship, 'ship_name': name(ship, 'ship'), 'signature': signature,
                           'modules': [f'{name(i, "item")} ×{q}' for i, q in sorted(modules.items())], 'losses': 0, 'evidence': []}
        g = groups[key]
        g['losses'] += 1
        if len(g['evidence']) < 5: g['evidence'].append(evidence(k))
    return {'items': sorted(groups.values(), key=lambda g: g['losses'], reverse=True)[:30],
            'limitations': limitations(rows) + ['Exact fitted-module fingerprints of lost ships; these are candidate patterns, not confirmed fleet doctrines.', 'No complete attacker fittings are inferred. Cargo, drones, and ammunition bays are excluded.']}
