"""Append-only, source-timestamped NPC observations, isolated from killmails."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from . import store

MAX_ROWS = 200000
QUALITIES = {'verified', 'legacy', 'synthetic'}


def utc(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be an ISO string with timezone.')
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timestamp must include timezone.')
    return d.astimezone(timezone.utc)


def init_db():
    store.init_db()
    with closing(store.connect()) as c, c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS npc_batches (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_end TEXT NOT NULL,
            observed_at TEXT NOT NULL, quality TEXT NOT NULL, metadata TEXT NOT NULL,
            UNIQUE(source,source_end));
        CREATE TABLE IF NOT EXISTS npc_observations (
            batch_id INTEGER NOT NULL, system_id INTEGER NOT NULL, npc_kills INTEGER NOT NULL,
            ship_kills INTEGER NOT NULL, pod_kills INTEGER NOT NULL,
            PRIMARY KEY(batch_id,system_id));
        CREATE INDEX IF NOT EXISTS npc_source_end ON npc_batches(source_end);
        CREATE INDEX IF NOT EXISTS npc_system ON npc_observations(system_id,batch_id);
        ''')


def positive_int(value, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1) or value > 2**63-1:
        raise ValueError('IDs must be positive integers; counts must be nonnegative integers.')
    return value


def ingest_snapshot(source, source_end, observed_at, rows, quality='verified', metadata=None):
    if quality not in QUALITIES or source not in {'esi', 'hunter_legacy', 'demo'}:
        raise ValueError('Unknown snapshot source or quality.')
    if (source, quality) not in {('esi','verified'), ('hunter_legacy','legacy'), ('demo','synthetic')}:
        raise ValueError('Snapshot source and quality disagree.')
    end, received = utc(source_end), utc(observed_at)
    if end > received or received > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError('Source period must end by receipt time; future receipts are invalid.')
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError('Snapshot rows must be a list of at most 20,000 systems.')
    normalized = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Each system row must be an object.')
        sid = positive_int(row.get('system_id'))
        if sid in seen: raise ValueError('Duplicate system in snapshot.')
        seen.add(sid)
        normalized.append((sid, *(positive_int(row.get(k),zero=True) for k in ('npc_kills','ship_kills','pod_kills'))))
    normalized.sort()
    init_db()
    end_text, received_text = end.isoformat(), received.isoformat()
    with closing(store.connect()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        mode = c.execute("SELECT value FROM state WHERE key='npc_data_mode'").fetchone()
        wanted = 'demo' if quality == 'synthetic' else 'live'
        killmode = c.execute("SELECT value FROM state WHERE key='data_mode'").fetchone()
        if (mode and json.loads(mode[0]) != wanted) or (killmode and json.loads(killmode[0]) != wanted):
            raise ValueError('Synthetic and live data require separate databases.')
        previous = c.execute('SELECT id,quality FROM npc_batches WHERE source=? AND source_end=?',(source,end_text)).fetchone()
        if previous:
            old = [tuple(r) for r in c.execute('SELECT system_id,npc_kills,ship_kills,pod_kills FROM npc_observations WHERE batch_id=? ORDER BY system_id',(previous['id'],))]
            if previous['quality'] != quality or old != normalized:
                raise ValueError('Conflicting replay: snapshot already exists with different counts or scope.')
            return False
        cursor = c.execute('INSERT INTO npc_batches(source,source_end,observed_at,quality,metadata) VALUES (?,?,?,?,?)',
                           (source,end_text,received_text,quality,json.dumps(metadata or {},allow_nan=False)))
        c.executemany('INSERT INTO npc_observations VALUES (?,?,?,?,?)',[(cursor.lastrowid,*r) for r in normalized])
        c.execute("INSERT OR REPLACE INTO state VALUES ('npc_data_mode',?)",(json.dumps(wanted),))
    return True


def observations(days=90, quality='verified', limit=MAX_ROWS):
    if quality not in QUALITIES: raise ValueError('Unknown quality.')
    cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    scope = [positive_int(int(s.strip())) for s in os.getenv('NPC_SYSTEM_IDS','').split(',') if s.strip()]
    clause = ' AND o.system_id IN ('+','.join('?' for _ in scope)+')' if scope else ''
    with closing(store.connect()) as c:
        rows = c.execute('''SELECT b.source,b.source_end,b.observed_at,b.quality,o.system_id,o.npc_kills,o.ship_kills,o.pod_kills
            FROM npc_batches b JOIN npc_observations o ON o.batch_id=b.id
            WHERE b.source_end>=? AND b.quality=?'''+clause+' ORDER BY b.source_end,o.system_id LIMIT ?',
            (cutoff,quality,*scope,limit+1)).fetchall()
    if len(rows)>limit: raise ValueError('NPC analysis cap reached. Set NPC_SYSTEM_IDS to a smaller system scope.')
    return [dict(r) for r in rows]


def status():
    with closing(store.connect()) as c:
        totals = [dict(r) for r in c.execute('''SELECT b.quality,COUNT(DISTINCT b.id) AS snapshots,COUNT(o.system_id) AS observations,
            COUNT(DISTINCT o.system_id) AS systems,MIN(b.source_end) AS first_source_end,
            MAX(b.source_end) AS last_source_end,MAX(b.observed_at) AS last_received
            FROM npc_batches b LEFT JOIN npc_observations o ON o.batch_id=b.id GROUP BY b.quality''')]
    return {'data_mode':store.get_state('npc_data_mode','empty'),'sources':totals,
            'collector_enabled':os.getenv('NPC_COLLECTOR_ENABLED','false').lower()=='true',
            'last_error':store.get_state('npc_collector_error'), 'last_success':store.get_state('npc_collector_success'),
            'scope':os.getenv('NPC_SYSTEM_IDS',''),
            'limitations':['Only explicitly reported systems have observations; absent rows are unknown, not zero.',
            'Legacy Hunter history has uncertain source timestamps and is excluded from training.',
            'NPC activity does not identify the ratting pilot, ship, or exact site.']}
