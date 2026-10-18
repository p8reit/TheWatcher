from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import importlib.util
import json
from pathlib import Path
import pytest

SPEC=importlib.util.spec_from_file_location('npc_intel_export',Path(__file__).resolve().parents[1]/'npc_intel_export.py')
module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(module)


def test_disabled_export_does_not_touch_files(tmp_path,monkeypatch):
    monkeypatch.delenv('HUNTER_NPC_SNAPSHOT_DIR',raising=False)
    assert module.capture_snapshot([], {}) is False
    assert list(tmp_path.iterdir())==[]


def test_exclusive_snapshot_receipt_and_conflict(tmp_path):
    now=datetime.now(timezone.utc).replace(microsecond=0)
    headers={'Last-Modified':format_datetime(now-timedelta(minutes=5),usegmt=True)}
    rows=[{'system_id':30000142,'npc_kills':123,'ship_kills':0,'pod_kills':0}]
    assert module.capture_snapshot(rows,headers,now,tmp_path)
    assert not module.capture_snapshot(rows,headers,now+timedelta(seconds=1),tmp_path)
    data=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert data['observed_at']==now.isoformat() and data['quality']=='verified'
    with pytest.raises(ValueError):module.capture_snapshot([dict(rows[0],npc_kills=999)],headers,now,tmp_path)
    assert len(list(tmp_path.iterdir()))==1


def test_invalid_capture_writes_nothing(tmp_path):
    with pytest.raises(ValueError):module.capture_snapshot([],{},directory=tmp_path)
    assert list(tmp_path.iterdir())==[]
