import streamlit as st

# Each concept: definition, where it lives in the code, a practical scenario,
# what to observe in the dashboard, and honest limitations.
CONCEPTS = [
    {"name": "Distributed processes & message passing",
     "definition": "Independent processes with private state that cooperate only by exchanging messages over a network.",
     "component": "`dcn/station.py` (one process per station) · `dcn/netclient.py` · `dcn/sync.py` (HTTP/JSON messages `/sync/exchange`, `/sync/push`).",
     "scenario": "Alpha creates an incident, then Alpha's station sends a real HTTP request to Bravo, Charlie and Delta.",
     "observe": "Overview: four separate stations, each with its own record counts. Sync Explorer → Event history: initiator/responder rows (`msgs_sent`/`msgs_received` counters).",
     "limits": "No shared memory or global clock. Messages are plain HTTP without authentication or encryption (educational prototype).",
     "live": lambda s: ("HTTP messages sent (all stations)", sum(x.get("counters", {}).get("msgs_sent", 0) for x in s if x["online"]))},
    {"name": "Partial failure",
     "definition": "Some components fail while the rest keep running; the system must continue (possibly degraded).",
     "component": "Independent station containers with `restart: unless-stopped`; `Cluster.status_all()` tolerates dead stations.",
     "scenario": "Run `docker compose stop station-bravo`: Alpha, Charlie and Delta keep accepting and syncing records.",
     "observe": "Overview card for Bravo turns OFFLINE; metrics show 3/4 online; Scenario D verifies the rest keep working.",
     "limits": "Only crash-stop failures are exercised (no Byzantine/corrupt nodes, no disk-full or slow-node faults).",
     "live": lambda s: ("Stations offline now", sum(1 for x in s if not x["online"]))},
    {"name": "Network partitions",
     "definition": "The network splits so that groups of live nodes cannot communicate with each other.",
     "component": "`db.set_link/is_blocked` + checks in `sync.sync_with_peer` (skip) and `station._peer` (HTTP 503 `link_down`).",
     "scenario": "Network Simulator → *Isolate Alpha*; create an incident at Alpha; synchronize.",
     "observe": "Red dashed links in the topology; attempts show `skipped_partition`; Alpha's incident is held by 1/4 stations.",
     "limits": "Simulated at the application layer (not by dropping packets). Links are symmetric and binary — no latency, loss or one-way faults.",
     "live": lambda s: ("Partition rejections served", sum(x.get("counters", {}).get("partition_rejections", 0) for x in s if x["online"]))},
    {"name": "Data replication",
     "definition": "Keeping copies of the same data on several nodes for availability and locality.",
     "component": "Every station stores every record (full replication) in its own SQLite DB; `sync.respond_exchange/respond_push`.",
     "scenario": "Scenario A: after one sync, all four databases contain the incident exactly once.",
     "observe": "Incident table: 💾 LOCAL vs 🔁 REPLICATED, and 'Same copy on N/4 stations'.",
     "limits": "Full replication only (no sharding); the summary exchange sends one entry per record, so very large datasets would need Merkle trees or deltas.",
     "live": lambda s: ("Replicated-in records (all stations)", sum(x.get("replicated_in", 0) for x in s if x["online"]))},
    {"name": "Eventual consistency",
     "definition": "If no new updates occur, all replicas eventually hold the same state — but may differ meanwhile.",
     "component": "Pull-push anti-entropy in `dcn/sync.py`; fingerprint comparison in `Cluster.convergence()`.",
     "scenario": "Scenario B then C: replicas differ during the partition, converge after reconnect + sync.",
     "observe": "Sync Explorer banner flips ⚠️ NOT CONVERGED → ✅ CONVERGED; fingerprints become equal.",
     "limits": "Convergence happens only when someone triggers sync (auto-sync is off by default). No bound on staleness; no read-your-writes across stations.",
     "live": lambda s: ("Distinct fingerprints among online stations", len({x["fingerprint"] for x in s if x["online"]}))},
    {"name": "Idempotency",
     "definition": "Applying the same operation more than once has the same effect as applying it once.",
     "component": "`merge.decide` (`duplicate` action when digests match) · `db.apply_remote_batch` · counter `duplicates_ignored`.",
     "scenario": "Sync Explorer → *Re-deliver this record*: the receiver stores nothing new and counts a duplicate.",
     "observe": "'Duplicate deliveries ignored' metric increases; record count is unchanged.",
     "limits": "Idempotent *delivery*, not exactly-once *processing* guarantees for external side effects. There is no delivery guarantee: lost messages are only repaired by the next sync.",
     "live": lambda s: ("Duplicate deliveries ignored", sum(x.get("counters", {}).get("duplicates_ignored", 0) for x in s if x["online"]))},
    {"name": "Conflict resolution",
     "definition": "Deciding deterministically which of two concurrent updates survives so all replicas agree.",
     "component": "`dcn/merge.py` — total order on `(version, updated_at, writer, digest)`; conflicts logged in table `conflicts`.",
     "scenario": "Scenario E: Alpha and Bravo update the same incident while disconnected; after healing everyone keeps the later one.",
     "observe": "Sync Explorer → Conflicts: rule applied, winner, and the metadata of both copies.",
     "limits": "Whole-record last-writer-wins: the losing edit is discarded, not merged. Relies on loosely synchronized clocks. Conflicts are only *detected* when versions are equal; integer versions can't prove causality (a version vector would).",
     "live": lambda s: ("Conflicts detected", sum(x.get("counters", {}).get("conflicts_detected", 0) for x in s if x["online"]))},
    {"name": "Failure detection & timeouts",
     "definition": "Inferring that a peer is down from the absence of a timely reply — inherently unreliable.",
     "component": "`netclient.http_json` (timeout + bounded retries) · `StationApp.probe_peers` · outcome `unreachable`.",
     "scenario": "Stop a station; the others' sync attempts end as `unreachable` after at most `HTTP_TIMEOUT × (retries+1)`.",
     "observe": "Network Simulator reachability matrix: ❌ unreachable (down/timeout) vs ⛔ partitioned (node alive).",
     "limits": "A timeout cannot distinguish a crash from a slow network or partition; the dashboard separates them only because simulated partitions are known to the stations. No heartbeat/gossip detector.",
     "live": lambda s: ("HTTP messages that failed", sum(x.get("counters", {}).get("msgs_failed", 0) for x in s if x["online"]))},
    {"name": "Availability vs consistency (CAP)",
     "definition": "During a partition a system cannot be both fully available and strongly consistent.",
     "component": "Design choice: local-first writes (AP). No quorum, no leader, no consensus.",
     "scenario": "Isolate Alpha: it still accepts incidents (available) while other stations don't see them (inconsistent).",
     "observe": "Alpha keeps accepting forms during partitions; 'Same copy on 1/4 stations'.",
     "limits": "This system does **not** provide strong consistency, consensus, or guaranteed delivery. Two stations can briefly hold different values for the same record.",
     "live": lambda s: ("Local writes accepted (all stations)", sum(x.get("counters", {}).get("local_writes", 0) for x in s if x["online"]))},
    {"name": "Why a central server alone is insufficient",
     "definition": "A single central database is a single point of failure and unreachable from disconnected stations.",
     "component": "No central DB exists: the dashboard holds no records; each station owns its SQLite file in its own Docker volume.",
     "scenario": "Stop the dashboard container: the stations keep accepting writes and (with auto-sync or `curl`) keep synchronizing.",
     "observe": "Isolated stations continue recording incidents — a central-server design would reject them.",
     "limits": "Peers still need *some* path between each other to converge; fully isolated stations stay divergent. Security, authentication and capacity planning are out of scope.",
     "live": lambda s: ("Stations able to accept writes now", sum(1 for x in s if x["online"]))},
]


def render(c, snap, controller):
    st.caption("Pick a concept. Live values come from the running stations, not hard-coded numbers.")
    names = [x["name"] for x in CONCEPTS]
    choice = st.selectbox("Concept", names)
    x = CONCEPTS[names.index(choice)]
    stats = list(snap["status"].values())
    label, val = x["live"](stats)
    st.metric(f"Live: {label}", val)
    a, b = st.columns(2)
    with a:
        st.markdown(f"#### 1 · Definition\n{x['definition']}")
        st.markdown(f"#### 2 · Implementation component\n{x['component']}")
        st.markdown(f"#### 3 · Practical scenario\n{x['scenario']}")
    with b:
        st.markdown(f"#### 4 · What to observe in the dashboard\n{x['observe']}")
        st.markdown(f"#### 5 · Limitations of this implementation\n{x['limits']}")
    st.divider()
    st.error("Not implemented here: consensus (Paxos/Raft), Byzantine fault tolerance, strong consistency, "
             "transactions across stations, or guaranteed delivery.")
    with st.expander("All concepts at a glance"):
        st.dataframe([{"Concept": y["name"], "Component": y["component"].replace("`", "")} for y in CONCEPTS],
                     hide_index=True, use_container_width=True)
