"""Repeatable demonstration scenarios A-E.

Each scenario starts from a clean state (reset), runs real operations against the
stations over HTTP, VERIFIES the expected outcome from the stations' own data, and
returns a structured result. Used by the dashboard buttons, the CLI and the tests.
"""
import time

INC = {"title": "Flooded underpass", "description": "Water level rising on main road",
       "category": "flood", "severity": "high", "location": "Sector 4"}


def _inc(title, **kw):
    return dict(INC, title=title, **kw)


class Result:
    def __init__(self, key, title):
        self.key, self.title, self.steps, self.t0 = key, title, [], time.perf_counter()
        self.data = {}

    def step(self, text, ok=True, detail="", **data):
        self.steps.append({"text": text, "ok": bool(ok), "detail": detail, **data})
        return ok

    def done(self):
        return {"key": self.key, "title": self.title, "steps": self.steps, "data": self.data,
                "passed": all(s["ok"] for s in self.steps),
                "duration_ms": round((time.perf_counter() - self.t0) * 1000, 1)}


def holders(cluster, rid):
    """Which online stations hold the record, and how many copies (must be 1 each)."""
    out = {}
    for n in cluster.nodes:
        st, b = cluster.call(n, "GET", f"/records/{rid}")
        out[n] = (b if st == 200 else None)
    return out


def _fresh(cluster, r):
    cluster.heal_all()
    cluster.reset_all()
    ok = all(s["online"] for s in cluster.status_all().values())
    r.step("Reset all stations to a clean state, all links connected", ok)
    return ok


def _count_exactly_once(cluster, rid):
    recs = cluster.records_all(include_deleted=True)
    return {n: sum(1 for x in (rs or []) if x["id"] == rid) for n, rs in recs.items()}


def scenario_a(cluster):
    r = Result("A", "Normal operation: report, replicate, verify")
    if not _fresh(cluster, r):
        return r.done()
    st, rec = cluster.create("alpha", "incident", _inc("Flooded underpass"))
    r.step("Create incident at Station Alpha (saved to Alpha's SQLite first)", st == 201,
           rec.get("id", ""))
    rid = rec.get("id")
    h = holders(cluster, rid)
    r.step("Before sync: only Alpha holds it (locally stored, not yet replicated)",
           h["alpha"] and not any(h[n] for n in ("bravo", "charlie", "delta")),
           f"holders={[n for n, v in h.items() if v]}")
    s = cluster.sync_network()
    r.data["sync_ms"] = s["duration_ms"]
    h = holders(cluster, rid)
    r.step("After sync: all four stations hold the incident", all(h.values()),
           f"holders={[n for n, v in h.items() if v]}, sync took {s['duration_ms']} ms")
    r.step("Each station holds it exactly once", set(_count_exactly_once(cluster, rid).values()) == {1})
    r.step("Replicas converged (identical fingerprints)", s["convergence"]["converged"])
    return r.done()


def scenario_b(cluster):
    r = Result("B", "Network partition: Alpha isolated, local writes continue")
    if not _fresh(cluster, r):
        return r.done()
    cluster.isolate("alpha")
    r.step("Disconnect Alpha from Bravo, Charlie and Delta (stations keep running)",
           len(cluster.blocked_pairs()) == 3, f"blocked={sorted(cluster.blocked_pairs())}")
    st, rec = cluster.create("alpha", "incident", _inc("Bridge collapse", category="infrastructure failure",
                                                      severity="critical"))
    r.step("Alpha still accepts a new incident while partitioned", st == 201, rec.get("id", ""))
    rid = rec.get("id")
    st2, rec2 = cluster.create("bravo", "incident", _inc("Fire near depot", category="fire"))
    s = cluster.sync_network()
    h = holders(cluster, rid)
    r.step("Other stations do NOT have Alpha's incident",
           h["alpha"] and not any(h[n] for n in ("bravo", "charlie", "delta")),
           f"holders={[n for n, v in h.items() if v]}")
    alpha_out = [x["outcome"] for x in s["results"] if x["initiator"] == "alpha"]
    r.step("Alpha's sync attempts were skipped by the partition logic",
           set(alpha_out) == {"skipped_partition"}, f"outcomes={alpha_out}")
    h2 = holders(cluster, rec2.get("id"))
    r.step("Inside the isolated side, Bravo/Charlie/Delta still synchronize with each other",
           all(h2[n] for n in ("bravo", "charlie", "delta")) and not h2["alpha"],
           f"holders={[n for n, v in h2.items() if v]}")
    r.step("Network is NOT converged (expected during a partition)", not s["convergence"]["converged"])
    r.data["incident_id"] = rid
    return r.done()


def scenario_c(cluster):
    r = Result("C", "Network recovery: reconnect Alpha and converge")
    b = scenario_b(cluster)           # reproduce the partition state from scratch
    r.step("Re-create the partition state (scenario B setup)", b["passed"])
    rid = b["data"].get("incident_id")
    cluster.heal_all()
    r.step("Reconnect all links", not cluster.blocked_pairs())
    s = cluster.sync_network()
    r.data["sync_ms"] = s["duration_ms"]
    h = holders(cluster, rid)
    r.step("Alpha's incident propagated to every station", all(h.values()),
           f"sync took {s['duration_ms']} ms in {s['rounds']} round(s)")
    cnt = _count_exactly_once(cluster, rid)
    r.step("Incident exists exactly once at each station", set(cnt.values()) == {1}, str(cnt))
    r.step("All stations converged (identical fingerprints)", s["convergence"]["converged"])
    return r.done()


def scenario_d(cluster, controller, target="bravo"):
    r = Result("D", f"Node crash and restart: {target}")
    if not _fresh(cluster, r):
        return r.done()
    _, a1 = cluster.create("alpha", "incident", _inc("Landslide on hill road", category="earthquake"))
    cluster.sync_network()
    st, _ = cluster.call(target, "GET", "/status")
    before = st == 200
    r.step(f"Baseline: all stations hold the first incident", before)
    controller.stop_node(target)
    gone = cluster.wait_offline(target)
    r.step(f"{target} container stopped; detected unavailable", gone is not None,
           f"detected after {gone}s")
    _, a2 = cluster.create("alpha", "incident", _inc("Medical camp needed", category="medical emergency"))
    s = cluster.sync_network()
    others = [n for n in cluster.nodes if n != target]
    h = holders(cluster, a2["id"])
    r.step("Remaining stations keep working and share the new incident",
           all(h[n] for n in others), f"holders={[n for n, v in h.items() if v]}")
    fails = [x for x in s["results"] if x["peer"] == target and x["outcome"] != "success"]
    r.step(f"Sync attempts to {target} fail as 'unreachable' (crash, not partition)",
           bool(fails) and all(x["outcome"] == "unreachable" for x in fails),
           f"{len(fails)} failed attempts")
    controller.start_node(target)
    up = cluster.wait_online(target)
    r.data["restart_s"] = up
    r.step(f"{target} restarted", up is not None, f"healthy after {up}s")
    h = holders(cluster, a1["id"])
    r.step("Persisted state survived: first incident still in its volume", bool(h[target]))
    r.step("Missed incident is absent until synchronization", not holders(cluster, a2["id"])[target])
    s = cluster.sync_network()
    r.data["catchup_ms"] = s["duration_ms"]
    h = holders(cluster, a2["id"])
    r.step("After sync the restarted station caught up", bool(h[target]),
           f"catch-up sync took {s['duration_ms']} ms")
    r.step("All stations converged", s["convergence"]["converged"])
    return r.done()


def scenario_e(cluster):
    r = Result("E", "Concurrent updates: deterministic conflict resolution")
    if not _fresh(cluster, r):
        return r.done()
    _, rec = cluster.create("alpha", "incident", _inc("Gas leak report", category="other", severity="medium"))
    rid = rec["id"]
    cluster.sync_network()
    cluster.isolate("alpha")
    cluster.isolate("bravo")
    r.step("Disconnect Alpha and Bravo from the network", len(cluster.blocked_pairs()) >= 5)
    s1, ua = cluster.update("alpha", rid, {"severity": "critical", "description": "Alpha: confirmed leak"})
    time.sleep(0.01)
    s2, ub = cluster.update("bravo", rid, {"severity": "low", "description": "Bravo: false alarm"})
    r.step("Update the SAME incident independently at Alpha and Bravo (both -> version 2)",
           s1 == 200 and s2 == 200 and ua["version"] == ub["version"] == 2,
           f"alpha v{ua.get('version')} @{ua.get('updated_at')}, bravo v{ub.get('version')} @{ub.get('updated_at')}")
    cluster.heal_all()
    s = cluster.sync_network()
    conf = cluster.conflicts_all()
    found = [dict(c, detected_at=n) for n, cs in conf.items() for c in (cs or []) if c["record_id"] == rid]
    r.step("Conflict detected and logged by the stations", bool(found), f"{len(found)} log entries")
    final = holders(cluster, rid)
    winners = {n: (v["writer"], v["version"], v["payload"]["severity"]) for n, v in final.items()}
    expect = "bravo" if ub["updated_at"] > ua["updated_at"] else "alpha"
    r.step(f"Every station converged on one winner, by the documented rule (expected writer: {expect})",
           len(set(winners.values())) == 1 and next(iter(winners.values()))[0] == expect, str(winners))
    r.step("Replicas converged", s["convergence"]["converged"])
    r.data["conflicts"] = found[:4]
    r.data["alpha_update"] = {k: ua[k] for k in ("version", "updated_at", "writer")}
    r.data["bravo_update"] = {k: ub[k] for k in ("version", "updated_at", "writer")}
    r.data["winner"] = expect
    return r.done()


SCENARIOS = {
    "A": ("Normal operation", scenario_a), "B": ("Network partition", scenario_b),
    "C": ("Network recovery", scenario_c), "D": ("Node crash & restart", scenario_d),
    "E": ("Concurrent updates", scenario_e),
}


def run(key, cluster, controller=None):
    fn = SCENARIOS[key][1]
    return fn(cluster, controller) if key == "D" else fn(cluster)


# ---- Scenario D, manual variant: used when the dashboard cannot control containers ----------
def scenario_d_prepare(cluster):
    r = Result("D1", "Crash demo - step 1: baseline")
    if not _fresh(cluster, r):
        return r.done()
    _, a1 = cluster.create("alpha", "incident", _inc("Landslide on hill road", category="earthquake"))
    s = cluster.sync_network()
    h = holders(cluster, a1["id"])
    r.step("Baseline incident replicated to all four stations", all(h.values()))
    r.data["baseline_id"] = a1["id"]
    r.step("Now stop one station, e.g.  docker compose stop station-bravo", True)
    return r.done()


def scenario_d_while_down(cluster, target="bravo"):
    r = Result("D2", f"Crash demo - step 2: operate while {target} is down")
    st, _ = cluster.call(target, "GET", "/health", timeout=1.0)
    r.step(f"{target} is unavailable (expected)", st is None)
    _, a2 = cluster.create("alpha", "incident", _inc("Medical camp needed", category="medical emergency"))
    s = cluster.sync_network()
    others = [n for n in cluster.nodes if n != target]
    h = holders(cluster, a2["id"])
    r.step("The other stations keep working and share the new incident", all(h[n] for n in others))
    fails = [x["outcome"] for x in s["results"] if x["peer"] == target]
    r.step(f"Attempts to reach {target} failed as 'unreachable' (crash, not partition)",
           bool(fails) and set(fails) == {"unreachable"}, f"outcomes={fails}")
    r.data["missed_id"] = a2["id"]
    r.step(f"Now restart it:  docker compose start station-{target}", True)
    return r.done()


def scenario_d_recover(cluster, baseline_id, missed_id, target="bravo"):
    r = Result("D3", f"Crash demo - step 3: {target} recovers and catches up")
    up = cluster.wait_online(target, 5)
    r.step(f"{target} is back online", up is not None)
    if up is None:
        return r.done()
    r.step("Persisted data survived the crash (baseline incident still in its volume)",
           bool(holders(cluster, baseline_id)[target]))
    r.step("Incident created during the outage is still missing here",
           not holders(cluster, missed_id)[target])
    s = cluster.sync_network()
    r.step("After synchronization it caught up", bool(holders(cluster, missed_id)[target]),
           f"{s['duration_ms']} ms")
    r.step("All stations converged", s["convergence"]["converged"])
    return r.done()
