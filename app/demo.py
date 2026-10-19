"""Explicit synthetic dataset; use a separate DATABASE_PATH from production."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import random
from . import store


def seed(now=None):
    store.init_db()
    with closing(store.connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT COUNT(*) FROM killmails').fetchone()[0] or store.get_state('data_mode'):
            raise ValueError('Demo requires an empty, unused database')
        npc_mode = conn.execute("SELECT value FROM state WHERE key='npc_data_mode'").fetchone()
        if npc_mode and npc_mode[0] == '"live"':
            raise ValueError('Demo cannot use a database with live NPC observations')
        conn.execute("INSERT INTO state VALUES ('data_mode', '\"demo\"')")
    rng = random.Random(42)
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=89)
    names = []
    for base, category, label, n in [(900001, 'character', 'Pilot', 12), (910001, 'corporation', 'Corp', 3),
                                   (920001, 'alliance', 'Alliance', 2), (930001, 'solar_system', 'System', 4),
                                   (940001, 'inventory_type', 'Hull', 3), (950001, 'inventory_type', 'Module', 6)]:
        names.extend({'id': base + i, 'category': category, 'name': f'SYNTHETIC {label} {i + 1}'} for i in range(n))
    store.cache_entities(names)
    count = 0
    for day in range(90):
        for j in range(rng.randint(6, 16)):
            when = start + timedelta(days=day, hours=rng.choice([0, 1, 2, 3, 12, 18, 20, 21]), minutes=rng.randrange(60))
            if when > now:
                continue
            victim = rng.randrange(12)
            attackers = rng.sample([x for x in range(12) if x != victim], rng.randint(1, 5))
            def actor(i):
                return {'character_id': 900001 + i, 'corporation_id': 910001 + i % 3,
                        'alliance_id': 920001 + i % 2, 'ship_type_id': 940001 + i % 3}
            v = actor(victim)
            v['items'] = [{'item_type_id': 950001 + victim % 3, 'flag': 27, 'quantity_destroyed': 2},
                          {'item_type_id': 950004 + victim % 3, 'flag': 19, 'quantity_dropped': 1}]
            count += 1
            payload = {'killmail_id': 800000000 + count, 'killmail_time': when.isoformat().replace('+00:00', 'Z'),
                       'solar_system_id': 930001 + rng.randrange(4), 'victim': v,
                       'attackers': [dict(actor(i), final_blow=k == 0) for k, i in enumerate(attackers)],
                       'zkb': {'totalValue': rng.randrange(10, 800) * 1000000, 'npc': False, 'synthetic': True}}
            observed = (when + timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
            store.ingest_killmail(payload, source='demo', observed_at=observed)
    ensure_geography(now)
    return count


def ensure_geography(now=None):
    """Add fictional topology to existing standard demos without reseeding events."""
    if store.get_state('data_mode') != 'demo':
        return
    now = now or datetime.now(timezone.utc)
    with closing(store.connect()) as conn, conn:
        for i in range(4):
            label = f'SYNTHETIC System {i+1}'
            if not conn.execute('SELECT 1 FROM entities WHERE id=? AND name=?', (930001+i, label)).fetchone():
                continue
            conn.execute('INSERT OR IGNORE INTO geography_systems VALUES (?,?,?,?,?,?,?)',
                (930001+i, label, 970001+i//2, f'SYNTHETIC Constellation {i//2+1}',
                 960001+i//2, f'SYNTHETIC Region {i//2+1}', now.isoformat()))


if __name__ == '__main__':
    print(f'Created {seed()} SYNTHETIC killmails. This database cannot collect live data.')
