# Viva preparation

Answers are specific to *this* implementation. Where a property is **not** provided, say so — examiners reward accuracy over claims.

## Design and architecture

**1. Why is this a distributed system and not a client/server app?**
Four independent processes (separate containers), each with private state (its own SQLite file in its own volume), cooperating only through HTTP messages, with no shared memory or global clock, and failing independently. The dashboard holds no data; if it disappears the stations keep working.

**2. Why not a central server?**
Stations cut off from it could not record anything, and it would be a single point of failure. Here each station accepts writes locally (availability) and reconciles later. `dcn/station.py`, `dcn/db.py`.

**3. Where do you choose between consistency and availability?**
Local-first writes: a station accepts writes during a partition (AP in CAP terms) and replicas may diverge until sync. We do **not** offer strong consistency or consensus. `Concepts` page, "Availability vs consistency".

**4. Why HTTP and `http.server`?**
Requirement to stay on the standard library and use real network calls between containers; `ThreadingHTTPServer` is enough for a prototype. Limitation: no TLS/auth, not tuned for load.

**5. Why SQLite per station?**
Durable local state with transactions and zero administration; WAL mode + `synchronous=FULL` so a committed write survives a process kill. One DB file per station, never shared.

## Synchronization and consistency

**6. Explain the sync protocol.**
Initiator sends a summary `{id: [version, updated_at, writer, digest]}` to `/sync/exchange`; the responder compares with its own, returns records the initiator lacks or has older (`send`) and the ids it wants (`need`); the initiator merges `send` atomically, then pushes `need` records to `/sync/push`. Code: `dcn/sync.py`. This is pull-push **anti-entropy**.

**7. What is the merge rule and why is it deterministic?**
Larger tuple `(version, updated_at, writer, digest)` wins (`dcn/merge.py`). A total order that is the same on every node means merge is commutative, associative and idempotent, so any delivery order yields the same result (unit test checks all 24 orderings of four competing copies).

**8. How do you guarantee idempotency?**
Re-applying a record whose digest equals the stored one is a no-op counted as `duplicates_ignored`; older versions are `stale_ignored`. Repeated syncs create no duplicates (test `test_repeated_sync_creates_no_duplicates`).

**9. What if a sync fails halfway?**
Each merge batch runs in one SQLite transaction (all-or-nothing). If the pull succeeded but the push failed the outcome is `partial` and the next sync repairs it. Test `test_batch_is_atomic`.

**10. How do you detect and resolve conflicts?**
Same record id with equal `version` but different content = concurrent update. We log both copies and the rule used, then keep the larger key (later `updated_at`, then higher node id). **Limitation:** whole-record last-writer-wins loses the other edit; integer versions can't detect all concurrency — a version vector or CRDT would.

**11. Why not vector clocks / CRDTs / Raft?**
Scope: the brief asked for simple version-based reconciliation. Vector clocks would detect true concurrency; Raft would give strong consistency but would make minority partitions unavailable for writes — the opposite of the stated goal.

**12. How are deletions handled?**
As tombstones: a delete is a normal versioned update with `deleted=true`, so a stale live copy (lower version) cannot resurrect it. Tombstones are never garbage-collected (limitation).

**13. Is delivery guaranteed? Is it exactly-once?**
No guarantee: a message can be lost; the next sync repairs it. Effects are idempotent, so at-least-once delivery is safe.

**14. How is convergence determined?**
Each station computes a fingerprint = hash of all `(id, digest)`; equal fingerprints across online stations ⇒ identical replicas (`Cluster.convergence`). Pending ops = records whose current digest is not confirmed at a peer (`peer_state` table).

**15. Convergence guarantee?**
If stations stop receiving writes and the connectivity graph (without blocked links) lets information flow between all of them, repeated syncs converge. In a true partition they cannot — and the UI shows ⚠️ not converged.

## Failures and timeouts

**16. Crash vs partition?**
Crash: the process/container is down → `unreachable` (connection refused / timeout). Partition: nodes are alive but the link is cut → `skipped_partition` (initiator) or `503 link_down` (receiver). From a peer's viewpoint both look like missing replies, which is why failure detection is inherently unreliable; we tell them apart only because stations know their simulated partition table.

**17. How is the partition simulated?**
Per-station `links` table checked inside the sync code and by the HTTP handlers for peer traffic — not a dashboard label. Test `test_one_sided_block_is_enforced_by_remote_rejection`.

**18. What are your timeouts/retries?**
`HTTP_TIMEOUT=2s`, `HTTP_RETRIES=1` with short backoff, only for network errors; HTTP error replies are not retried. Dashboard calls use 2–6 s limits and run in parallel, so a dead peer never blocks it.

**19. What survives a crash?**
Everything committed to SQLite: `synchronous=FULL` + WAL. Tested with SIGKILL (`test_state_survives_hard_kill_and_restart`) and in Docker via container stop/start and `docker compose down`/`up`.

**20. Docker `restart: unless-stopped` and health checks — what do they prove?**
Restart policy revives a crashed process; a manual `stop` is respected. Health checks show the HTTP server answers `/health`; they say nothing about peer reachability. `depends_on` ordering only holds at startup.

## Practical / evaluation

**21. How did you test?**
45 `unittest` tests: unit (persistence, validation, merge, atomicity) and integration with four real station processes (HTTP validation, sync, partition enforcement, recovery, SIGKILL restart, scenarios A–E and repeatability). Docker stack tested end-to-end including container stop/start through the Docker socket and `down`/`up` persistence.

**22. How are the dashboard metrics computed?**
From the stations' tables: `sync_events` (outcomes, durations), `counters` (duplicates, stale, conflicts, messages), `peer_state` (pending), `records` (counts, fingerprint). No hard-coded values.

**23. What does the measured sync time depend on?**
Number of stations (sequential HTTP round trips), summary size (O(records)), and SQLite fsync cost. See `results/experiments.md`; numbers are single-machine, not WAN.

**24. What are the biggest limitations?**
Eventual consistency only; LWW loses concurrent edits; clock skew can pick odd winners; full-summary exchange does not scale to huge datasets; unauthenticated, unencrypted HTTP; symmetric on/off partitions only; tombstones never pruned; manual/explicit sync by default.

**25. How would you improve it?**
Version vectors or CRDT field merges; Merkle-tree/delta sync; background gossip with failure detector; mutual TLS; clock handling (hybrid logical clocks); tombstone GC; real network fault injection (tc/netem, Toxiproxy-style).

**26. Tricky: "Two stations updated the same incident while disconnected; which one wins and why?"**
Both produce version 2; versions tie → conflict; later `updated_at` wins; if equal, higher writer id (`bravo` > `alpha`). Every station applies the same rule so all converge to the same copy, and `Conflicts` shows both sides' metadata.

**27. Tricky: "Is your system consistent?"**
Not at all times. It is eventually consistent: it guarantees *convergence to the same state given enough connectivity and no further writes*, not that two stations always agree.
