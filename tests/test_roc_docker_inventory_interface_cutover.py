"""No-network safety checks for the ROC Zabbix Agent interface migration."""
from pathlib import Path
from tempfile import TemporaryDirectory
import importlib.util

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ops/observability/docker-inventory/containers/host_interface.py"
SPEC = importlib.util.spec_from_file_location("roc_interface_cutover", SOURCE)
cutover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover)


class FakeAPI:
    def __init__(self, host_id="10785", ip="10.45.0.2"):
        self.host_id = host_id
        self.iface = {
            "interfaceid": "84", "hostid": host_id,
            "type": "1", "main": "1", "useip": "1",
            "ip": ip, "dns": "", "port": "10050",
        }
        self.writes = []

    def call(self, method, payload):
        if method == "host.get":
            return [{"hostid": self.host_id, "host": cutover.HOST}]
        if method == "hostinterface.get":
            return [self.iface.copy()]
        if method == "hostinterface.update":
            self.writes.append(payload.copy())
            self.iface.update(payload)
            return {"interfaceids": [self.iface["interfaceid"]]}
        raise AssertionError("unexpected API method " + method)


def test_preflight_never_writes():
    api = FakeAPI()
    before = cutover.identify(api)
    assert before["ip"] == "10.45.0.2"
    assert api.writes == []
    assert not cutover.is_target(before)


def test_cutover_preserves_same_host_and_checks_rollback():
    api = FakeAPI()
    with TemporaryDirectory() as tmp:
        file = Path(tmp) / "checkpoint.json"
        assert cutover.apply(api, "10.45.0.2", file) == "CUTOVER_PASS"
        assert file.exists() and file.stat().st_mode & 0o777 == 0o600
        assert cutover.is_target(cutover.identify(api))
        assert api.writes[0]["useip"] == 0
        assert api.writes[0]["dns"] == "roc-agent2"
        assert api.writes[0]["ip"] == ""
        assert "hostid" not in api.writes[0]
        assert cutover.apply(api, "10.45.0.2", file) == "ALREADY_CONTAINERIZED"
        assert len(api.writes) == 1
        assert cutover.rollback(api, file) == "ROLLBACK_PASS"
        assert cutover.identify(api)["ip"] == "10.45.0.2"
        assert len(api.writes) == 2


def test_wrong_origin_is_fail_closed():
    api = FakeAPI()
    with TemporaryDirectory() as tmp:
        file = Path(tmp) / "checkpoint.json"
        with pytest.raises(RuntimeError, match="precondition"):
            cutover.apply(api, "10.45.0.22", file)
        assert api.writes == []
        assert not file.exists()


def test_existing_rollback_checkpoint_is_not_overwritten():
    api = FakeAPI()
    with TemporaryDirectory() as tmp:
        file = Path(tmp) / "checkpoint.json"
        file.write_text("keep-private-backup")
        with pytest.raises(RuntimeError, match="already exists"):
            cutover.apply(api, "10.45.0.2", file)
        assert file.read_text() == "keep-private-backup"
        assert api.writes == []


def test_wrong_interface_is_refused():
    api = FakeAPI()
    api.iface["main"] = "0"
    with pytest.raises(RuntimeError, match="ambiguous main"):
        cutover.identify(api)
    api.iface["main"] = "1"
    api.iface["port"] = "18103"
    with pytest.raises(RuntimeError, match="unexpected agent port"):
        cutover.identify(api)
