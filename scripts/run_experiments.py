"""Measure the system and write results/experiments.json (+ results/experiments.md).

Local processes (default, no Docker):   python scripts/run_experiments.py
Against the running Docker stack:       python scripts/run_experiments.py --compose
Everything reported is measured from the stations' own responses; nothing is hard-coded.
"""
import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dcn import scenarios  # noqa: E402
from dcn.client import Cluster  # noqa: E402
from dcn.controllers import ComposeCliController, LocalCluster  # noqa: E402

INC = {"title": "Experiment incident", "description": "bulk", "category": "other",
       "severity": "medium", "location": "Lab"}
COMPOSE_URLS = {"alpha": "http://localhost:8001", "bravo": "http://localhost:8002",
                "charlie": "http://localhost:8003", "delta": "http://localhost:8004"}


def comm_totals(cluster):
    tot = {"success": 0, "failed": 0, "skipped_partition": 0, "other": 0}
    by_outcome = {}
    for n, evs in cluster.events_all(100000).items():
        for e in evs or []:
            if e["role"] != "initiator":
                continue
            by_outcome[e["outcome"]] = by_outcome.get(e["outcome"], 0) + 1
    for k, v in by_outcome.items():
        key = "success" if k == "success" else "skipped_partition" if k == "skipped_partition" else \
            "failed" if k in ("unreachable", "rejected_partition", "failed", "partial") else "other"
        tot[key] += v
    return {**tot, "by_outcome": by_outcome}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compose", action="store_true", help="use the running Docker stack on localhost:8001-8004")
    ap.add_argument("--in-docker", action="store_true",
                    help="run inside the dashboard container (uses STATIONS env + Docker socket)")
    ap.add_argument("--repeat", type=int, default=5, help="repetitions for the scaling measurement")
    args = ap.parse_args()

    lc = None
    if args.in_docker:
        from dcn.config import parse_peers
        from dcn.controllers import DockerSocketController
        cluster, ctl = Cluster(parse_peers(os.environ["STATIONS"])), DockerSocketController()
    elif args.compose:
        cluster, ctl = Cluster(COMPOSE_URLS), ComposeCliController()
    else:
        lc = LocalCluster(http_timeout=1.0, retries=0)
        lc.start_all()
        cluster, ctl = lc.cluster, lc
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
           "mode": "docker-compose" if (args.compose or args.in_docker) else "local-processes (4 real OS processes, localhost HTTP)",
           "platform": f"{platform.system()} {platform.release()}, Python {platform.python_version()}"}
    try:
        # 1. scenarios A-E
        rows = []
        for k in "ABCDE":
            res = scenarios.run(k, cluster, ctl)
            rows.append({"scenario": f"{k} · {res['title']}", "passed": res["passed"],
                         "steps_passed": f"{sum(s['ok'] for s in res['steps'])}/{len(res['steps'])}",
                         "duration_ms": res["duration_ms"]})
            print(rows[-1])
        out["scenarios"] = rows

        # 2. sync time vs number of records created at Alpha
        scaling = []
        for n in (1, 10, 50, 100, 250):
            times = []
            for _ in range(args.repeat if n <= 100 else 2):
                cluster.heal_all()
                cluster.reset_all()
                for i in range(n):
                    cluster.create("alpha", "incident", dict(INC, title=f"Incident {i}"))
                s = cluster.sync_network()
                assert s["convergence"]["converged"], "did not converge"
                times.append(s["duration_ms"])
            scaling.append({"records": n, "runs": len(times), "sync_ms": round(statistics.median(times), 1),
                            "min_ms": min(times), "max_ms": max(times), "converged": True})
            print(scaling[-1])
        out["sync_scaling"] = scaling

        # 3. partition recovery
        cluster.heal_all()
        cluster.reset_all()
        cluster.isolate("alpha")
        for i in range(20):
            cluster.create("alpha", "incident", dict(INC, title=f"Isolated {i}"))
        pre = cluster.sync_network()
        cluster.heal_all()
        t0 = time.perf_counter()
        s = cluster.sync_network()
        out["partition_recovery"] = {
            "records_created_while_isolated": 20,
            "sync_attempts_during_partition": len(pre["results"]),
            "recovery_sync_ms": s["duration_ms"], "rounds": s["rounds"],
            "converged_after": s["convergence"]["converged"],
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1)}

        # 4. crash & restart
        cluster.heal_all()
        cluster.reset_all()
        for i in range(10):
            cluster.create("alpha", "incident", dict(INC, title=f"Before crash {i}"))
        cluster.sync_network()
        ctl.stop_node("bravo")
        down = cluster.wait_offline("bravo")
        for i in range(15):
            cluster.create("charlie", "incident", dict(INC, title=f"During outage {i}"))
        during = cluster.sync_network()
        ctl.start_node("bravo")
        restart_s = cluster.wait_online("bravo", 60)
        survived = len(cluster.call("bravo", "GET", "/records")[1]["records"])
        s = cluster.sync_network()
        after = len(cluster.call("bravo", "GET", "/records")[1]["records"])
        out["crash_recovery"] = {
            "records_before_crash": 10, "records_created_during_outage": 15,
            "detected_down_after_s": down, "container_restart_s": restart_s,
            "records_surviving_in_bravo_volume": survived,
            "failed_attempts_to_bravo_during_outage": sum(1 for r in during["results"] if r["peer"] == "bravo" and r["outcome"] == "unreachable"),
            "catchup_sync_ms": s["duration_ms"], "records_after_catchup": after,
            "converged_after": s["convergence"]["converged"]}

        # 5. communication totals (events recorded since the last reset = the crash experiment)
        out["communication_crash_experiment"] = comm_totals(cluster)  # events since the last reset
        out["notes"] = ("Medians over repeated runs. Times include sequential HTTP round trips between 4 stations "
                        "on one machine plus fsync'd SQLite commits (synchronous=FULL); they are not WAN figures.")
        cluster.heal_all()
    finally:
        if lc:
            lc.stop_all()
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", "experiments.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    with open(os.path.join(ROOT, "results", "experiments.md"), "w", encoding="utf-8") as f:
        f.write(f"# Measured experiment results\n\nGenerated {out['generated_at']} — mode: {out['mode']} — {out['platform']}\n\n")
        f.write("## Scenarios\n\n| Scenario | Passed | Steps | ms |\n|---|---|---|---|\n")
        for r in out["scenarios"]:
            f.write(f"| {r['scenario']} | {r['passed']} | {r['steps_passed']} | {r['duration_ms']} |\n")
        f.write("\n## Sync time vs records (median)\n\n| Records | Runs | Median ms | Min | Max |\n|---|---|---|---|---|\n")
        for r in out["sync_scaling"]:
            f.write(f"| {r['records']} | {r['runs']} | {r['sync_ms']} | {r['min_ms']} | {r['max_ms']} |\n")
        f.write("\n## Partition recovery\n\n```json\n" + json.dumps(out["partition_recovery"], indent=2) + "\n```\n")
        f.write("\n## Crash and restart\n\n```json\n" + json.dumps(out["crash_recovery"], indent=2) + "\n```\n")
        f.write("\n## Communication attempts during the crash-and-restart experiment\n\n```json\n" + json.dumps(out["communication_crash_experiment"], indent=2) + "\n```\n")
        f.write("\n" + out["notes"] + "\n")
    print("wrote results/experiments.json and results/experiments.md")


if __name__ == "__main__":
    main()
