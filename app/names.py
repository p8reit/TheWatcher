"""Persistent background ESI name resolution. Never modifies training observations."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import threading
import time
import httpx
from . import store

URL = 'https://esi.evetech.net/latest/universe/names/'


def enabled():
    ua = os.getenv('USER_AGENT','')
    return (os.getenv('NAME_RESOLVER_ENABLED','true').lower()=='true'
            and any(s in ua for s in ('http','@'))
            and store.get_state('data_mode',store.get_state('npc_data_mode','empty'))!='demo')


def discover():
    """Migrate existing history incrementally; newly ingested data queues itself."""
    if store.get_state('data_mode',store.get_state('npc_data_mode','empty'))=='demo': return
    cursor = store.get_state('names_discovery_cursor',0)
    npc_cursor = store.get_state('names_npc_discovery_cursor',0)
    with closing(store.connect()) as c, c:
        rows = c.execute('SELECT id,raw FROM killmails WHERE id>? ORDER BY id LIMIT 1000',(cursor,)).fetchall()
        refs = {}
        for r in rows: refs.update(store.killmail_names(json.loads(r['raw'])))
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='npc_observations'").fetchone():
            last = c.execute('SELECT COALESCE(MAX(id),0) FROM npc_batches').fetchone()[0]
            refs.update({r[0]:'system' for r in c.execute('SELECT DISTINCT system_id FROM npc_observations WHERE batch_id>? AND batch_id<=?',(npc_cursor,last))})
            c.execute("INSERT OR REPLACE INTO state VALUES ('names_npc_discovery_cursor',?)",(json.dumps(last),))
        store.queue_names(c, refs)
        if rows:
            c.execute("INSERT OR REPLACE INTO state VALUES ('names_discovery_cursor',?)",(json.dumps(rows[-1]['id']),))


class Resolver:
    def __init__(self, client, sleep=time.sleep, stop=None):
        self.client, self.sleep, self.stop = client, sleep, stop

    def defer(self, ids, seconds, unavailable=False):
        with closing(store.connect()) as c, c:
            c.executemany('UPDATE name_queue SET attempts=attempts+1,next_attempt=?,unavailable=? WHERE id=?',
                          [(time.time()+seconds,int(unavailable),i) for i in ids])

    def once(self):
        if not enabled() or time.time()<store.get_state('names_retry_after',0): return 0
        discover()
        with closing(store.connect()) as c:
            ids = [r[0] for r in c.execute('SELECT id FROM name_queue WHERE next_attempt<=? ORDER BY priority DESC,next_attempt,id LIMIT 100',(time.time(),))]
        pending = [ids] if ids else []
        resolved = 0
        # Bound poison-batch splitting as well as normal traffic.
        for _ in range(8):
            if not pending or (self.stop and self.stop.is_set()): break
            batch = pending.pop(0)
            self.sleep(.25)
            try:
                response = self.client.post(URL,json=batch)
                if response.status_code == 404:
                    if len(batch)>1:
                        mid=len(batch)//2
                        pending[0:0] = [batch[:mid],batch[mid:]]
                    else: self.defer(batch,86400,unavailable=True)
                    continue
                response.raise_for_status()
                raw = response.json()
                if not isinstance(raw,list): raise ValueError('Invalid ESI names response')
                items = []
                for item in raw:
                    if (not isinstance(item,dict) or type(item.get('id')) is not int
                            or item['id'] not in batch or not isinstance(item.get('name'),str)
                            or not item['name'].strip() or not isinstance(item.get('category'),str)):
                        raise ValueError('Invalid ESI name entry')
                    items.append(item)
                found = {i['id'] for i in items}
                if len(found)!=len(items): raise ValueError('Duplicate ESI names')
                store.cache_entities(items)
                with closing(store.connect()) as c, c:
                    c.executemany('UPDATE name_queue SET attempts=0,next_attempt=?,unavailable=0,priority=0 WHERE id=?',
                                  [(time.time()+2592000,i) for i in found])
                self.defer(set(batch)-found,86400,unavailable=True)
                resolved += len(items)
                store.set_state('names_last_error',None)
                store.set_state('names_last_success',store.utcnow())
            except (httpx.HTTPError,ValueError) as exc:
                seconds = 300
                if isinstance(exc,httpx.HTTPStatusError):
                    value=exc.response.headers.get('Retry-After','')
                    try: seconds=max(seconds,float(value))
                    except ValueError:
                        try: seconds=max(seconds,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                        except (ValueError,TypeError,OverflowError): pass
                seconds=min(86400,seconds)
                self.defer([i for group in [batch,*pending] for i in group],seconds)
                # Global pause prevents a new batch bypassing an ESI rate limit.
                store.set_state('names_retry_after',time.time()+seconds)
                store.set_state('names_last_error','Name lookup deferred: '+type(exc).__name__)
                break
        return resolved


def status():
    with closing(store.connect()) as c:
        pending=c.execute('SELECT COUNT(*) FROM name_queue WHERE id NOT IN (SELECT id FROM entities)').fetchone()[0]
        cached=c.execute('SELECT COUNT(*) FROM entities').fetchone()[0]
    return {'enabled':enabled(),'pending':pending,'cached':cached,
            'last_error':store.get_state('names_last_error'),'last_success':store.get_state('names_last_success')}


def worker(stop):
    ua=os.getenv('USER_AGENT','')
    with httpx.Client(headers={'User-Agent':ua,'Accept-Encoding':'gzip'},timeout=10,trust_env=False) as client:
        resolver=Resolver(client,stop=stop)
        while not stop.is_set():
            try:
                if enabled() and time.time()>=store.get_state('names_retry_after',0): resolver.once()
            except Exception as exc:
                store.set_state('names_last_error','Name lookup deferred: '+type(exc).__name__)
            stop.wait(5)


def start():
    stop=threading.Event()
    thread=threading.Thread(target=worker,args=(stop,),daemon=True,name='esi-names')
    thread.start()
    return stop,thread


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    store.init_db()
    if not enabled(): parser.exit(2,'Name resolution disabled, demo data, or missing USER_AGENT contact.\n')
    if time.time()<store.get_state('names_retry_after',0): parser.exit(2,'Name lookup is in retry backoff.\n')
    with httpx.Client(headers={'User-Agent':os.environ['USER_AGENT']},timeout=10,trust_env=False) as client:
        print(f'Resolved {Resolver(client).once()} names; remaining entries resolve in the web background worker.')
