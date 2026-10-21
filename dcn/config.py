"""Station configuration, read from environment variables (set in compose.yaml)."""
import os
from dataclasses import dataclass, field

from .util import NODE_RE


def parse_peers(text):
    """'alpha=http://station-alpha:8000,bravo=http://station-bravo:8000' -> dict"""
    out = {}
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        node, _, url = part.partition("=")
        node, url = node.strip(), url.strip().rstrip("/")
        if not NODE_RE.match(node) or not url.startswith("http"):
            raise ValueError(f"bad peer entry: {part!r}")
        out[node] = url
    return out


@dataclass
class StationConfig:
    node_id: str
    name: str
    zone: str
    port: int = 8000
    db_path: str = "/data/station.db"
    peers: dict = field(default_factory=dict)
    http_timeout: float = 2.0       # per-request timeout (seconds)
    http_retries: int = 1           # extra attempts after the first (network errors only)
    auto_sync_seconds: float = 0    # 0 = only explicit, user-triggered sync

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        node_id = env["NODE_ID"].strip().lower()
        if not NODE_RE.match(node_id):
            raise ValueError("NODE_ID must match [a-z][a-z0-9_]{0,15}")
        peers = parse_peers(env.get("PEERS", ""))
        peers.pop(node_id, None)
        return cls(
            node_id=node_id,
            name=env.get("NODE_NAME", f"Station {node_id.title()}"),
            zone=env.get("ZONE", ""),
            port=int(env.get("PORT", "8000")),
            db_path=env.get("DB_PATH", "/data/station.db"),
            peers=peers,
            http_timeout=float(env.get("HTTP_TIMEOUT", "2.0")),
            http_retries=int(env.get("HTTP_RETRIES", "1")),
            auto_sync_seconds=float(env.get("AUTO_SYNC_SECONDS", "0")),
        )
