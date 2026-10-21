"""Pure, deterministic merge rule. No I/O, so it is trivial to unit-test.

Every record carries (version, updated_at, writer). Two copies of the same
record id are compared by the tuple

    (version, updated_at, writer, digest)        -> larger tuple wins

Rule text shown in the dashboard:
  1. higher `version` wins (a newer update never loses to an older one)
  2. equal version  -> concurrent update (conflict); later `updated_at` wins
  3. equal timestamp -> higher `writer` node id wins (lexicographic)
  4. still equal     -> larger digest wins (total order, so always deterministic)

Because the order is total and the same on every node, merging is commutative,
associative and idempotent: any delivery order converges to the same winner.
"""


def order_key(rec):
    return (rec["version"], rec["updated_at"], rec["writer"], rec["digest"])


def decide(local, remote):
    """Return a decision dict for merging `remote` into `local` (local may be None).

    action: insert | replace | duplicate | keep
    conflict: True when both sides hold the same version with different content
    rule: human-readable reason for the winner
    """
    if local is None:
        return {"action": "insert", "conflict": False, "winner": "remote",
                "rule": "record unknown locally"}
    if local["digest"] == remote["digest"]:
        return {"action": "duplicate", "conflict": False, "winner": "local",
                "rule": "identical copy already stored (idempotent no-op)"}
    kl, kr = order_key(local), order_key(remote)
    conflict = local["version"] == remote["version"]
    if local["version"] != remote["version"]:
        rule = "higher version wins"
    elif local["updated_at"] != remote["updated_at"]:
        rule = "equal version: later updated_at wins"
    elif local["writer"] != remote["writer"]:
        rule = "equal version & timestamp: higher writer node id wins"
    else:
        rule = "equal version, timestamp & writer: larger digest wins"
    remote_wins = kr > kl
    return {"action": "replace" if remote_wins else "keep", "conflict": conflict,
            "winner": "remote" if remote_wins else "local", "rule": rule}


def merge_pair(a, b):
    """Return the surviving record when merging two copies (for property tests)."""
    return b if order_key(b) > order_key(a) else a
