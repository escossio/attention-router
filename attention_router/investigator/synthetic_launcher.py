"""One bounded, provider-free Investigator run for the VLAN 219 journal gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket

from .activity_journal import InvestigatorJournal
from .model import Query, SourceEvent
from .service import Budget, Investigator
from .simulated import MemoryAudit, MemoryStore, ScriptedRuntime, SimulatedConnector


def _source_ip(route_peer: str) -> str:
    # UDP connect selects a route/source address without sending a packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((route_peer, 9))
        return probe.getsockname()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--expected-source-ip", required=True)
    parser.add_argument("--route-peer", required=True)
    args = parser.parse_args()
    actual_ip = _source_ip(args.route_peer)
    if actual_ip != args.expected_source_ip:
        raise SystemExit("NETWORK_IDENTITY_MISMATCH")

    queries = (Query("zabbix", "inspect"), Query("tempo", "read_trace"))
    investigator = Investigator(
        ScriptedRuntime(queries),
        {
            "zabbix": SimulatedConnector("zabbix", {"inspect": {"state": "synthetic"}}),
            "tempo": SimulatedConnector("tempo", {"read_trace": {"state": "synthetic"}}),
        },
        MemoryStore(), MemoryAudit(), Budget(2, 10),
        authorized_tenants=frozenset({"synthetic"}),
        allowed_operations={
            "zabbix": frozenset({"inspect"}),
            "tempo": frozenset({"read_trace"}),
        },
        activity_sink=InvestigatorJournal(args.journal),
    )
    result = investigator.investigate(
        SourceEvent("synthetic", b"synthetic-only", "text/plain", "synthetic"),
    )
    print(json.dumps({
        "run_id": result.investigation_id,
        "status": result.status,
        "queries": result.query_count,
        "mode": "synthetic",
        "source_ip_verified": True,
    }, sort_keys=True))
    return 0 if result.status == "completed" and result.query_count == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
