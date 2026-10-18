"""Durable local event storage. Receipt time is never rewritten on replay."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def connect(db_path=None):
    path = db_path or os.getenv('DATABASE_PATH', './data/intel.db')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def init_db():
    with closing(connect()) as conn, conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS killmails (
            id INTEGER PRIMARY KEY, killmail_time TEXT NOT NULL,
            observed_at TEXT NOT NULL, source TEXT NOT NULL, raw TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS killmail_time_idx ON killmails(killmail_time);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entities (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            category TEXT NOT NULL, updated_at TEXT NOT NULL);
        ''')


def get_state(key, default=None):
    with closing(connect()) as conn:
        row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default


def set_state(key, value):
    with closing(connect()) as conn, conn:
        conn.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))


def ingest_killmail(payload, source='r2z2', observed_at=None):
    if not isinstance(payload, dict):
        raise ValueError('Killmail must be an object')
    if not isinstance(payload.get('victim'), dict) or not isinstance(payload.get('attackers'), list):
        raise ValueError('Full ESI victim and attackers required')
    def positive_id(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    victim = payload['victim']
    if not positive_id(victim.get('ship_type_id')):
        raise ValueError('Victim ship_type_id must be a positive integer')
    for actor in [victim] + payload['attackers']:
        if not isinstance(actor, dict):
            raise ValueError('Each participant must be an object')
        for field in ('character_id', 'corporation_id', 'alliance_id', 'ship_type_id'):
            if field in actor and not positive_id(actor[field]):
                raise ValueError(f'Invalid participant {field}')
    items = victim.get('items', [])
    if not isinstance(items, list):
        raise ValueError('Victim items must be a list')
    def validate_items(items):
        for item in items:
            if not isinstance(item, dict) or not positive_id(item.get('item_type_id')):
                raise ValueError('Each item requires a positive item_type_id')
            for field in ('flag', 'quantity_dropped', 'quantity_destroyed'):
                if field in item and (not isinstance(item[field], int) or item[field] < 0):
                    raise ValueError(f'Invalid item {field}')
            if 'items' in item:
                if not isinstance(item['items'], list):
                    raise ValueError('Nested items must be a list')
                validate_items(item['items'])
    validate_items(items)
    if 'zkb' in payload and not isinstance(payload['zkb'], dict):
        raise ValueError('zkb metadata must be an object')
    kid = int(payload['killmail_id'])
    system = int(payload['solar_system_id'])
    if kid <= 0 or system <= 0:
        raise ValueError('Invalid killmail or system ID')
    event = datetime.fromisoformat(payload['killmail_time'].replace('Z', '+00:00'))
    if event.tzinfo is None:
        raise ValueError('Killmail time must include timezone')
    event_text = event.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    received = observed_at or utcnow()
    received_dt = datetime.fromisoformat(received.replace('Z', '+00:00'))
    if received_dt.tzinfo is None:
        raise ValueError('Receipt time must include timezone')
    received = received_dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    mode = 'demo' if source == 'demo' else 'live'
    with closing(connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT value FROM state WHERE key='data_mode'").fetchone()
        existing = json.loads(row[0]) if row else None
        npc_mode = conn.execute("SELECT value FROM state WHERE key='npc_data_mode'").fetchone()
        if npc_mode and json.loads(npc_mode[0]) != mode:
            raise ValueError('Synthetic and live data cannot share a database; use a separate DATABASE_PATH')
        if existing and existing != mode:
            raise ValueError('Synthetic and live data cannot share a database; use a separate DATABASE_PATH')
        conn.execute("INSERT OR REPLACE INTO state VALUES ('data_mode',?)", (json.dumps(mode),))
        cur = conn.execute('INSERT OR IGNORE INTO killmails VALUES (?,?,?,?,?)',
                           (kid, event_text, received, source, json.dumps(payload)))
        return cur.rowcount == 1


def list_killmails(days=30, limit=1000):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat().replace('+00:00', 'Z')
    scope = [int(x.strip()) for x in os.getenv('ANALYSIS_SYSTEM_IDS', '').split(',') if x.strip()]
    clause = ' AND json_extract(raw, \'$.solar_system_id\') IN (' + ','.join('?' for _ in scope) + ')' if scope else ''
    with closing(connect()) as conn:
        rows = conn.execute('SELECT * FROM killmails WHERE killmail_time>=?' + clause + ' ORDER BY killmail_time DESC,id DESC LIMIT ?',
                            (cutoff, *scope, max(1, int(limit)))).fetchall()
    return [dict(json.loads(r['raw']), observed_at=r['observed_at'], source=r['source']) for r in rows]


def entity_name(id):
    with closing(connect()) as conn:
        row = conn.execute('SELECT name FROM entities WHERE id=?', (int(id),)).fetchone()
        return row['name'] if row else f'ID {id}'


def cache_entities(items):
    with closing(connect()) as conn, conn:
        conn.executemany('INSERT OR REPLACE INTO entities VALUES (?,?,?,?)',
                         [(int(x['id']), x['name'], x['category'], utcnow()) for x in items])
