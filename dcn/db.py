"""SQLite persistence for one field station. Each station owns its own file.

All writes happen inside a single BEGIN IMMEDIATE transaction, so a failed or
interrupted operation (including a partially delivered sync batch) rolls back
and never leaves a half-applied state.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import schemas
from .merge import decide
from .util import fingerprint, new_id, now_iso, record_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
    version INTEGER NOT NULL, updated_at TEXT NOT NULL, writer TEXT NOT NULL,
    origin TEXT NOT NULL, created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
    digest TEXT NOT NULL, received_from TEXT, received_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, role TEXT NOT NULL,
    peer TEXT NOT NULL, outcome TEXT NOT NULL, sent INTEGER DEFAULT 0,
    received INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0, stale INTEGER DEFAULT 0,
    conflicts INTEGER DEFAULT 0, invalid INTEGER DEFAULT 0, duration_ms REAL DEFAULT 0,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, record_id TEXT NOT NULL,
    kind TEXT NOT NULL, rule TEXT NOT NULL, winner TEXT NOT NULL,
    local_json TEXT NOT NULL, remote_json TEXT NOT NULL, from_node TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS peer_state (
    peer TEXT NOT NULL, record_id TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY (peer, record_id)
);
CREATE TABLE IF NOT EXISTS links (peer TEXT PRIMARY KEY, blocked INTEGER NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
"""


def _row_to_record(row):
    return {
        "id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]),
        "version": row["version"], "updated_at": row["updated_at"], "writer": row["writer"],
        "origin": row["origin"], "created_at": row["created_at"],
        "deleted": bool(row["deleted"]), "digest": row["digest"],
        "received_from": row["received_from"], "received_at": row["received_at"],
    }


def wire_record(rec):
    """Strip local-only bookkeeping before a record goes on the wire."""
    return {k: rec[k] for k in ("id", "kind", "payload", "version", "updated_at",
                                 "writer", "origin", "created_at", "deleted", "digest")}


class NotFound(KeyError):
    pass


class Database:
    def __init__(self, path, node_id):
        self.path = path
        self.node_id = node_id
        self._lock = threading.RLock()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        c = self._connect()
        try:
            c.executescript(SCHEMA)
        finally:
            c.close()

    # ---- connection helpers -------------------------------------------------
    def _connect(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @contextmanager
    def tx(self):
        with self._lock:
            c = self._connect()
            try:
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.execute("COMMIT")
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                c.close()

    def _q(self, sql, args=()):
        c = self._connect()
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    # ---- counters -----------------------------------------------------------
    @staticmethod
    def _incr(c, name, n=1):
        if n:
            c.execute("INSERT INTO counters(name,value) VALUES(?,?) "
                      "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, n))

    def incr(self, name, n=1):
        with self.tx() as c:
            self._incr(c, name, n)

    def counters(self):
        return {r["name"]: r["value"] for r in self._q("SELECT * FROM counters")}

    # ---- local writes -------------------------------------------------------
    @staticmethod
    def _store(c, rec, received_from=None, replace=False):
        vals = (rec["id"], rec["kind"], json.dumps(rec["payload"], sort_keys=True),
                rec["version"], rec["updated_at"], rec["writer"], rec["origin"],
                rec["created_at"], int(rec["deleted"]), rec["digest"],
                received_from, now_iso() if received_from else None)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        c.execute(f"{verb} INTO records(id,kind,payload,version,updated_at,writer,origin,"
                  "created_at,deleted,digest,received_from,received_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", vals)

    @staticmethod
    def _get(c, rid):
        row = c.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        return _row_to_record(row) if row else None

    def create_record(self, kind, data):
        payload = schemas.validate_payload(kind, data, self.node_id)
        ts = now_iso()
        rec = {"id": new_id(kind, self.node_id), "kind": kind, "payload": payload,
               "version": 1, "updated_at": ts, "writer": self.node_id,
               "origin": self.node_id, "created_at": ts, "deleted": False}
        rec["digest"] = record_digest(rec)
        with self.tx() as c:
            self._store(c, rec)
            self._incr(c, "local_writes")
        return rec

    def update_record(self, rid, changes):
        with self.tx() as c:
            cur = self._get(c, rid)
            if not cur:
                raise NotFound(rid)
            if cur["deleted"]:
                raise schemas.ValidationError("record is deleted (tombstone)", code="deleted")
            payload = schemas.apply_changes(cur["kind"], cur["payload"], changes)
            rec = dict(cur, payload=payload, version=cur["version"] + 1,
                       updated_at=max(now_iso(), cur["updated_at"]), writer=self.node_id)
            rec["digest"] = record_digest(rec)
            self._store(c, rec, received_from=cur["received_from"], replace=True)
            self._incr(c, "local_writes")
            return rec

    def delete_record(self, rid):
        """Soft delete: a tombstone is a normal versioned record, so deletes replicate
        and a stale copy elsewhere can never resurrect the record."""
        with self.tx() as c:
            cur = self._get(c, rid)
            if not cur:
                raise NotFound(rid)
            if cur["deleted"]:
                return cur
            rec = dict(cur, deleted=True, version=cur["version"] + 1,
                       updated_at=max(now_iso(), cur["updated_at"]), writer=self.node_id)
            rec["digest"] = record_digest(rec)
            self._store(c, rec, received_from=cur["received_from"], replace=True)
            self._incr(c, "local_writes")
            return rec

    # ---- reads --------------------------------------------------------------
    def get_record(self, rid):
        rows = self._q("SELECT * FROM records WHERE id=?", (rid,))
        return _row_to_record(rows[0]) if rows else None

    def list_records(self, kind=None, include_deleted=False):
        sql, args = "SELECT * FROM records WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if not include_deleted:
            sql += " AND deleted=0"
        sql += " ORDER BY created_at, id"
        return [_row_to_record(r) for r in self._q(sql, args)]

    def get_many(self, ids):
        if not ids:
            return []
        ids = list(ids)
        out = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            out += [_row_to_record(r) for r in
                    self._q(f"SELECT * FROM records WHERE id IN ({q})", chunk)]
        return out

    def summary(self):
        return {r["id"]: [r["version"], r["updated_at"], r["writer"], r["digest"]]
                for r in self._q("SELECT id,version,updated_at,writer,digest FROM records")}

    def fingerprint(self):
        return fingerprint((r["id"], r["digest"]) for r in
                           self._q("SELECT id,digest FROM records"))

    # ---- applying remote data ----------------------------------------------
    def apply_remote_batch(self, records, from_node):
        """Validate then merge a batch atomically. Returns statistics.

        Invalid records are counted and skipped (never persisted). Valid records
        are merged in ONE transaction: all-or-nothing.
        """
        stats = {"inserted": 0, "replaced": 0, "duplicates": 0, "stale": 0,
                 "conflicts": 0, "invalid": 0, "invalid_details": [], "applied": {},
                 "conflict_details": []}
        valid = []
        for r in records:
            try:
                valid.append(schemas.validate_record_envelope(r))
            except schemas.ValidationError as e:
                stats["invalid"] += 1
                stats["invalid_details"].append(str(e))
        with self.tx() as c:
            for rem in valid:
                local = self._get(c, rem["id"])
                d = decide(local, rem)
                if d["conflict"]:
                    stats["conflicts"] += 1
                    c.execute("INSERT INTO conflicts(ts,record_id,kind,rule,winner,local_json,"
                              "remote_json,from_node) VALUES(?,?,?,?,?,?,?,?)",
                              (now_iso(), rem["id"], rem["kind"], d["rule"], d["winner"],
                               json.dumps(wire_record(local)), json.dumps(wire_record(rem)),
                               from_node))
                    stats["conflict_details"].append(
                        {"record_id": rem["id"], "rule": d["rule"], "winner": d["winner"]})
                if d["action"] == "insert":
                    self._store(c, rem, received_from=from_node)
                    stats["inserted"] += 1
                    stats["applied"][rem["id"]] = "applied"
                elif d["action"] == "replace":
                    self._store(c, rem, received_from=from_node, replace=True)
                    stats["replaced"] += 1
                    stats["applied"][rem["id"]] = "applied"
                elif d["action"] == "duplicate":
                    stats["duplicates"] += 1
                    stats["applied"][rem["id"]] = "duplicate"
                else:
                    stats["stale"] += 1
                    stats["applied"][rem["id"]] = "stale"
            self._incr(c, "duplicates_ignored", stats["duplicates"])
            self._incr(c, "stale_ignored", stats["stale"])
            self._incr(c, "conflicts_detected", stats["conflicts"])
            self._incr(c, "invalid_rejected", stats["invalid"])
        return stats

    # ---- sync bookkeeping ---------------------------------------------------
    def log_event(self, role, peer, outcome, sent=0, received=0, duplicates=0, stale=0,
                  conflicts=0, invalid=0, duration_ms=0.0, detail=""):
        with self.tx() as c:
            c.execute("INSERT INTO sync_events(ts,role,peer,outcome,sent,received,duplicates,"
                      "stale,conflicts,invalid,duration_ms,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now_iso(), role, peer, outcome, sent, received, duplicates, stale,
                       conflicts, invalid, round(duration_ms, 2), detail[:500]))

    def events(self, limit=100):
        return [dict(r) for r in self._q(
            "SELECT * FROM sync_events ORDER BY id DESC LIMIT ?", (limit,))]

    def conflicts(self, limit=50):
        out = []
        for r in self._q("SELECT * FROM conflicts ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["local"] = json.loads(d.pop("local_json"))
            d["remote"] = json.loads(d.pop("remote_json"))
            out.append(d)
        return out

    def set_peer_digests(self, peer, mapping):
        if not mapping:
            return
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO peer_state(peer,record_id,digest) VALUES(?,?,?)",
                          [(peer, rid, dg) for rid, dg in mapping.items()])

    def pending_by_peer(self, peers):
        """Records whose current digest the peer has not been confirmed to hold."""
        out = {}
        for p in peers:
            row = self._q(
                "SELECT COUNT(*) n FROM records r LEFT JOIN peer_state s "
                "ON s.peer=? AND s.record_id=r.id WHERE s.digest IS NULL OR s.digest!=r.digest",
                (p,))
            out[p] = row[0]["n"]
        return out

    # ---- partition configuration -------------------------------------------
    def set_link(self, peer, blocked):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO links(peer,blocked,updated_at) VALUES(?,?,?)",
                      (peer, int(bool(blocked)), now_iso()))

    def blocked_peers(self):
        return sorted(r["peer"] for r in self._q("SELECT peer FROM links WHERE blocked=1"))

    def is_blocked(self, peer):
        return bool(self._q("SELECT 1 FROM links WHERE peer=? AND blocked=1", (peer,)))

    # ---- stats / reset ------------------------------------------------------
    def stats(self):
        recs = self._q("SELECT kind,deleted,origin FROM records")
        live = [r for r in recs if not r["deleted"]]
        by_kind = {k: sum(1 for r in live if r["kind"] == k) for k in schemas.KINDS}
        ev = self._q(
            "SELECT outcome, COUNT(*) n FROM sync_events WHERE role='initiator' GROUP BY outcome")
        outcomes = {r["outcome"]: r["n"] for r in ev}
        last = self._q("SELECT ts FROM sync_events WHERE role='initiator' AND outcome='success' "
                       "ORDER BY id DESC LIMIT 1")
        avg = self._q("SELECT AVG(duration_ms) a FROM sync_events "
                      "WHERE role='initiator' AND outcome='success'")[0]["a"]
        return {
            "incidents": by_kind["incident"], "requests": by_kind["request"],
            "tombstones": sum(1 for r in recs if r["deleted"]),
            "local_origin": sum(1 for r in live if r["origin"] == self.node_id),
            "replicated_in": sum(1 for r in live if r["origin"] != self.node_id),
            "fingerprint": self.fingerprint(),
            "sync_outcomes": outcomes,
            "sync_success": outcomes.get("success", 0),
            "sync_failed": sum(v for k, v in outcomes.items()
                               if k in ("unreachable", "rejected_partition", "failed", "partial")),
            "sync_skipped": outcomes.get("skipped_partition", 0),
            "last_success_at": last[0]["ts"] if last else None,
            "avg_sync_ms": round(avg, 2) if avg else None,
            "counters": self.counters(),
        }

    def reset(self):
        with self.tx() as c:
            for t in ("records", "sync_events", "conflicts", "peer_state", "links", "counters"):
                c.execute(f"DELETE FROM {t}")
