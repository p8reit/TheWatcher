"""Explicit synthetic NPC pipeline examples, never real ratting intelligence."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import math
import random
from . import npc_store, store


def seed(now=None):
    npc_store.init_db()
    if store.get_state('data_mode') not in (None,'demo') or store.get_state('npc_data_mode') not in (None,'demo'):
        raise ValueError('NPC demo requires a separate demo database.')
    with closing(store.connect()) as c:
        if c.execute('SELECT COUNT(*) FROM npc_batches').fetchone()[0]: raise ValueError('NPC demo already has observations.')
    now = now or datetime.now(timezone.utc)
    end = (now-timedelta(minutes=10)).replace(minute=0,second=0,microsecond=0)
    rng = random.Random(77)
    for h in range(36*24):
        when = end-timedelta(hours=36*24-1-h)
        rows = [{'system_id':930001+i,'npc_kills':max(0,round(200+120*math.sin(when.hour*math.tau/24+i)+rng.gauss(0,9))),
                 'ship_kills':0,'pod_kills':0} for i in range(4)]
        npc_store.ingest_snapshot('demo',when.isoformat(),(when+timedelta(minutes=2)).isoformat(),rows,quality='synthetic')
    store.cache_entities([{'id':930001+i,'name':f'SYNTHETIC System {i+1}','category':'solar_system'} for i in range(4)])
    return 36*24

if __name__ == '__main__': print(f'Created {seed()} SYNTHETIC NPC snapshots.')
