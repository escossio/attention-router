import json
import os
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops/provisioning/andy-ops-panel"))
import tcpbrain_observability as m  # noqa: E402

NOW = 1800000000000000000
EPOCH = NOW - 60_000_000_000


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cap = {
            "schema_version": "tcp-brain.capture-status.v1",
            "started_timestamp_ns": EPOCH,
            "state": "HEALTHY",
            "carrier": True,
            "packets_received": 100,
            "events_emitted": 90,
            "socket_drop_count": 0,
            "interface_rx_drop_delta": 0,
            "interface_rx_missed_delta": 0,
            "parse_errors": 0,
            "subscriber_drops": 0,
            "freshness_seconds": 0.1,
            "current_bps": 1000000,
            "peak_bps": 2000000,
            "link_speed_mbps": 1000,
        }
        self.flow = {
            "schema_version": "tcp-brain.flow-status.v1",
            "started_timestamp_ns": EPOCH,
            "state": "HEALTHY",
            "inputs_configured": ["mirror", "trunk"],
            "inputs_connected": ["mirror", "trunk"],
            "input_events": 180,
            "active_flows": 3,
            "forward_ok_total": 5,
            "forward_missing_total": 0,
            "forward_ok_by_vlan": {"310->312": 5},
            "freshness_seconds": 0.2,
        }
        self.put("mirror", self.cap)
        self.put("trunk", self.cap)
        self.put("flow", self.flow)

    def tearDown(self):
        self.temp.cleanup()

    def path(self, n):
        return self.root / m.SOURCES[n][0]

    def put(self, n, d, age=0):
        p = self.path(n)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d))
        stamp = NOW - int(age * 1e9)
        os.utime(p, ns=(stamp, stamp))

    def sample(self):
        return m.sample_tcpbrain(self.root, NOW)

    def v2(self):
        for n in ("mirror", "trunk"):
            self.put(
                n,
                {
                    **self.cap,
                    "packet_statistics_drops": 0,
                    "subscriber_queue_overflows": 0,
                    "subscriber_write_errors": 0,
                    "subscriber_queue_records_discarded": 0,
                    "subscriber_backpressure": 0,
                },
            )
        c = {
            n: {"epoch": str(EPOCH), **{k: 0 for k in m.CONTINUITY_COUNTS}}
            for n in ("mirror", "trunk")
        }
        w = {
            n: {"fresh": True, "watermark_ns": NOW - 100000000, "start_ns": NOW - 2000000000}
            for n in c
        }
        self.put(
            "flow",
            {
                **self.flow,
                "stream_continuity": c,
                "coverage": w,
                "observation_state": "BOUNDED_COVERAGE",
            },
        )

    def test_legacy_not_certified(self):
        r = self.sample()
        self.assertEqual(r["sources"]["mirror"]["capture_health"], "NO_RECORDED_DROPS")
        self.assertEqual(r["sources"]["mirror"]["delivery_health"], "UNVERIFIED")
        self.assertEqual(r["flow"]["observation_health"], "LEGACY_UNVERIFIED")
        self.assertIsNone(r["sources"]["trunk"]["subscriber_queue_overflows"])

    def test_subscriber_and_physical_distinct(self):
        self.put("mirror", {**self.cap, "subscriber_drops": 11})
        s = self.sample()["sources"]["mirror"]
        self.assertEqual(s["capture_health"], "NO_RECORDED_DROPS")
        self.assertEqual(s["delivery_health"], "ISSUES_RECORDED")

    def test_physical_drop(self):
        self.put("mirror", {**self.cap, "interface_rx_drop_delta": 1})
        self.assertEqual(self.sample()["sources"]["mirror"]["capture_health"], "DROPS_RECORDED")

    def test_no_carrier(self):
        self.put("mirror", {**self.cap, "carrier": False})
        self.assertEqual(self.sample()["sources"]["mirror"]["capture_health"], "NO_CARRIER")

    def test_stale_null_not_zero(self):
        self.put("mirror", self.cap, age=6)
        r = self.sample()
        s = r["sources"]["mirror"]
        self.assertEqual(s["telemetry"], "STALE")
        self.assertIsNone(s["packets_received"])
        self.assertEqual(r["flow"]["observation_health"], "UNKNOWN")

    def test_missing(self):
        self.path("trunk").unlink()
        s = self.sample()["sources"]["trunk"]
        self.assertEqual(s["telemetry"], "UNAVAILABLE")
        self.assertIsNone(s["socket_drop_count"])

    def test_unknown_schema(self):
        self.put("flow", {**self.flow, "schema_version": "future"})
        self.assertEqual(self.sample()["flow"]["telemetry"], "INVALID")

    def test_invalid_json(self):
        p = self.path("mirror")
        p.write_text("{")
        os.utime(p, ns=(NOW, NOW))
        self.assertEqual(self.sample()["sources"]["mirror"]["telemetry"], "INVALID")

    def test_symlink_rejected(self):
        p = self.path("mirror")
        p.unlink()
        p.symlink_to(self.path("trunk"))
        self.assertEqual(self.sample()["sources"]["mirror"]["telemetry"], "UNAVAILABLE")

    def test_oversize(self):
        p = self.path("mirror")
        p.write_bytes(b" " * (m.MAX_BYTES + 1))
        os.utime(p, ns=(NOW, NOW))
        self.assertEqual(self.sample()["sources"]["mirror"]["telemetry"], "INVALID")

    def test_future_file(self):
        self.put("mirror", self.cap, age=-10)
        self.assertEqual(self.sample()["sources"]["mirror"]["telemetry"], "INVALID")

    def test_invalid_numeric(self):
        self.put("mirror", {**self.cap, "socket_drop_count": False, "subscriber_drops": -1})
        s = self.sample()["sources"]["mirror"]
        self.assertIsNone(s["socket_drop_count"])
        self.assertEqual(s["capture_health"], "UNKNOWN")
        self.assertIsNone(s["subscriber_drops"])

    def test_missing_input(self):
        self.put("flow", {**self.flow, "inputs_connected": ["trunk"]})
        self.assertEqual(self.sample()["flow"]["observation_health"], "INCOMPLETE_INPUTS")

    def test_bad_input_type(self):
        self.put("flow", {**self.flow, "inputs_connected": "trunk"})
        self.assertEqual(self.sample()["flow"]["observation_health"], "UNKNOWN")

    def test_positive_forward_counter(self):
        self.assertEqual(
            self.sample()["flow"]["forward_pairs"], [{"from_vlan": 310, "to_vlan": 312, "count": 5}]
        )

    def test_private_fields_not_serialized(self):
        self.put(
            "mirror",
            {
                **self.cap,
                "last_event": {"src_ip": "198.51.100.55", "payload": "PRIVATE_FIXTURE"},
                "interface": "PRIVATE_FIXTURE",
            },
        )
        self.put(
            "flow",
            {
                **self.flow,
                "last_anomaly": {"description": "PRIVATE_FIXTURE"},
                "forward_ok_by_vlan": {"PRIVATE_FIXTURE": 7},
            },
        )
        r = self.sample()
        self.assertIsNone(r["flow"]["forward_pairs"])
        self.assertNotIn("PRIVATE_FIXTURE", json.dumps(r))
        self.assertNotIn("198.51.100.55", json.dumps(r))

    def test_v2_bounded_window(self):
        self.v2()
        r = self.sample()
        self.assertEqual(r["flow"]["observation_health"], "BOUNDED_COVERAGE")
        self.assertEqual(r["sources"]["mirror"]["delivery_health"], "NO_ERRORS_RECORDED")

    def test_v2_epoch_mismatch(self):
        self.v2()
        d = json.loads(self.path("trunk").read_text())
        d["started_timestamp_ns"] += 1
        self.put("trunk", d)
        self.assertEqual(self.sample()["flow"]["observation_health"], "INCONCLUSIVE")

    def test_v2_expired_watermark(self):
        self.v2()
        d = json.loads(self.path("flow").read_text())
        d["coverage"]["trunk"]["watermark_ns"] = NOW - 2_000_000_000
        self.put("flow", d)
        self.assertEqual(self.sample()["flow"]["observation_health"], "INCONCLUSIVE")

    def test_v2_no_stats_not_healthy(self):
        self.v2()
        d = json.loads(self.path("trunk").read_text())
        del d["packet_statistics_drops"]
        self.put("trunk", d)
        self.assertEqual(self.sample()["sources"]["trunk"]["capture_health"], "UNKNOWN")

    def test_v2_overflow(self):
        self.v2()
        d = json.loads(self.path("mirror").read_text())
        d["subscriber_queue_overflows"] = 1
        self.put("mirror", d)
        self.assertEqual(self.sample()["sources"]["mirror"]["delivery_health"], "ISSUES_RECORDED")

    def test_v2_missing_backpressure_not_healthy(self):
        self.v2()
        d = json.loads(self.path("mirror").read_text())
        del d["subscriber_backpressure"]
        self.put("mirror", d)
        self.assertEqual(self.sample()["sources"]["mirror"]["delivery_health"], "UNKNOWN")

    def test_v2_carrier_down_invalidates_window(self):
        self.v2()
        d = json.loads(self.path("trunk").read_text())
        d["carrier"] = False
        self.put("trunk", d)
        self.assertEqual(self.sample()["flow"]["observation_health"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
