"""Opt-in ESI source snapshots; unchanged cached results never become new samples."""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import time
import httpx
from . import npc_store, store

URL = 'https://esi.evetech.net/latest/universe/system_kills/'


def http_date(value):
    try:
        d = parsedate_to_datetime(value)
        if d.tzinfo is None: raise ValueError('HTTP date lacks timezone.')
        return d.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError) as e:
        raise ValueError('Missing or invalid Last-Modified/HTTP date.') from e


def delay(value, now, default=120):
    if not value: return default
    try: return max(60, int(value))
    except ValueError:
        try: return max(60,int((http_date(value)-now).total_seconds()))
        except ValueError: return default


class Collector:
    def __init__(self, client=None):
        if os.getenv('NPC_COLLECTOR_ENABLED','false').lower() != 'true':
            raise ValueError('Set NPC_COLLECTOR_ENABLED=true to collect ESI NPC snapshots.')
        if store.get_state('npc_data_mode') == 'demo' or store.get_state('data_mode') == 'demo':
            raise ValueError('Live collection cannot use a demo database.')
        ua = os.getenv('USER_AGENT','')
        if not any(s in ua for s in ('http','@')): raise ValueError('Set USER_AGENT with a contact email or project URL.')
        self.client = client or httpx.Client(timeout=30,headers={'User-Agent':ua,'Accept-Encoding':'gzip'},trust_env=False,follow_redirects=False)
        self.etag = None

    def poll(self, now=None):
        response = self.client.get(URL,headers={'If-None-Match':self.etag} if self.etag else {})
        now = now or datetime.now(timezone.utc)
        if response.status_code == 429 or response.status_code >= 500:
            store.set_state('npc_collector_error',f'ESI HTTP {response.status_code}; retry deferred.')
            return delay(response.headers.get('Retry-After'),now)
        if response.status_code == 304:
            store.set_state('npc_collector_error',None)
            store.set_state('npc_collector_success',now.isoformat())
            return delay(response.headers.get('Expires'),now)
        response.raise_for_status()
        end = http_date(response.headers.get('Last-Modified'))
        rows = response.json()
        if not isinstance(rows,list): raise ValueError('ESI snapshot must be an array.')
        scope = {int(s.strip()) for s in os.getenv('NPC_SYSTEM_IDS','').split(',') if s.strip()}
        if scope: rows = [r for r in rows if isinstance(r,dict) and r.get('system_id') in scope]
        npc_store.ingest_snapshot('esi',end.isoformat(),now.isoformat(),rows,metadata={
            'etag':response.headers.get('ETag'),'scope':sorted(scope),'endpoint':URL})
        # Advancing the cache validator before the durable write could lose a sample.
        self.etag = response.headers.get('ETag')
        store.set_state('npc_collector_error',None)
        store.set_state('npc_collector_success',now.isoformat())
        return delay(response.headers.get('Expires'),now)


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--once',action='store_true')
    args = parser.parse_args(); npc_store.init_db()
    try: collector = Collector()
    except ValueError as e: parser.exit(2,str(e)+'\n')
    failures = 0
    while True:
        try:
            wait = collector.poll(); failures = 0
        except (httpx.HTTPError,ValueError,KeyError,TypeError) as e:
            failures += 1
            store.set_state('npc_collector_error',type(e).__name__+': snapshot rejected; inspect source/configuration.')
            if args.once: raise
            wait = min(3600,120*2**min(failures,4))
        if args.once: break
        until = time.monotonic()+wait
        while time.monotonic()<until: time.sleep(min(60,max(0,until-time.monotonic())))

if __name__ == '__main__': main()
