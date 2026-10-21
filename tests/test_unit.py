"""Unit tests: persistence, validation, merge rules (no network)."""
import itertools
import os
import tempfile
import unittest

from dcn import schemas
from dcn.db import Database, wire_record
from dcn.merge import decide, merge_pair
from dcn.util import record_digest

INC = {"title": "Flood", "description": "d", "category": "flood", "severity": "high",
       "location": "Sector 1"}
REQ = {"resource_type": "food", "quantity": 20, "priority": "high"}


def mkdb(node="alpha", path=None):
    path = path or os.path.join(tempfile.mkdtemp(), f"{node}.db")
    return Database(path, node), path


def mkrec(rid="INC-ALPHA-00000001", version=1, ts="2026-01-01T00:00:00.000000Z", writer="alpha",
          sev="high", deleted=False, origin="alpha"):
    r = {"id": rid, "kind": "incident",
         "payload": dict(INC, severity=sev, reporting_station=origin, status="open"),
         "version": version, "updated_at": ts, "writer": writer, "origin": origin,
         "created_at": "2026-01-01T00:00:00.000000Z", "deleted": deleted}
    r["digest"] = record_digest(r)
    return r


class PersistenceTests(unittest.TestCase):
    def test_incident_saved_locally(self):
        db, _ = mkdb()
        rec = db.create_record("incident", INC)
        got = db.get_record(rec["id"])
        self.assertEqual(got["payload"]["title"], "Flood")
        self.assertEqual((got["version"], got["origin"], got["writer"]), (1, "alpha", "alpha"))
        self.assertEqual(got["payload"]["reporting_station"], "alpha")

    def test_request_saved_locally(self):
        db, _ = mkdb()
        rec = db.create_record("request", REQ)
        got = db.get_record(rec["id"])
        self.assertEqual(got["payload"]["status"], "pending")
        self.assertEqual(got["payload"]["requesting_station"], "alpha")
        self.assertTrue(got["created_at"] and got["updated_at"])

    def test_survives_reopen(self):
        db, path = mkdb()
        rid = db.create_record("incident", INC)["id"]
        db.update_record(rid, {"severity": "critical"})
        db2 = Database(path, "alpha")           # simulates a service restart
        got = db2.get_record(rid)
        self.assertEqual((got["version"], got["payload"]["severity"]), (2, "critical"))

    def test_update_bumps_version_and_digest(self):
        db, _ = mkdb()
        r1 = db.create_record("incident", INC)
        r2 = db.update_record(r1["id"], {"severity": "low"})
        self.assertEqual(r2["version"], 2)
        self.assertNotEqual(r1["digest"], r2["digest"])

    def test_request_status_transitions(self):
        db, _ = mkdb()
        rid = db.create_record("request", REQ)["id"]
        with self.assertRaises(schemas.ValidationError) as cm:
            db.update_record(rid, {"status": "fulfilled"})       # pending -> fulfilled illegal
        self.assertEqual(cm.exception.code, "invalid_transition")
        db.update_record(rid, {"status": "acknowledged"})
        db.update_record(rid, {"status": "fulfilled"})
        with self.assertRaises(schemas.ValidationError):
            db.update_record(rid, {"status": "cancelled"})       # terminal

    def test_delete_is_tombstone(self):
        db, _ = mkdb()
        rid = db.create_record("incident", INC)["id"]
        t = db.delete_record(rid)
        self.assertTrue(t["deleted"])
        self.assertEqual(db.list_records(), [])
        self.assertEqual(len(db.list_records(include_deleted=True)), 1)


class ValidationTests(unittest.TestCase):
    def test_bad_incident_fields(self):
        for bad in ({}, dict(INC, severity="extreme"), dict(INC, category="zombies"),
                    dict(INC, title=""), dict(INC, title="x" * 500), dict(INC, location=5)):
            with self.assertRaises(schemas.ValidationError):
                schemas.validate_incident_payload(bad, "alpha")

    def test_bad_request_fields(self):
        for bad in (dict(REQ, quantity=0), dict(REQ, quantity="5"), dict(REQ, quantity=True),
                    dict(REQ, resource_type="gold"), dict(REQ, priority="whenever")):
            with self.assertRaises(schemas.ValidationError):
                schemas.validate_request_payload(bad, "alpha")

    def test_reporting_station_must_match(self):
        with self.assertRaises(schemas.ValidationError):
            schemas.validate_incident_payload(dict(INC, reporting_station="bravo"), "alpha")

    def test_non_editable_field_rejected(self):
        with self.assertRaises(schemas.ValidationError):
            schemas.apply_changes("incident", mkrec()["payload"], {"reporting_station": "x"})

    def test_envelope_rejects_tampering(self):
        r = mkrec()
        schemas.validate_record_envelope(r)
        r2 = dict(r, payload=dict(r["payload"], severity="low"))     # digest now wrong
        with self.assertRaises(schemas.ValidationError):
            schemas.validate_record_envelope(r2)
        for bad in (dict(r, version=0), dict(r, id="nope"), dict(r, updated_at="yesterday")):
            with self.assertRaises(schemas.ValidationError):
                schemas.validate_record_envelope(bad)


class MergeTests(unittest.TestCase):
    def test_higher_version_wins_and_stale_is_ignored(self):
        db, _ = mkdb("bravo")
        new, old = mkrec(version=3, sev="critical"), mkrec(version=2, sev="low")
        db.apply_remote_batch([new], "alpha")
        s = db.apply_remote_batch([old], "alpha")                    # older arrives late
        self.assertEqual(s["stale"], 1)
        self.assertEqual(db.get_record(new["id"])["version"], 3)

    def test_duplicate_delivery_is_idempotent(self):
        db, _ = mkdb("bravo")
        r = mkrec()
        first = db.apply_remote_batch([r], "alpha")
        again = db.apply_remote_batch([r, r], "alpha")
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(again["duplicates"], 2)
        self.assertEqual(len(db.list_records()), 1)
        self.assertEqual(db.counters()["duplicates_ignored"], 2)

    def test_conflict_rule_is_deterministic_and_order_independent(self):
        a = mkrec(version=2, ts="2026-01-01T00:00:05.000000Z", writer="alpha", sev="critical")
        b = mkrec(version=2, ts="2026-01-01T00:00:09.000000Z", writer="bravo", sev="low")
        self.assertEqual(merge_pair(a, b)["writer"], "bravo")       # later timestamp wins
        self.assertEqual(merge_pair(b, a)["writer"], "bravo")
        c = mkrec(version=2, ts="2026-01-01T00:00:09.000000Z", writer="charlie", sev="medium")
        self.assertEqual(merge_pair(b, c)["writer"], "charlie")     # timestamp tie -> node id
        self.assertEqual(decide(a, b)["conflict"], True)

    def test_every_delivery_order_converges(self):
        recs = [mkrec(version=2, ts="2026-01-01T00:00:0%d.000000Z" % i, writer=w, sev=s)
                for i, (w, s) in enumerate([("alpha", "low"), ("bravo", "medium"),
                                             ("charlie", "high"), ("delta", "critical")])]
        finals = set()
        for perm in itertools.permutations(recs):
            db, _ = mkdb("bravo")
            for r in perm:
                db.apply_remote_batch([r], "x")
            finals.add(db.get_record(recs[0]["id"])["digest"])
        self.assertEqual(len(finals), 1)

    def test_conflict_is_logged_with_metadata(self):
        db, _ = mkdb("charlie")
        a = mkrec(version=2, ts="2026-01-01T00:00:05.000000Z", writer="alpha")
        b = mkrec(version=2, ts="2026-01-01T00:00:09.000000Z", writer="bravo", sev="low")
        db.apply_remote_batch([a], "alpha")
        s = db.apply_remote_batch([b], "bravo")
        self.assertEqual(s["conflicts"], 1)
        c = db.conflicts()[0]
        self.assertEqual(c["winner"], "remote")
        self.assertIn("later updated_at", c["rule"])
        self.assertEqual((c["local"]["writer"], c["remote"]["writer"]), ("alpha", "bravo"))

    def test_tombstone_not_resurrected_by_stale_copy(self):
        db, _ = mkdb("bravo")
        live = mkrec(version=1)
        tomb = mkrec(version=2, deleted=True, ts="2026-01-01T00:00:03.000000Z")
        db.apply_remote_batch([tomb], "alpha")
        db.apply_remote_batch([live], "charlie")                    # stale live copy arrives later
        self.assertEqual(db.list_records(), [])
        self.assertTrue(db.get_record(live["id"])["deleted"])

    def test_invalid_records_never_persisted_and_valid_ones_still_merge(self):
        db, _ = mkdb("bravo")
        good, bad = mkrec(), dict(mkrec(rid="INC-ALPHA-00000002"), digest="deadbeef")
        s = db.apply_remote_batch([good, bad], "alpha")
        self.assertEqual((s["inserted"], s["invalid"]), (1, 1))
        self.assertIsNone(db.get_record("INC-ALPHA-00000002"))

    def test_batch_is_atomic(self):
        db, _ = mkdb("bravo")
        r1 = mkrec()
        r2 = mkrec(rid="INC-ALPHA-00000002")
        orig = Database._store
        calls = {"n": 0}

        def flaky(c, rec, received_from=None, replace=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk error")
            return orig(c, rec, received_from, replace)
        Database._store = staticmethod(flaky)
        try:
            with self.assertRaises(RuntimeError):
                db.apply_remote_batch([r1, r2], "alpha")
        finally:
            Database._store = staticmethod(orig)
        self.assertEqual(db.list_records(include_deleted=True), [])  # nothing half-applied

    def test_wire_record_strips_local_columns(self):
        db, _ = mkdb()
        rec = db.create_record("incident", INC)
        self.assertNotIn("received_from", wire_record(db.get_record(rec["id"])))


if __name__ == "__main__":
    unittest.main()
