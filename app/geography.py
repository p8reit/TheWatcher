"""Background ESI geography cache and local-only location lookup."""
from contextlib import closing
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import threading
import time

import httpx

from . import store, names

BASE_URL = 'https://esi.evetech.net/latest/universe'
LOCATION_CATEGORIES = {'system': 'system', 'solar_system': 'system',
                       'constellation': 'constellation', 'region': 'region'}


def enabled():
    return os.getenv('GEOGRAPHY_RESOLVER_ENABLED', 'true').lower() == 'true' and names.enabled() and store.get_state('data_mode', store.get_state('npc_data_mode', 'empty')) != 'demo'


def discover():
    """Queue known location IDs from ESI name work and cached entities."""
    if not enabled():
        return
    with closing(store.connect()) as c, c:
        rows = c.execute("""SELECT id FROM name_queue
            WHERE category IN ('system','solar_system')
            UNION SELECT id FROM entities
            WHERE category IN ('system','solar_system')""").fetchall()
        c.executemany('INSERT OR IGNORE INTO geography_queue(system_id) VALUES (?)',
                      [(int(r[0]),) for r in rows if int(r[0]) > 0])


def _valid_id(value):
    return type(value) is int and value > 0


def _known_parents(constellation_id):
    """Return cached (constellation name, region ID, region name), if available."""
    with closing(store.connect()) as c:
        row = c.execute('''SELECT constellation_name,region_id,region_name
            FROM geography_systems WHERE constellation_id=? LIMIT 1''', (constellation_id,)).fetchone()
        if row:
            return row['constellation_name'], row['region_id'], row['region_name']
    return None


class Resolver:
    def __init__(self, client, sleep=time.sleep, stop=None):
        self.client, self.sleep, self.stop = client, sleep, stop

    def _request(self, url):
        if self.stop and self.stop.is_set():
            raise _Stopped()
        self.sleep(.25)
        if self.stop and self.stop.is_set():
            raise _Stopped()
        response = self.client.get(url)
        response.raise_for_status()
        return response.json()

    def _resolve(self, system_id):
        system = self._request(f'{BASE_URL}/systems/{system_id}/')
        if (not isinstance(system, dict) or not _valid_id(system.get('system_id')) or system['system_id'] != system_id
                or not isinstance(system.get('name'), str) or not system['name'].strip()):
            raise ValueError('Invalid ESI system response')
        constellation_id = system.get('constellation_id')
        if not _valid_id(constellation_id):
            raise ValueError('Invalid ESI system parent')

        parents = _known_parents(constellation_id)
        if parents:
            constellation_name, region_id, region_name = parents
        else:
            constellation = self._request(f'{BASE_URL}/constellations/{constellation_id}/')
            if (not isinstance(constellation, dict) or not _valid_id(constellation.get('constellation_id'))
                    or constellation['constellation_id'] != constellation_id
                    or not isinstance(constellation.get('name'), str) or not constellation['name'].strip()):
                raise ValueError('Invalid ESI constellation response')
            systems = constellation.get('systems')
            if not isinstance(systems, list) or not all(_valid_id(i) for i in systems) or system_id not in systems:
                raise ValueError('ESI constellation does not contain the requested system')
            region_id = constellation.get('region_id')
            if not _valid_id(region_id):
                raise ValueError('Invalid ESI constellation parent')
            constellation_name = constellation['name']
            region = self._request(f'{BASE_URL}/regions/{region_id}/')
            if (not isinstance(region, dict) or not _valid_id(region.get('region_id')) or region['region_id'] != region_id
                    or not isinstance(region.get('name'), str) or not region['name'].strip()):
                raise ValueError('Invalid ESI region response')
            constellations = region.get('constellations')
            if (not isinstance(constellations, list) or not all(_valid_id(i) for i in constellations)
                    or constellation_id not in constellations):
                raise ValueError('ESI region does not contain the requested constellation')
            region_name = region['name']

        with closing(store.connect()) as c, c:
            c.execute('''INSERT OR REPLACE INTO geography_systems
                (system_id,system_name,constellation_id,constellation_name,region_id,region_name,updated_at)
                VALUES (?,?,?,?,?,?,?)''',
                      (system_id, system['name'].strip(), constellation_id, constellation_name,
                       region_id, region_name, store.utcnow()))
            c.execute('UPDATE geography_queue SET attempts=0,next_attempt=? WHERE system_id=?',
                      (time.time() + 2592000, system_id))

    def _defer(self, system_id, seconds):
        with closing(store.connect()) as c, c:
            c.execute('''UPDATE geography_queue SET attempts=attempts+1,next_attempt=?
                WHERE system_id=?''', (time.time() + seconds, system_id))

    def once(self):
        if not enabled() or time.time() < store.get_state('geography_retry_after', 0):
            return 0
        discover()
        with closing(store.connect()) as c:
            row = c.execute('''SELECT system_id FROM geography_queue WHERE next_attempt<=?
                ORDER BY next_attempt,system_id LIMIT 1''', (time.time(),)).fetchone()
        if not row or (self.stop and self.stop.is_set()):
            return 0
        system_id = row['system_id']
        try:
            self._resolve(system_id)
            store.set_state('geography_last_error', None)
            store.set_state('geography_last_success', store.utcnow())
            return 1
        except _Stopped:
            return 0
        except (httpx.HTTPError, ValueError) as exc:
            seconds = 300
            if isinstance(exc, httpx.HTTPStatusError):
                value = exc.response.headers.get('Retry-After', '')
                try:
                    seconds = max(seconds, float(value))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(value)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        seconds = max(seconds, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            seconds = min(86400, seconds)
            self._defer(system_id, seconds)
            store.set_state('geography_retry_after', time.time() + seconds)
            store.set_state('geography_last_error', 'Geography lookup deferred: ' + type(exc).__name__)
            return 0


class _Stopped(Exception):
    pass


def all_locations():
    """All locally known locations, suitable for analyst name resolution."""
    found = {}
    with closing(store.connect()) as c:
        for row in c.execute("SELECT id FROM name_queue WHERE category IN ('system','solar_system')"):
            found[('system', int(row['id']))] = 'System name unavailable'
        for row in c.execute('SELECT system_id,system_name,constellation_id,constellation_name,region_id,region_name FROM geography_systems'):
            found[('system', int(row['system_id']))] = row['system_name']
            found[('constellation', int(row['constellation_id']))] = row['constellation_name']
            found[('region', int(row['region_id']))] = row['region_name']
        for row in c.execute("SELECT id,name,category FROM entities WHERE category IN ('system','solar_system','constellation','region')"):
            kind = LOCATION_CATEGORIES[row['category']]
            found[(kind, int(row['id']))] = row['name']
    return [{'id': ident, 'name': name, 'kind': kind}
            for (kind, ident), name in sorted(found.items(), key=lambda item: (item[0][0], item[1].casefold(), item[0][1]))]


def locations(query='', kind=None):
    needle = str(query or '').casefold()
    values = [x for x in all_locations() if (kind is None or x['kind'] == kind)
              and (needle in x['name'].casefold() or needle in str(x['id']))]
    return sorted(values, key=lambda x: (x['name'].casefold(), x['kind'], x['id']))[:50]


def status():
    with closing(store.connect()) as c:
        pending = c.execute('SELECT COUNT(*) FROM geography_queue WHERE next_attempt<=?',
                            (time.time(),)).fetchone()[0]
        cached = c.execute('SELECT COUNT(*) FROM geography_systems').fetchone()[0]
    return {'enabled': enabled(), 'pending': pending, 'cached': cached,
            'last_error': store.get_state('geography_last_error')}


def worker(stop):
    """Resolve geography independently from entity-name requests."""
    ua = os.getenv('USER_AGENT', '')
    with httpx.Client(headers={'User-Agent': ua, 'Accept-Encoding': 'gzip'}, timeout=10,
                      trust_env=True, follow_redirects=False) as client:
        resolver = Resolver(client, stop=stop)
        while not stop.is_set():
            try:
                if enabled() and time.time() >= store.get_state('geography_retry_after', 0):
                    resolver.once()
            except Exception as exc:
                store.set_state('geography_last_error', 'Geography lookup deferred: ' + type(exc).__name__)
            stop.wait(5)


def start():
    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(stop,), daemon=True, name='esi-geography')
    thread.start()
    return stop, thread
