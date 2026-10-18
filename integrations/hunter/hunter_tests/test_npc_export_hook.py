"""Exercise the real collector loop function without importing the Flask application."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import logging
import pytest

class StopCycle(BaseException): pass


def collector_namespace(response, capture):
    source = (Path(__file__).resolve().parents[1]/'test.py').read_text()
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='update_kill_data')
    def stop(*args): raise StopCycle()
    ns = {'time':SimpleNamespace(monotonic=lambda:1,sleep=stop), 'COLLECTOR_STATUS':Mock(),
          'ESI_ETAG':None,'requests':SimpleNamespace(get=Mock(return_value=response)),
          'capture_snapshot':capture,'logging':logging,'load_current_kill_data':lambda:response.json(),
          'get_db_connection':lambda:None,'record_service_heartbeat':Mock(),'record_heartbeat':Mock(),
          'COLLECTOR_HEARTBEAT_FILE':'ignored','COLLECTOR_INTERVAL':120}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'test.py','exec'),ns)
    return ns


def test_export_failure_keeps_primary_collection_successful():
    response=Mock(status_code=200)
    response.json.return_value=[{'system_id':30000142,'npc_kills':20,'ship_kills':0,'pod_kills':0}]
    ns=collector_namespace(response,Mock(side_effect=OSError('disk full')))
    with pytest.raises(StopCycle): ns['update_kill_data']()
    ns['record_heartbeat'].assert_called_once()
    assert ns['record_service_heartbeat'].call_args.args[:2]==('esi_collector',True)


def test_304_never_creates_another_snapshot():
    capture=Mock()
    ns=collector_namespace(Mock(status_code=304),capture)
    with pytest.raises(StopCycle): ns['update_kill_data']()
    capture.assert_not_called()
    ns['record_heartbeat'].assert_called_once()
