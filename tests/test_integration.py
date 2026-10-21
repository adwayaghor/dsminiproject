"""Integration tests: 4 REAL station processes talking real HTTP on localhost."""
import json
import unittest
import urllib.request

from dcn import scenarios
from dcn.controllers import LocalCluster
from dcn.netclient import http_json

INC = {"title": "Flooded road", "description": "", "category": "flood", "severity": "high",
       "location": "Sector 2"}
REQ = {"resource_type": "drinking water", "quantity": 100, "priority": "urgent"}


class ClusterTestCase(unittest.TestCase):
    lc: LocalCluster

    @classmethod
    def setUpClass(cls):
        cls.lc = LocalCluster()
        cls.lc.start_all()
        cls.c = cls.lc.cluster

    @classmethod
    def tearDownClass(cls):
        cls.lc.stop_all()

    def setUp(self):
        self.c.heal_all()
        self.c.reset_all()


class HttpValidationTests(ClusterTestCase):
    def test_invalid_payload_400(self):
        st, b = self.c.create("alpha", "incident", dict(INC, severity="extreme"))
        self.assertEqual(st, 400)
        self.assertEqual(b["error"], "validation")
        self.assertEqual(self.c.call("alpha", "GET", "/records")[1]["records"], [])

    def test_non_json_and_wrong_content_type(self):
        url = self.c.urls["alpha"] + "/incidents"
        req = urllib.request.Request(url, data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 400)
        req = urllib.request.Request(url, data=b"x=1", method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 415)

    def test_unknown_route_and_record(self):
        self.assertEqual(self.c.call("alpha", "GET", "/nope")[0], 404)
        self.assertEqual(self.c.call("alpha", "GET", "/records/INC-ALPHA-00000000")[0], 404)

    def test_sync_endpoints_require_known_peer(self):
        st, _ = self.c.call("alpha", "POST", "/sync/push", {"records": []})
        self.assertEqual(st, 403)
        st, _ = http_json("POST", self.c.urls["alpha"] + "/sync/push", {"records": []},
                          {"X-Node-Id": "mallory"})
        self.assertEqual(st, 403)

    def test_forged_record_rejected_by_peer(self):
        st, rec = self.c.create("alpha", "incident", INC)
        rec["payload"]["severity"] = "low"      # tamper without fixing digest
        st, b = http_json("POST", self.c.urls["bravo"] + "/sync/push",
                          {"records": [rec]}, {"X-Node-Id": "alpha"})
        self.assertEqual(st, 200)
        self.assertEqual(b["stats"]["invalid"], 1)
        self.assertEqual(self.c.call("bravo", "GET", "/records")[1]["records"], [])

    def test_illegal_status_transition_409(self):
        _, r = self.c.create("alpha", "request", REQ)
        st, b = self.c.update("alpha", r["id"], {"status": "fulfilled"})
        self.assertEqual((st, b["error"]), (409, "invalid_transition"))


class SyncTests(ClusterTestCase):
    def test_local_write_is_not_replicated_until_sync(self):
        _, rec = self.c.create("alpha", "incident", INC)
        self.assertEqual(self.c.call("bravo", "GET", "/records")[1]["records"], [])
        st, b = self.c.sync_node("alpha", ["bravo"])
        self.assertEqual(b["results"][0]["outcome"], "success")
        got = self.c.call("bravo", "GET", f"/records/{rec['id']}")[1]
        self.assertEqual(got["origin"], "alpha")          # replicated copy keeps its origin
        self.assertEqual(got["received_from"], "alpha")

    def test_two_way_exchange(self):
        _, a = self.c.create("alpha", "incident", INC)
        _, b = self.c.create("bravo", "request", REQ)
        self.c.sync_node("alpha", ["bravo"])
        for n in ("alpha", "bravo"):
            ids = {r["id"] for r in self.c.call(n, "GET", "/records")[1]["records"]}
            self.assertEqual(ids, {a["id"], b["id"]})

    def test_repeated_sync_creates_no_duplicates(self):
        self.c.create("alpha", "incident", INC)
        for _ in range(3):
            self.c.sync_network()
        for n in self.c.nodes:
            self.assertEqual(len(self.c.call(n, "GET", "/records")[1]["records"]), 1)

    def test_duplicate_delivery_ignored_and_counted(self):
        _, rec = self.c.create("alpha", "incident", INC)
        self.c.sync_network()
        st, b = self.c.call("alpha", "POST", "/admin/redeliver",
                            {"peer": "bravo", "record_id": rec["id"]})
        self.assertEqual(b["reply"]["applied"][rec["id"]], "duplicate")
        self.assertGreaterEqual(self.c.call("bravo", "GET", "/status")[1]["counters"]["duplicates_ignored"], 1)
        self.assertEqual(len(self.c.call("bravo", "GET", "/records")[1]["records"]), 1)

    def test_newer_update_not_overwritten_by_older(self):
        _, rec = self.c.create("alpha", "incident", INC)
        self.c.sync_network()
        self.c.update("alpha", rec["id"], {"severity": "critical"})   # v2 at alpha
        old = self.c.call("bravo", "GET", f"/records/{rec['id']}")[1]  # v1 copy
        self.c.sync_network()
        # re-deliver the OLD v1 copy to alpha: must be ignored as stale
        st, b = http_json("POST", self.c.urls["alpha"] + "/sync/push",
                          {"records": [{k: old[k] for k in old if k not in ("received_from", "received_at")}]},
                          {"X-Node-Id": "bravo"})
        self.assertEqual(b["applied"][rec["id"]], "stale")
        self.assertEqual(self.c.call("alpha", "GET", f"/records/{rec['id']}")[1]["version"], 2)

    def test_pending_counter_is_real(self):
        self.c.create("alpha", "incident", INC)
        st = self.c.call("alpha", "GET", "/status")[1]
        self.assertEqual(st["pending_total"], 3)               # 1 record x 3 peers
        self.c.sync_network()
        self.assertEqual(self.c.call("alpha", "GET", "/status")[1]["pending_total"], 0)


class PartitionTests(ClusterTestCase):
    def test_local_writes_continue_and_sync_is_skipped(self):
        self.c.isolate("alpha")
        st, rec = self.c.create("alpha", "request", REQ)
        self.assertEqual(st, 201)
        _, b = self.c.sync_node("alpha")
        self.assertEqual({r["outcome"] for r in b["results"]}, {"skipped_partition"})
        self.assertEqual(self.c.call("bravo", "GET", "/records")[1]["records"], [])

    def test_one_sided_block_is_enforced_by_remote_rejection(self):
        # Only Bravo knows about the partition; Alpha tries and is rejected over real HTTP.
        self.c.call("bravo", "POST", "/admin/links", {"peer": "alpha", "blocked": True})
        self.c.create("alpha", "incident", INC)
        _, b = self.c.sync_node("alpha", ["bravo"])
        self.assertEqual(b["results"][0]["outcome"], "rejected_partition")
        self.assertGreaterEqual(self.c.call("bravo", "GET", "/status")[1]["counters"]["partition_rejections"], 1)
        self.assertEqual(self.c.call("bravo", "GET", "/records")[1]["records"], [])

    def test_peers_probe_distinguishes_partition_from_crash(self):
        self.c.set_link("alpha", "bravo", True)
        self.lc.stop("charlie")
        try:
            p = self.c.call("alpha", "GET", "/peers", timeout=8)[1]
            self.assertEqual(p["bravo"]["state"], "partitioned")
            self.assertEqual(p["charlie"]["state"], "unreachable")
            self.assertEqual(p["delta"]["state"], "reachable")
        finally:
            self.lc.start("charlie")
            self.c.wait_online("charlie")

    def test_recovery_after_reconnect(self):
        self.c.isolate("alpha")
        _, rec = self.c.create("alpha", "incident", INC)
        self.c.sync_network()
        self.assertIsNone(self.c.call("delta", "GET", f"/records/{rec['id']}")[1].get("id"))
        self.c.heal_all()
        s = self.c.sync_network()
        self.assertTrue(s["convergence"]["converged"])
        for n in self.c.nodes:
            self.assertEqual(self.c.call(n, "GET", f"/records/{rec['id']}")[0], 200)


class RestartTests(ClusterTestCase):
    def test_state_survives_hard_kill_and_restart(self):
        _, rec = self.c.create("bravo", "incident", INC)
        self.c.update("bravo", rec["id"], {"severity": "critical"})
        self.lc.stop("bravo", kill=True)                       # SIGKILL: no clean shutdown
        self.assertIsNotNone(self.c.wait_offline("bravo"))
        self.lc.start("bravo")
        self.assertIsNotNone(self.c.wait_online("bravo"))
        got = self.c.call("bravo", "GET", f"/records/{rec['id']}")[1]
        self.assertEqual((got["version"], got["payload"]["severity"]), (2, "critical"))

    def test_partition_config_survives_restart(self):
        self.c.set_link("alpha", "bravo", True)
        self.lc.stop("alpha")
        self.lc.start("alpha")
        self.c.wait_online("alpha")
        self.assertIn("bravo", self.c.call("alpha", "GET", "/admin/links")[1]["blocked"])

    def test_failed_sync_to_dead_node_does_not_corrupt_state(self):
        _, rec = self.c.create("alpha", "incident", INC)
        self.lc.stop("delta")
        try:
            s = self.c.sync_node("alpha")[1]
            outs = {r["peer"]: r["outcome"] for r in s["results"]}
            self.assertEqual(outs["delta"], "unreachable")
            self.assertEqual(outs["bravo"], "success")
            self.assertEqual(self.c.call("alpha", "GET", f"/records/{rec['id']}")[1]["version"], 1)
        finally:
            self.lc.start("delta")
            self.c.wait_online("delta")
        self.c.sync_network()
        self.assertEqual(self.c.call("delta", "GET", f"/records/{rec['id']}")[0], 200)


class ScenarioTests(ClusterTestCase):
    def _check(self, res):
        failed = [s for s in res["steps"] if not s["ok"]]
        self.assertTrue(res["passed"], f"scenario {res['key']} failed steps: {failed}")

    def test_scenario_a(self): self._check(scenarios.scenario_a(self.c))
    def test_scenario_b(self): self._check(scenarios.scenario_b(self.c))
    def test_scenario_c(self): self._check(scenarios.scenario_c(self.c))
    def test_scenario_d(self): self._check(scenarios.scenario_d(self.c, self.lc))
    def test_scenario_e(self): self._check(scenarios.scenario_e(self.c))

    def test_scenarios_are_repeatable(self):
        for _ in range(2):
            self._check(scenarios.scenario_c(self.c))


if __name__ == "__main__":
    unittest.main()
