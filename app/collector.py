"""Opt-in R2Z2 collector. See https://zkillboard.com/api/docs/ ."""
import argparse
from datetime import datetime, timedelta, timezone
import os
import time
import httpx
from . import store

BASE = 'https://r2z2.zkillboard.com'


class FeedGap(RuntimeError):
    pass


class Collector:
    def __init__(self, client=None, sleep=time.sleep):
        ua = os.getenv('USER_AGENT', '')
        if not ua or not any(x in ua for x in ('http', '@')):
            raise ValueError('Set USER_AGENT to project name and maintainer URL or email')
        self.client = client or httpx.Client(headers={'User-Agent': ua, 'Accept-Encoding': 'gzip'}, timeout=45)
        self.sleep = sleep

    def request(self, method, url, **kwargs):
        for attempt in range(4):
            self.sleep(0.25)  # At most four sequential requests/sec, below feed limit.
            try:
                response = self.client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 3:
                        response.raise_for_status()
                    delay = response.headers.get('Retry-After', '0')
                    delay = float(delay) if delay.replace('.', '', 1).isdigit() else 0
                    self.sleep(min(60, max(delay, 2 ** (attempt + 1))))
                    continue
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                result = response.json()
                if isinstance(result, dict) and 'error' in result:
                    raise RuntimeError(f'Upstream error: {result["error"]}')
                return result
            except httpx.TransportError:
                if attempt == 3:
                    raise
                self.sleep(2 ** (attempt + 1))
        raise RuntimeError('Request retry limit reached')

    def pointer(self):
        value = self.request('GET', f'{BASE}/ephemeral/sequence.json')
        if not isinstance(value, dict) or not isinstance(value.get('sequence'), int):
            raise ValueError('Invalid R2Z2 sequence pointer')
        return value['sequence']

    @staticmethod
    def normalize(raw):
        if not isinstance(raw, dict):
            raise ValueError('Invalid feed object')
        # R2Z2 carries the full body under "esi"; retain support for older
        # "killmail" envelopes and flat ESI-format archive entries.
        wrappers = [key for key in ('esi', 'killmail') if key in raw]
        if len(wrappers) > 1:
            raise ValueError('Ambiguous feed object: multiple killmail bodies')
        if not wrappers:
            return raw
        body = raw[wrappers[0]]
        if not isinstance(body, dict):
            raise ValueError('Invalid wrapped killmail')
        payload = dict(body)
        if 'killmail_id' in raw:
            outer_id = raw['killmail_id']
            if not isinstance(outer_id, int) or isinstance(outer_id, bool) or outer_id <= 0:
                raise ValueError('Invalid envelope killmail ID')
            if 'killmail_id' in body and body['killmail_id'] != outer_id:
                raise ValueError('Envelope killmail ID does not match body')
            payload.setdefault('killmail_id', outer_id)
        payload['zkb'] = raw.get('zkb', body.get('zkb', {}))
        for key in ('uploaded_at', 'sequence_id'):
            if key in raw:
                payload[key] = raw[key]
        return payload

    def once(self):
        if store.get_state('data_mode') == 'demo':
            raise ValueError('Collector cannot run against synthetic data')
        seq = store.get_state('r2z2_sequence')
        if seq is None:
            seq = self.pointer()
            store.set_state('r2z2_sequence', seq)
            store.set_state('collector_started_at', store.utcnow())
        pending = store.get_state('collector_wait')
        if pending and pending.get('sequence') == seq and pending.get('retry_at'):
            retry_at = datetime.fromisoformat(pending['retry_at'])
            remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                self.sleep(min(60, remaining))
        raw = self.request('GET', f'{BASE}/ephemeral/{seq}.json')
        if raw is None:
            # A pointer ahead of a missing object does not prove permanent loss:
            # publication/cache propagation can briefly expose this ordering.
            self.sleep(6)
            latest = self.pointer()
            previous = pending if pending and pending.get('sequence') == seq else {}
            attempts = previous.get('attempts', 0) + 1
            interval = min(60, 6 * 2 ** min(attempts - 1, 4)) if latest > seq else 6
            now = datetime.now(timezone.utc)
            waiting = {'status': 'waiting_for_sequence' if latest > seq else 'caught_up',
                       'sequence': seq, 'pointer': latest, 'attempts': attempts,
                       'first_seen': previous.get('first_seen', now.isoformat()),
                       'last_checked': now.isoformat(), 'retry_seconds': interval,
                       'retry_at': (now + timedelta(seconds=interval - 6)).isoformat()}
            store.set_state('collector_wait', waiting)
            message = (f'R2Z2 sequence {seq} currently unavailable (HTTP 404); pointer is {latest}. '
                       f'Retrying the same sequence with a {interval}s polling interval; cursor retained.')
            store.set_state('collector_last_error', message if latest > seq else None)
            if latest > seq:
                print(message, flush=True)
            if interval > 6:
                self.sleep(interval - 6)
            return False
        if not isinstance(raw, dict):
            raise ValueError('Invalid feed object; cursor unchanged')
        for key in ('sequence_id', 'sequence'):
            if key in raw and (not isinstance(raw[key], int) or isinstance(raw[key], bool) or raw[key] != seq):
                raise FeedGap(f'Response sequence mismatch at {seq}; cursor unchanged')
        payload = self.normalize(raw)
        store.ingest_killmail(payload)
        # A crash between insert and cursor update replays safely through unique killmail ID.
        store.set_state('r2z2_sequence', seq + 1)
        store.set_state('collector_wait', None)
        store.set_state('collector_last_success', store.utcnow())
        store.set_state('collector_last_error', None)
        return True

    def enrich_names(self, limit=100):
        # Resolution belongs to the web worker; a names outage must never stop ingestion.
        from .names import discover
        discover()

    def reset_to_current(self):
        if store.get_state('data_mode') == 'demo':
            raise ValueError('Collector cannot run against synthetic data')
        old = store.get_state('r2z2_sequence')
        current = self.pointer()
        gap = {'previous_next_sequence': old, 'new_next_sequence': current,
               'skipped_sequence_start': old if old is not None and old < current else None,
               'skipped_sequence_end': current - 1 if old is not None and old < current else None,
               'reset_at': store.utcnow(), 'previous_success_at': store.get_state('collector_last_success'),
               'warning': 'Manual cursor reset. Any skipped interval may remain incomplete; backfill and verify coverage.'}
        # Preserve an audit record independently of the transient collector error.
        store.set_state('collector_last_manual_reset', gap)
        store.set_state('collector_gap_' + gap['reset_at'], gap)
        store.set_state('r2z2_sequence', current)
        store.set_state('collector_wait', None)
        print(f'MANUAL RESET: next sequence {old} -> {current}; skipped interval '
              f"{gap['skipped_sequence_start']}..{gap['skipped_sequence_end']}. " + gap['warning'])
        return gap

    def backfill(self, days, refresh=False):
        if not 1 <= days <= 90:
            raise ValueError('Backfill must be between 1 and 90 completed UTC days')
        if store.get_state('data_mode') == 'demo':
            raise ValueError('Backfill cannot run against synthetic data')
        today = datetime.now(timezone.utc).date()
        count = 0
        for offset in range(days, 0, -1):
            day = (today - timedelta(days=offset)).strftime('%Y%m%d')
            if not refresh and store.get_state(f'archive_{day}'):
                continue
            raw = self.request('GET', f'{BASE}/history/raw/{day}.json')
            if raw is None:
                raise FeedGap(f'Archive missing for {day}; backfill incomplete')
            if isinstance(raw, dict):
                if 'killmail_id' in raw or 'killmail' in raw or 'esi' in raw:
                    raw = [raw]
                elif all(str(key).isdigit() for key in raw):
                    raw = list(raw.values())
                else:
                    raise ValueError(f'Unknown archive wrapper for {day}; backfill incomplete')
            if not isinstance(raw, list):
                raise ValueError(f'Unexpected archive format for {day}; backfill incomplete')
            for record in raw:
                count += store.ingest_killmail(self.normalize(record), source='archive')
            store.set_state(f'archive_{day}', {'completed_at': store.utcnow(), 'records': len(raw)})
        return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--backfill-days', type=int, default=0)
    parser.add_argument('--refresh-archives', action='store_true', help='Re-fetch completed archive days')
    parser.add_argument('--reset-to-current', action='store_true', help='Explicitly skip unavailable feed interval; record audit warning')
    parser.add_argument('--follow', action='store_true', help='Continue live collection after backfill or reset')
    args = parser.parse_args()
    if args.refresh_archives and not args.backfill_days:
        parser.error('--refresh-archives requires --backfill-days')
    if args.backfill_days < 0 or args.backfill_days > 90:
        parser.error('--backfill-days must be between 1 and 90')
    store.init_db()
    if os.getenv('COLLECTOR_ENABLED', 'false').lower() != 'true':
        parser.exit(0, 'Collector disabled. Set COLLECTOR_ENABLED=true and USER_AGENT.\n')
    collector = Collector()
    try:
        if args.backfill_days:
            print(f'Imported {collector.backfill(args.backfill_days, refresh=args.refresh_archives)} killmails')
        if args.reset_to_current:
            collector.reset_to_current()
        if (args.backfill_days or args.reset_to_current) and not args.follow and not args.once:
            return
        cycles = 0
        while True:
            collector.once()
            cycles += 1
            if cycles % 100 == 0 or args.once:
                collector.enrich_names()
            if args.once:
                break
    except Exception as exc:
        store.set_state('collector_last_error', str(exc))
        raise
    finally:
        collector.client.close()


if __name__ == '__main__':
    main()
