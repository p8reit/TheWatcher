"""Optional append-only ESI snapshots for offline New Eden Intel analysis.

No database writes or new dependencies. Disabled unless HUNTER_NPC_SNAPSHOT_DIR
is configured. Existing Hunter collection remains authoritative for its UI.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import tempfile


def capture_snapshot(rows, headers, observed_at=None, directory=None):
    directory = directory or os.getenv('HUNTER_NPC_SNAPSHOT_DIR')
    if not directory: return False
    end = parsedate_to_datetime(headers.get('Last-Modified',''))
    if end.tzinfo is None: raise ValueError('Last-Modified must include timezone.')
    end = end.astimezone(timezone.utc)
    received = observed_at or datetime.now(timezone.utc)
    if received.tzinfo is None or end > received: raise ValueError('Invalid source or receipt timestamp.')
    if not isinstance(rows,list) or len(rows)>20000: raise ValueError('Invalid snapshot rows.')
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError('Invalid system row.')
        values = {}
        for key in ('system_id','npc_kills','ship_kills','pod_kills'):
            val = row.get(key)
            if isinstance(val,bool) or not isinstance(val,int) or val < (1 if key=='system_id' else 0):
                raise ValueError('Invalid system ID or count.')
            values[key] = val
        if values['system_id'] in seen: raise ValueError('Duplicate system row.')
        seen.add(values['system_id']); clean.append(values)
    clean.sort(key=lambda r:r['system_id'])
    payload = {'version':1,'source':'esi','quality':'verified','source_end':end.isoformat(),
               'observed_at':received.astimezone(timezone.utc).isoformat(),'rows':clean}
    folder = Path(directory); folder.mkdir(parents=True,exist_ok=True)
    target = folder/('esi-'+end.strftime('%Y%m%dT%H%M%SZ')+'.json')
    fd, temporary = tempfile.mkstemp(prefix='.snapshot-',dir=folder)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as output:
            json.dump(payload,output,allow_nan=False); output.flush(); os.fsync(output.fileno())
        try: os.link(temporary,target)  # Atomic, exclusive publication; never replace first receipt.
        except FileExistsError:
            existing = json.loads(target.read_text(encoding='utf-8'))
            if existing.get('rows') != clean: raise ValueError('Conflicting source epoch replay.')
            return False
        return True
    finally:
        os.unlink(temporary)
