import pandas as pd
import streamlit as st

from dashboard import ui


def topology_dot(snap, nodes):
    lines = ['graph G {', 'layout=circo; bgcolor="transparent"; pad=0.3;',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, margin="0.25,0.15"];',
             'edge [fontname="Helvetica", fontsize=10];']
    for n in nodes:
        name, zone = ui.label(n)
        s = snap["status"][n]
        if s["online"]:
            txt = f'{name}\\n{zone}\\n{s["incidents"]} inc · {s["requests"]} req'
            lines.append(f'{n} [label="{txt}", fillcolor="#d4f4dd", color="#1b873f"];')
        else:
            lines.append(f'{n} [label="{name}\\n{zone}\\nOFFLINE (crashed/stopped)", '
                         f'fillcolor="#e0e0e0", color="#7f8c8d", fontcolor="#555555", style="rounded,filled,dashed"];')
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if tuple(sorted((a, b))) in snap["blocked"]:
                lines.append(f'{a} -- {b} [color="#c0392b", style=dashed, penwidth=2, label="partitioned"];')
            elif snap["online"][a] and snap["online"][b]:
                lines.append(f'{a} -- {b} [color="#1b873f", penwidth=2];')
            else:
                lines.append(f'{a} -- {b} [color="#bdc3c7", style=dotted];')
    lines.append("}")
    return "\n".join(lines)


def render(c, snap, controller):
    nodes, status = c.nodes, snap["status"]
    online = [n for n in nodes if status[n]["online"]]
    inc_ids = {i for i, per in snap["matrix"].items()
               if any(r and not r["deleted"] and r["kind"] == "incident" for r in per.values())}
    req_ids = {i for i, per in snap["matrix"].items()
               if any(r and not r["deleted"] and r["kind"] == "request" for r in per.values())}
    conv = c.convergence()

    m = st.columns(5)
    m[0].metric("Stations online", f"{len(online)} / {len(nodes)}")
    m[1].metric("Unique incidents (network)", len(inc_ids))
    m[2].metric("Unique resource requests", len(req_ids))
    m[3].metric("Active partitioned links", len(snap["blocked"]))
    m[4].metric("Replicas converged", "✅ yes" if conv["converged"] else "⚠️ not yet")

    st.subheader("Field stations")
    cols = st.columns(len(nodes))
    for col, n in zip(cols, nodes):
        s = status[n]
        name, zone = ui.label(n)
        with col:
            if s["online"]:
                body = (f'<div class="card"><h4>{name}</h4><div class="muted">{zone} · node id <code>{n}</code></div>'
                        f'<p>{ui.pill(True)}</p>'
                        f'<div class="small">Local DB: <b>{s["incidents"]}</b> incidents · <b>{s["requests"]}</b> requests<br>'
                        f'Created here: <b>{s["local_origin"]}</b> · replicated in: <b>{s["replicated_in"]}</b><br>'
                        f'Pending sync ops: <b>{s["pending_total"]}</b><br>'
                        f'Sync ok/failed/skipped: <b>{s["sync_success"]}/{s["sync_failed"]}/{s["sync_skipped"]}</b><br>'
                        f'Last success: <b>{ui.short_ts(s["last_success_at"])}</b><br>'
                        f'Blocked links: <b>{", ".join(s["blocked_links"]) or "none"}</b><br>'
                        f'Up since: <span class="muted">{ui.short_ts(s["started_at"])} UTC</span></div></div>')
            else:
                body = (f'<div class="card"><h4>{name}</h4><div class="muted">{zone} · node id <code>{n}</code></div>'
                        f'<p>{ui.pill(False)}</p><div class="small">Not answering HTTP. Its SQLite volume is '
                        f'intact; records reappear when the container restarts.</div></div>')
            st.markdown(body, unsafe_allow_html=True)

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Network topology")
        st.graphviz_chart(topology_dot(snap, nodes), use_container_width=True)
        st.caption("Green = healthy link · red dashed = partitioned (simulated at application level, "
                   "nodes still running) · grey node = crashed/stopped container.")
    with right:
        st.subheader("Replication status")
        rows = [{"station": n, "online": "✅" if status[n]["online"] else "⚫ offline",
                 "incidents": status[n].get("incidents", "—"), "requests": status[n].get("requests", "—"),
                 "pending ops": status[n].get("pending_total", "—"),
                 "fingerprint": status[n].get("fingerprint", "—")} for n in nodes]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("Equal fingerprints (hash of every record id + content digest) ⇒ identical replicas.")
        df = pd.DataFrame({"incidents": [status[n].get("incidents", 0) for n in nodes],
                           "requests": [status[n].get("requests", 0) for n in nodes]}, index=nodes)
        st.bar_chart(df)

    st.subheader("Architecture in one paragraph")
    st.markdown(
        "Four **independent field-station services** (separate containers) each own a SQLite database. "
        "Writes are saved **locally first** and replicated only when stations exchange summaries over HTTP "
        "(**anti-entropy sync**, version-based merge, idempotent). This dashboard is a stateless control "
        "panel: it holds no records and cannot be a single point of failure — if it disappears the stations "
        "keep working and syncing.")
    ui.explain("system overview",
               "- **Partial failure**: one station can be OFFLINE while the others work.\n"
               "- **Partition vs crash**: a red dashed link means the nodes are alive but cannot talk.\n"
               "- **Eventual consistency**: replicas can differ (⚠️ not yet converged) until a sync runs.")
