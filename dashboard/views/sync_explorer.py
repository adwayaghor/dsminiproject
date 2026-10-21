import json

import pandas as pd
import streamlit as st

from dashboard import ui
from dcn import scenarios


def _matrices(snap):
    out = {}
    for kind in ("incident", "request"):
        df = ui.network_matrix(snap, kind)
        if not df.empty:
            out[kind] = df
    return pd.concat(out.values()) if out else pd.DataFrame()


def render(c, snap, controller):
    nodes = c.nodes
    conv = c.convergence()
    if conv["converged"]:
        st.success("✅ CONVERGED — all online stations hold identical data (equal fingerprints).")
    else:
        st.warning("⚠️ NOT CONVERGED — online stations differ. This is normal during a partition or before a sync "
                   "(eventual consistency).")
    if conv["offline"]:
        st.error(f"Offline stations (excluded from the comparison): {', '.join(conv['offline'])}")

    TABS = ["🔁 Synchronize", "🧮 Replica comparison", "📜 Event history",
                                  "⚔️ Conflicts", "🎬 Guided scenarios A–E"]
    sel = ui.tabs(TABS, "sx_tab")

    # ---------------------------------------------------------------- synchronize
    if sel == TABS[0]:
        a, b = st.columns(2)
        if a.button("🔁 Synchronize whole network", type="primary", use_container_width=True):
            before = _matrices(snap)
            s = c.sync_network()
            after = _matrices(ui.take_snapshot(c))
            st.session_state["last_sync"] = {"result": s, "before": before, "after": after}
            st.rerun()
        node = b.selectbox("…or only one station initiates:", nodes, format_func=ui.station_title,
                           label_visibility="collapsed")
        if b.button(f"Sync from {node} only", use_container_width=True):
            before = _matrices(snap)
            code, body = c.sync_node(node)
            if code == 200:
                after = _matrices(ui.take_snapshot(c))
                res = [dict(r, initiator=node, round=1) for r in body["results"]]
                st.session_state["last_sync"] = {"result": {"results": res, "duration_ms": sum(r["duration_ms"] for r in res),
                                                            "rounds": 1, "convergence": c.convergence()},
                                                 "before": before, "after": after}
            else:
                ui.flash("err", f"{node} did not answer: {body}")
            st.rerun()

        last = st.session_state.get("last_sync")
        if last:
            s = last["result"]
            st.markdown(f"**Last run:** {s['duration_ms']} ms · {s['rounds']} round(s) · "
                        f"converged = **{s['convergence']['converged']}**")
            df = pd.DataFrame([{k: r[k] for k in ("round", "initiator", "peer", "outcome", "sent", "received",
                                                  "duplicates", "stale", "conflicts", "invalid", "duration_ms", "detail")}
                               for r in s["results"]])
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.markdown("**Before-and-after replica comparison**")
            x, y = st.columns(2)
            x.caption("BEFORE")
            x.dataframe(last["before"], hide_index=True, use_container_width=True)
            y.caption("AFTER")
            y.dataframe(last["after"], hide_index=True, use_container_width=True)

        st.divider()
        st.markdown("**Idempotency demo — deliver the same record again**")
        ids = list(snap["matrix"])
        if ids:
            d1, d2, d3 = st.columns(3)
            rid = d1.selectbox("Record", ids, key="dup_rid")
            holders = [n for n in nodes if snap["matrix"][rid].get(n)]
            src = d2.selectbox("Send from", holders, key="dup_src") if holders else None
            dst = d3.selectbox("To", [n for n in nodes if n != src], key="dup_dst")
            if src and st.button("Re-deliver this record"):
                code, body = c.call(src, "POST", "/admin/redeliver", {"peer": dst, "record_id": rid}, timeout=5)
                st.json(body)
                ui.flash("info", "Re-delivered; see the `duplicates` counter below (nothing was duplicated).")
        else:
            st.caption("Create a record first.")
        tot = {k: sum((s.get("counters") or {}).get(k, 0) for s in snap["status"].values() if s["online"])
               for k in ("duplicates_ignored", "stale_ignored", "conflicts_detected", "invalid_rejected")}
        m = st.columns(4)
        m[0].metric("Duplicate deliveries ignored", tot["duplicates_ignored"])
        m[1].metric("Stale (older) updates ignored", tot["stale_ignored"])
        m[2].metric("Conflicts detected", tot["conflicts_detected"])
        m[3].metric("Invalid records rejected", tot["invalid_rejected"])

    # ---------------------------------------------------------------- comparison
    if sel == TABS[1]:
        df = _matrices(snap)
        if df.empty:
            st.info("No records yet.")
        else:
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.markdown("**Records missing from individual replicas**")
            miss = []
            for n in nodes:
                if not snap["online"][n]:
                    miss.append({"station": n, "state": "offline", "missing": "—"})
                    continue
                lack = [i for i, per in snap["matrix"].items() if per.get(n) is None]
                stale = [i for i, per in snap["matrix"].items() if per.get(n) is not None
                         and per[n]["digest"] != max((r for r in per.values() if r),
                                                     key=lambda r: (r["version"], r["updated_at"], r["writer"], r["digest"]))["digest"]]
                miss.append({"station": n, "state": "online", "missing": len(lack),
                             "outdated": len(stale), "ids": ", ".join(lack + stale) or "none"})
            st.dataframe(pd.DataFrame(miss), hide_index=True, use_container_width=True)
        st.markdown("**Fingerprints**")
        st.dataframe(pd.DataFrame([{"station": n, "fingerprint": s.get("fingerprint", "—"),
                                    "pending sync ops": s.get("pending_total", "—"),
                                    "pending per peer": json.dumps(s.get("pending_by_peer", {}))}
                                   for n, s in snap["status"].items()]), hide_index=True, use_container_width=True)

    # ---------------------------------------------------------------- events
    if sel == TABS[2]:
        ev = ui.events_frame(c, 300)
        if ev.empty:
            st.info("No synchronization events yet.")
        else:
            f1, f2 = st.columns(2)
            role = f1.multiselect("Role", ["initiator", "responder"], default=["initiator"])
            outs = f2.multiselect("Outcome", sorted(ev["outcome"].unique()), default=sorted(ev["outcome"].unique()))
            view = ev[ev["role"].isin(role) & ev["outcome"].isin(outs)]
            st.dataframe(view, hide_index=True, use_container_width=True)
            ini = ev[ev["role"] == "initiator"]
            if not ini.empty:
                cc = st.columns(2)
                cc[0].bar_chart(ini["outcome"].value_counts())
                ok = ini[ini["outcome"] == "success"].sort_values("time")
                if not ok.empty:
                    cc[1].line_chart(ok.set_index("time")["ms"])
                    cc[1].caption("Duration of successful exchanges (ms).")

    # ---------------------------------------------------------------- conflicts
    if sel == TABS[3]:
        conf = c.conflicts_all()
        rows = []
        for n, cs in conf.items():
            for x in cs or []:
                rows.append({"detected at": n, "time": x["ts"], "record": x["record_id"], "rule applied": x["rule"],
                             "winner": x["winner"], "local copy (writer/v/time)":
                             f"{x['local']['writer']} / v{x['local']['version']} / {ui.short_ts(x['local']['updated_at'])}",
                             "remote copy (writer/v/time)":
                             f"{x['remote']['writer']} / v{x['remote']['version']} / {ui.short_ts(x['remote']['updated_at'])}",
                             "from": x["from_node"]})
        if not rows:
            st.info("No conflicts so far. Run **Scenario E** to produce one.")
        else:
            st.dataframe(pd.DataFrame(rows).sort_values("time", ascending=False), hide_index=True,
                         use_container_width=True)
        st.markdown(
            "**Deterministic rule** — compare `(version, updated_at, writer, digest)`; the larger tuple wins:\n"
            "1. higher **version** wins · 2. equal version ⇒ *conflict*; later **updated_at** wins · "
            "3. equal timestamp ⇒ higher **writer node id** wins · 4. final tie ⇒ larger digest.\n\n"
            "*Limitation:* whole-record last-writer-wins; the losing concurrent edit is discarded (logged here, not merged). "
            "Timestamps come from node clocks, so clock skew can pick a surprising winner.")

    # ---------------------------------------------------------------- scenarios
    if sel == TABS[4]:
        st.markdown("Each scenario **resets the stations**, performs real operations over HTTP, and verifies the "
                    "outcome from the stations' own data.")
        cols = st.columns(5)
        picks = {"A": "A · Normal operation", "B": "B · Network partition", "C": "C · Network recovery",
                 "D": "D · Crash & restart", "E": "E · Concurrent updates"}
        for col, (k, t) in zip(cols, picks.items()):
            if col.button(t, key=f"sc_{k}", use_container_width=True, disabled=(k == "D" and not controller)):
                with st.spinner(f"Running scenario {k}…"):
                    res = scenarios.run(k, c, controller)
                st.session_state.setdefault("scn", {})[k] = res
                st.rerun()
        if not controller:
            st.info("Scenario D needs container control (Docker socket mount). Without it, use the **guided manual "
                    "flow** below with `docker compose stop/start` commands.")
            m1, m2, m3 = st.columns(3)
            if m1.button("D1 · Prepare baseline"):
                res = scenarios.scenario_d_prepare(c)
                st.session_state["d_ids"] = {"baseline": res["data"].get("baseline_id")}
                st.session_state.setdefault("scn", {})["D1"] = res
                st.rerun()
            if m2.button("D2 · Work while Bravo is down"):
                res = scenarios.scenario_d_while_down(c)
                st.session_state.setdefault("d_ids", {})["missed"] = res["data"].get("missed_id")
                st.session_state.setdefault("scn", {})["D2"] = res
                st.rerun()
            if m3.button("D3 · Verify recovery"):
                ids = st.session_state.get("d_ids", {})
                if ids.get("baseline") and ids.get("missed"):
                    st.session_state.setdefault("scn", {})["D3"] = scenarios.scenario_d_recover(
                        c, ids["baseline"], ids["missed"])
                else:
                    ui.flash("err", "Run D1 and D2 first.")
                st.rerun()
        for k, res in st.session_state.get("scn", {}).items():
            icon = "✅" if res["passed"] else "❌"
            with st.expander(f"{icon} Scenario {k} — {res['title']}  ({res['duration_ms']} ms)", expanded=True):
                for s in res["steps"]:
                    st.write(f"{'✅' if s['ok'] else '❌'} {s['text']}" + (f" — `{s['detail']}`" if s["detail"] else ""))
                if k == "E" and res["data"].get("alpha_update"):
                    st.markdown("**Conflict metadata used for the decision**")
                    st.dataframe(pd.DataFrame([
                        dict(station="alpha", **res["data"]["alpha_update"]),
                        dict(station="bravo", **res["data"]["bravo_update"])]), hide_index=True)
                    st.success(f"Winner: **{res['data']['winner']}** — equal version (v2) ⇒ later `updated_at` wins.")
