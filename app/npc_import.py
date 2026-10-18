"""Audit/import explicit Hunter exports; never connects to or writes Hunter MySQL."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from . import npc_store

MAX_BYTES = 50 * 1024 * 1024


def envelopes(path):
    p = Path(path)
    if p.stat().st_size > MAX_BYTES: raise ValueError('Import capped at 50 MiB; split exports into smaller files.')
    text = p.read_text(encoding='utf-8')
    try: parsed = json.loads(text)
    except json.JSONDecodeError: parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(parsed, dict): parsed = [parsed]
    if not isinstance(parsed, list): raise ValueError('Expected a JSON array, snapshot object or JSONL.')
    batches = []
    for item in parsed:
        if not isinstance(item, dict): raise ValueError('Each import entry must be an object.')
        if item.get('version') == 1 and 'rows' in item:
            if item.get('source') != 'esi' or item.get('quality') != 'verified':
                raise ValueError('Native exports require source=esi and quality=verified.')
            batches.append({k:item[k] for k in ('source','source_end','observed_at','rows','quality')})
        else:
            # Predictor samples do not preserve the ESI source epoch. Never relabel them verified.
            stamp = item.get('observed_at')
            npc_store.utc(stamp)
            batches.append({'source':'hunter_legacy','source_end':stamp,'observed_at':stamp,'quality':'legacy',
                            'rows':[{'system_id':item.get('system_id'),'npc_kills':item.get('npc_kills'),
                                     'ship_kills':0,'pod_kills':0}]})
    # Legacy rows share capture epochs: combine systems and dedupe identical repeated rows.
    grouped = {}
    for b in batches:
        key = (b['source'],npc_store.utc(b['source_end']).isoformat())
        if key not in grouped: grouped[key] = dict(b,rows=[])
        known = {r['system_id']:r for r in grouped[key]['rows']}
        for row in b['rows']:
            if row['system_id'] in known and known[row['system_id']] != row:
                raise ValueError('Conflicting duplicate import row.')
            known[row['system_id']] = row
        grouped[key]['rows'] = list(known.values())
    return list(grouped.values())


def audit(batches):
    counts = Counter()
    ends = []
    systems = defaultdict(list)
    for b in batches:
        end, received = npc_store.utc(b['source_end']), npc_store.utc(b['observed_at'])
        if end > received or received > datetime.now(timezone.utc): raise ValueError('Invalid source or receipt time.')
        ends.append(end)
        for r in b['rows']:
            npc_store.positive_int(r['system_id'])
            for key in ('npc_kills','ship_kills','pod_kills'): npc_store.positive_int(r.get(key),zero=True)
            systems[r['system_id']].append((end,r['npc_kills']))
        counts[b['quality']] += len(b['rows'])
    coverage = []
    for sid, points in sorted(systems.items())[:100]:
        points.sort()
        span = (points[-1][0]-points[0][0]).total_seconds()/3600
        gaps = sum((b[0]-a[0]).total_seconds()>4500 for a,b in zip(points,points[1:]))
        coverage.append({'system_id':sid,'samples':len(points),'span_hours':round(span,2),
                         'gaps_over_75_minutes':gaps,'equal_consecutive_counts':sum(a[1]==b[1] for a,b in zip(points,points[1:]))})
    return {'snapshots':len(batches),'observations_by_quality':dict(counts),
            'systems':len(systems),'system_coverage_first_100':coverage,
            'first_source_end':min(ends).isoformat() if ends else None,
            'last_source_end':max(ends).isoformat() if ends else None,
            'legacy_training_eligible':False,
            'warning':'Audit reports exported records only; this does not establish complete coverage or trustworthy provenance.'}


def import_file(path, apply=False):
    batches = envelopes(path)
    result = audit(batches)
    if apply:
        npc_store.init_db()
        result['inserted_snapshots'] = sum(npc_store.ingest_snapshot(**b) for b in batches)
    result['applied'] = apply
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path'); parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    try: print(json.dumps(import_file(args.path,args.apply),indent=2))
    except (ValueError,KeyError,TypeError,OSError) as e: parser.exit(2,f'Import failed: {e}\n')

if __name__ == '__main__': main()
