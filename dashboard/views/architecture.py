import json
import os

import pandas as pd
import streamlit as st

from dashboard import ui

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "results", "experiments.json")

ARCH_DOT = """
digraph A { rankdir=LR; compound=true; bgcolor="transparent"; fontname="Helvetica"; nodesep=0.25; ranksep=0.8;
 node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];
 edge [fontname="Helvetica", fontsize=9];
 user [label="Operator\\n(browser :8501)", shape=oval, fillcolor="#fdebd0"];
 dash [label="Streamlit dashboard\\n(stateless control + view)", fillcolor="#d6eaf8"];
 subgraph cluster_net { label="Docker Compose network dcn-net  (stations talk to each other by service name)"; style=dashed; color="#7f8c8d";
  subgraph cluster_a { label=""; style=invis; a [label="station-alpha\\nNorth Zone\\nHTTP :8000", fillcolor="#d4f4dd"]; va [label="alpha.db\\n(volume)", shape=cylinder, fillcolor="#f9e79f"]; a -> va; }
  subgraph cluster_b { label=""; style=invis; b [label="station-bravo\\nSouth Zone\\nHTTP :8000", fillcolor="#d4f4dd"]; vb [label="bravo.db\\n(volume)", shape=cylinder, fillcolor="#f9e79f"]; b -> vb; }
  subgraph cluster_c { label=""; style=invis; c [label="station-charlie\\nEast Zone\\nHTTP :8000", fillcolor="#d4f4dd"]; vc [label="charlie.db\\n(volume)", shape=cylinder, fillcolor="#f9e79f"]; c -> vc; }
  subgraph cluster_d { label=""; style=invis; d [label="station-delta\\nWest Zone\\nHTTP :8000", fillcolor="#d4f4dd"]; vd [label="delta.db\\n(volume)", shape=cylinder, fillcolor="#f9e79f"]; d -> vd; }
  a -> b [dir=both, color="#1b873f", constraint=false, label="peer sync"]; c -> d [dir=both, color="#1b873f", constraint=false];
  a -> c [dir=both, color="#1b873f", constraint=false]; b -> d [dir=both, color="#1b873f", constraint=false];
  a -> d [dir=both, color="#1b873f", constraint=false]; b -> c [dir=both, color="#1b873f", constraint=false];
 }
 user -> dash; dash -> a [lhead=cluster_net, label="REST / JSON to each station"];
}
"""

FLOW_DOT = """
digraph F { rankdir=LR; bgcolor="transparent"; node [shape=box, style="rounded,filled", fillcolor="#eaf2f8", fontname="Helvetica", fontsize=11]; edge [fontname="Helvetica", fontsize=10];
 A [label="Initiator A"]; B [label="Responder B"];
 A -> B [label="1. POST /sync/exchange\\n{summary: id -> version, time, writer, digest}"];
 B -> A [label="2. {send: records A lacks/older,\\nneed: ids B lacks/older}"];
 A -> A [label="3. merge `send`\\n(one atomic SQLite tx)"];
 A -> B [label="4. POST /sync/push {records = need}"];
 B -> B [label="5. validate + merge\\n(idempotent)"];
 B -> A [label="6. per-record result\\napplied | duplicate | stale"];
}
"""


def render(c, snap, controller):
    TABS = ["🏗 Architecture", "🔀 Flows", "🧪 Experiment results", "📡 Live communication metrics"]
    sel = ui.tabs(TABS, "arch_tab")
    if sel == TABS[0]:
        st.graphviz_chart(ARCH_DOT, use_container_width=True)
        st.dataframe(pd.DataFrame([
            ("Streamlit dashboard", "dashboard/", "Stateless control panel and observability. Reads station state over HTTP; writes only on explicit clicks. Holds no records."),
            ("Field station service", "dcn/station.py", "Independent process: HTTP API, validation, own SQLite DB, partition table, sync initiator and responder."),
            ("Persistence", "dcn/db.py", "SQLite (WAL, synchronous=FULL). Atomic transactions; records, sync events, conflicts, peer ack state, links, counters."),
            ("Merge rule", "dcn/merge.py", "Pure deterministic comparison of (version, updated_at, writer, digest)."),
            ("Sync protocol", "dcn/sync.py", "Summary exchange + push; idempotent; outcome logging."),
            ("HTTP client", "dcn/netclient.py", "Standard-library client with timeouts and bounded retries on network errors only."),
            ("Scenarios / experiments", "dcn/scenarios.py, scripts/run_experiments.py", "Repeatable A–E scenarios with verification; measured results."),
        ], columns=["Component", "Where", "Responsibility"]), hide_index=True, use_container_width=True)
    if sel == TABS[1]:
        st.markdown("**Write path:** form → `POST /incidents` on the chosen station → validated → **SQLite commit** → `201`. "
                    "No other station is contacted.")
        st.markdown("**Synchronization flow** (anti-entropy between two stations):")
        st.graphviz_chart(FLOW_DOT, use_container_width=True)
        st.markdown("Partition checks happen **before step 1** at the initiator (`skipped_partition`) and on receipt at the "
                    "responder (`503 link_down` → `rejected_partition`).")
    if sel == TABS[2]:
        if not os.path.exists(RESULTS):
            st.info("No experiment file yet. Generate real measurements with:\n\n"
                    "`python scripts/run_experiments.py` (local processes) or "
                    "`python scripts/run_experiments.py --compose` (against the Docker stack).")
        else:
            with open(RESULTS, encoding="utf-8") as f:
                r = json.load(f)
            st.caption(f"Generated {r['generated_at']} · mode: **{r['mode']}** · {r['platform']} · "
                       "measured by `scripts/run_experiments.py` against real station services.")
            st.markdown("**Scenarios A–E**")
            st.dataframe(pd.DataFrame(r["scenarios"]), hide_index=True, use_container_width=True)
            st.markdown("**Synchronization time vs. number of records created at Alpha** (full network, 4 stations)")
            df = pd.DataFrame(r["sync_scaling"])
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.line_chart(df.set_index("records")["sync_ms"])
            a, b = st.columns(2)
            a.markdown("**Partition recovery**")
            a.json(r["partition_recovery"])
            b.markdown("**Node crash & restart**")
            b.json(r["crash_recovery"])
            st.markdown("**Successful vs failed communication attempts (crash-and-restart experiment: 10 records, Bravo stopped, 15 more records, restart, catch-up)**")
            st.json(r["communication_crash_experiment"])
            st.caption(r.get("notes", ""))
    if sel == TABS[3]:
        ev = ui.events_frame(c, 500)
        if ev.empty:
            st.info("No synchronization events recorded yet.")
        else:
            ini = ev[ev["role"] == "initiator"]
            ok = int((ini["outcome"] == "success").sum())
            bad = int(ini["outcome"].isin(["unreachable", "rejected_partition", "failed", "partial"]).sum())
            sk = int((ini["outcome"] == "skipped_partition").sum())
            m = st.columns(4)
            m[0].metric("Successful exchanges", ok)
            m[1].metric("Failed exchanges", bad)
            m[2].metric("Skipped (partition)", sk)
            m[3].metric("Mean success time", f"{ini[ini['outcome'] == 'success']['ms'].mean():.1f} ms" if ok else "—")
            st.bar_chart(ini.groupby(["station", "outcome"]).size().unstack(fill_value=0))
        rows = [{"station": n, **{k: v for k, v in (s.get("counters") or {}).items()}} for n, s in snap["status"].items() if s["online"]]
        if rows:
            st.markdown("**Per-station counters (from each station's SQLite)**")
            st.dataframe(pd.DataFrame(rows).fillna(0), hide_index=True, use_container_width=True)
