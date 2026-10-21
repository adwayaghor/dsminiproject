import itertools

import pandas as pd
import streamlit as st

from dashboard import ui

STATE_ICON = {"reachable": "✅ reachable", "partitioned": "⛔ partitioned (node alive)",
              "unreachable": "❌ unreachable (down / timeout)"}


def render(c, snap, controller):
    nodes = c.nodes
    pairs = list(itertools.combinations(nodes, 2))
    flags = c.link_flags()

    st.info("**Partition ≠ crash.** A partitioned station is still running and still accepts local writes; "
            "only the *link* is cut. A crashed station is not running at all. From a peer's point of view "
            "both look like “no answer” — a distributed system can't tell them apart with certainty, "
            "which is why this dashboard shows them differently (⛔ vs ❌) using what each station actually observes.")

    st.subheader("Communication links")
    q = st.columns(4)
    if q[0].button("Isolate Alpha", use_container_width=True):
        c.isolate("alpha")
        ui.flash("ok", "Alpha disconnected from all peers (still running).")
        st.rerun()
    if q[1].button("Split: Alpha+Bravo ✕ Charlie+Delta", use_container_width=True):
        for a in ("alpha", "bravo"):
            for b in ("charlie", "delta"):
                c.set_link(a, b, True)
        ui.flash("ok", "Network split into two halves.")
        st.rerun()
    if q[2].button("✅ Reconnect all links", use_container_width=True):
        c.heal_all()
        ui.flash("ok", "All links reconnected. Run a synchronization to heal the data.")
        st.rerun()
    if q[3].button("🔁 Synchronize network", type="primary", use_container_width=True):
        s = c.sync_network()
        ui.flash("ok" if s["convergence"]["converged"] else "warn",
                 f"Sync {s['duration_ms']} ms; converged = {s['convergence']['converged']}. "
                 f"Outcomes: {pd.Series([r['outcome'] for r in s['results']]).value_counts().to_dict()}")
        st.rerun()

    hdr = st.columns([2, 2, 3, 2])
    for h, t in zip(hdr, ("Link", "State", "Configured at each end", "Action")):
        h.markdown(f"**{t}**")
    for a, b in pairs:
        blocked = tuple(sorted((a, b))) in snap["blocked"]
        fa, fb = flags.get(a), flags.get(b)
        ends = []
        for x, y, f in ((a, b, fa), (b, a, fb)):
            ends.append(f"{x}: " + ("offline" if f is None else ("blocked" if y in f else "open")))
        asym = fa is not None and fb is not None and ((b in fa) != (a in fb))
        row = st.columns([2, 2, 3, 2])
        row[0].write(f"{a} ↔ {b}")
        row[1].write("⛔ disconnected" if blocked else "✅ connected")
        row[2].write(" · ".join(ends) + ("  ⚠️ asymmetric" if asym else ""))
        if row[3].button("Reconnect" if blocked else "Disconnect", key=f"lnk_{a}_{b}"):
            res = c.set_link(a, b, not blocked)
            if not all(res.values()):
                ui.flash("warn", f"Link configured only at {[k for k, v in res.items() if v]}; the other end is "
                         f"offline. One side blocking is enough to block traffic, but reconnecting needs both ends.")
            else:
                ui.flash("ok", f"{a} ↔ {b} {'reconnected' if blocked else 'disconnected'}.")
            st.rerun()
    st.caption("Enforced inside the stations: the initiator skips a blocked link (`skipped_partition`) and the "
               "receiver answers HTTP 503 `link_down` to traffic from a blocked peer (`rejected_partition`).")

    st.subheader("Reachability as observed by each station")
    with st.spinner("Probing peers (bounded timeouts)…"):
        probes = c.peers_all()
    rows = []
    for n in nodes:
        p = probes.get(n)
        row = {"from \\ to": n}
        for m in nodes:
            row[m] = "—" if m == n else ("⚫ station offline" if p is None else STATE_ICON.get(p[m]["state"], "?"))
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.subheader("Node failure vs partition")
    cols = st.columns(len(nodes))
    for col, n in zip(cols, nodes):
        with col:
            st.markdown(f"**{ui.label(n)[0]}** {ui.pill(snap['online'][n])}", unsafe_allow_html=True)
            if controller:
                if snap["online"][n]:
                    if st.button("⏹ Stop container", key=f"stop_{n}"):
                        try:
                            controller.stop_node(n)
                            ui.flash("warn", f"{n} stopped (crash). Its volume keeps the data.")
                        except Exception as e:
                            ui.flash("err", f"Could not stop {n}: {e}")
                        st.rerun()
                elif st.button("▶ Start container", key=f"start_{n}"):
                    try:
                        controller.start_node(n)
                        c.wait_online(n, 20)
                        ui.flash("ok", f"{n} restarted. Run a sync to catch it up.")
                    except Exception as e:
                        ui.flash("err", f"Could not start {n}: {e}")
                    st.rerun()
    if not controller:
        st.caption("Container control is not enabled in this dashboard (Docker socket not mounted). Use:")
        st.code("docker compose stop station-bravo     # crash\n"
                "docker compose start station-bravo    # recover\n"
                "docker compose logs -f station-bravo  # watch it come back", language="bash")

    st.subheader("Effect on synchronization attempts")
    ev = ui.events_frame(c, 60)
    if ev.empty:
        st.info("No synchronization attempts yet.")
    else:
        ini = ev[ev["role"] == "initiator"].head(25)
        st.dataframe(ini[["time", "station", "peer", "outcome", "sent", "received", "ms", "detail"]],
                     hide_index=True, use_container_width=True)
        st.caption("`success` · `skipped_partition` (link cut, nothing sent) · `rejected_partition` (peer refused) · "
                   "`unreachable` (timeout / connection refused — node down) · `partial` · `failed`.")

    with st.expander("📚 Why are network partitions a fundamental challenge?"):
        st.markdown(
            "- Networks fail in ways nodes cannot distinguish from slow or dead peers: a timeout tells you *something* is wrong, not *what*.\n"
            "- During a partition each side must choose: **stay available** (accept writes, risk divergence) or **stay consistent** (refuse writes). "
            "This is the **CAP** trade-off. This system chooses availability — emergency stations must keep recording — "
            "and accepts that replicas diverge temporarily (**eventual consistency**).\n"
            "- After healing, divergent histories must be reconciled deterministically, otherwise replicas disagree forever.\n"
            "- A single central server can't help: stations cut off from it can't write at all.")
