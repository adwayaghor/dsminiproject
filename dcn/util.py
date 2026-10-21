"""Small shared helpers: timestamps, canonical JSON, digests, ids."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

NODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
ID_RE = re.compile(r"^(INC|REQ)-[A-Z0-9_]{1,16}-[0-9a-f]{8}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

# Fields that make up a record's identity for hashing. `digest` itself and
# local bookkeeping columns (received_from/received_at) are NOT included.
DIGEST_FIELDS = ("id", "kind", "payload", "version", "updated_at",
                 "writer", "origin", "created_at", "deleted")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_digest(rec: dict) -> str:
    core = {k: rec[k] for k in DIGEST_FIELDS}
    core["deleted"] = bool(core["deleted"])
    return hashlib.sha256(canonical(core).encode("utf-8")).hexdigest()[:20]


def new_id(kind: str, node_id: str) -> str:
    prefix = "INC" if kind == "incident" else "REQ"
    return f"{prefix}-{node_id.upper()}-{uuid.uuid4().hex[:8]}"


def fingerprint(pairs) -> str:
    """Order-independent hash of (id, digest) pairs; equal => identical replicas."""
    h = hashlib.sha256()
    for rid, dg in sorted(pairs):
        h.update(f"{rid}:{dg}\n".encode())
    return h.hexdigest()[:16]
