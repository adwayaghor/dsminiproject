"""Peer-to-peer anti-entropy synchronization (version-based, pull + push).

One round between initiator A and responder B:

  1. A -> B  POST /sync/exchange  {summary: {id: [version, updated_at, writer, digest]}}
  2. B diffs summaries with the deterministic order key and replies
        send: full records B holds that are missing/newer at A
        need: ids that A holds that are missing/newer at B
  3. A merges `send` locally in one atomic transaction
  4. A -> B  POST /sync/push  {records: [... the `need` records ...]}
  5. B merges them atomically and replies with a per-record result

Every step is idempotent: re-delivering any message changes nothing.
Failure at any step leaves local state valid (each merge is all-or-nothing).
This is NOT consensus and gives no delivery guarantee: it is anti-entropy that
converges whenever the network lets peers talk long enough.
"""
import time

from . import schemas
from .db import wire_record
from .netclient import NetError, http_json

MAX_SUMMARY = 50000
MAX_BATCH = 5000


# ---------------------------------------------------------------- responder side
def respond_exchange(db, from_node, summary):
    if not isinstance(summary, dict) or len(summary) > MAX_SUMMARY:
        raise schemas.ValidationError("summary must be an object with <= 50000 entries")
    for rid, v in summary.items():
        if not (isinstance(v, list) and len(v) == 4 and isinstance(v[0], int)
                and all(isinstance(x, str) for x in v[1:])):
            raise schemas.ValidationError(f"bad summary entry for {rid}")
    t0 = time.perf_counter()
    mine = db.summary()
    send_ids, need_ids = [], []
    for rid in set(mine) | set(summary):
        if rid not in summary:
            send_ids.append(rid)
        elif rid not in mine:
            need_ids.append(rid)
        elif mine[rid][3] != summary[rid][3]:
            (send_ids if mine[rid] > summary[rid] else need_ids).append(rid)
    send = [wire_record(r) for r in db.get_many(send_ids)]
    # We now know exactly what the initiator holds for every id it listed.
    db.set_peer_digests(from_node, {rid: v[3] for rid, v in summary.items()})
    db.log_event("responder", from_node, "exchange", sent=len(send), received=0,
                 duration_ms=(time.perf_counter() - t0) * 1000,
                 detail=f"diff: sending {len(send)}, requesting {len(need_ids)}")
    db.incr("msgs_received")
    return {"from": db.node_id, "send": send, "need": sorted(need_ids)}


def respond_push(db, from_node, records):
    if not isinstance(records, list) or len(records) > MAX_BATCH:
        raise schemas.ValidationError("records must be a list with <= 5000 items")
    t0 = time.perf_counter()
    stats = db.apply_remote_batch(records, from_node)
    held = {r["id"]: r["digest"] for r in records
            if isinstance(r, dict) and stats["applied"].get(r.get("id")) in ("applied", "duplicate")}
    db.set_peer_digests(from_node, held)
    db.log_event("responder", from_node, "push",
                 received=stats["inserted"] + stats["replaced"], duplicates=stats["duplicates"],
                 stale=stats["stale"], conflicts=stats["conflicts"], invalid=stats["invalid"],
                 duration_ms=(time.perf_counter() - t0) * 1000,
                 detail=f"merged {stats['inserted'] + stats['replaced']} records")
    db.incr("msgs_received")
    return {"applied": stats["applied"],
            "stats": {k: stats[k] for k in ("inserted", "replaced", "duplicates", "stale",
                                             "conflicts", "invalid")},
            "conflict_details": stats["conflict_details"]}


# ---------------------------------------------------------------- initiator side
def sync_with_peer(db, cfg, peer):
    """Synchronize with one peer. Always returns a result dict; never raises on network faults."""
    t0 = time.perf_counter()
    res = {"peer": peer, "outcome": "failed", "sent": 0, "received": 0, "duplicates": 0,
           "stale": 0, "conflicts": 0, "invalid": 0, "detail": "", "duration_ms": 0.0,
           "conflict_details": []}

    def finish(outcome, detail=""):
        res["outcome"], res["detail"] = outcome, detail
        res["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        db.log_event("initiator", peer, outcome, sent=res["sent"], received=res["received"],
                     duplicates=res["duplicates"], stale=res["stale"], conflicts=res["conflicts"],
                     invalid=res["invalid"], duration_ms=res["duration_ms"], detail=detail)
        return res

    if peer not in cfg.peers:
        return finish("failed", "unknown peer")
    if db.is_blocked(peer):
        # Partition enforced in the peer-communication logic: no packet is even attempted.
        return finish("skipped_partition", f"link {cfg.node_id}<->{peer} is disconnected")

    base, hdr = cfg.peers[peer], {"X-Node-Id": cfg.node_id}
    summary = db.summary()

    def call(path, body):
        db.incr("msgs_sent")
        return http_json("POST", f"{base}{path}", body, hdr, cfg.http_timeout, cfg.http_retries)

    try:
        status, resp = call("/sync/exchange", {"from": cfg.node_id, "summary": summary})
    except NetError as e:
        db.incr("msgs_failed")
        return finish("unreachable", f"{e.kind}: {e.detail}")
    if status == 503 and resp.get("error") == "link_down":
        db.incr("msgs_failed")
        return finish("rejected_partition", "peer rejected: link is disconnected")
    if status != 200:
        db.incr("msgs_failed")
        return finish("failed", f"exchange HTTP {status}: {resp.get('error', '')}")

    send, need = resp.get("send"), resp.get("need")
    if not isinstance(send, list) or not isinstance(need, list):
        return finish("failed", "malformed exchange response")
    try:
        stats = db.apply_remote_batch(send, peer)   # atomic; invalid records are skipped
    except Exception as e:                           # DB error: nothing was committed
        return finish("failed", f"local merge failed: {e}")
    res["received"] = stats["inserted"] + stats["replaced"]
    for k in ("duplicates", "stale", "conflicts", "invalid"):
        res[k] += stats[k]
    res["conflict_details"] += stats["conflict_details"]

    # What do we now know the peer holds? equal ids + whatever it just sent us.
    known = {rid: v[3] for rid, v in summary.items() if rid not in set(need)}
    known.update({r["id"]: r["digest"] for r in send if isinstance(r, dict) and "id" in r})
    db.set_peer_digests(peer, known)

    outcome, detail = "success", f"received {res['received']}"
    to_push = [wire_record(r) for r in db.get_many([i for i in need if isinstance(i, str)])]
    if to_push:
        try:
            status, presp = call("/sync/push", {"from": cfg.node_id, "records": to_push})
        except NetError as e:
            db.incr("msgs_failed")
            return finish("partial", f"pull ok, push failed ({e.kind}: {e.detail})")
        if status != 200:
            db.incr("msgs_failed")
            return finish("partial", f"pull ok, push HTTP {status}: {presp.get('error', '')}")
        applied = presp.get("applied", {})
        res["sent"] = sum(1 for v in applied.values() if v == "applied")
        pst = presp.get("stats", {})
        res["duplicates"] += pst.get("duplicates", 0)
        res["stale"] += pst.get("stale", 0)
        res["conflicts"] += pst.get("conflicts", 0)
        res["conflict_details"] += presp.get("conflict_details", [])
        by_id = {r["id"]: r["digest"] for r in to_push}
        db.set_peer_digests(peer, {rid: by_id[rid] for rid, v in applied.items()
                                   if v in ("applied", "duplicate") and rid in by_id})
        detail += f", sent {res['sent']}"
    return finish(outcome, detail)


def sync_all(db, cfg, peers=None):
    return [sync_with_peer(db, cfg, p) for p in (peers or sorted(cfg.peers))]
