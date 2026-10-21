"""Tiny HTTP/JSON client on the standard library with explicit timeouts and bounded retries."""
import json
import socket
import time
import urllib.error
import urllib.request


class NetError(Exception):
    """Network-level failure: connection refused, DNS failure, timeout."""

    def __init__(self, kind, detail):
        self.kind = kind          # 'timeout' | 'connection'
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


def http_json(method, url, body=None, headers=None, timeout=2.0, retries=1, backoff=0.15):
    """Return (status, parsed_json). HTTP error statuses are returned, not raised.

    Only network-level errors are retried (bounded by `retries`); a 4xx/5xx reply
    is a definite answer from the peer and is returned immediately.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _parse(e.read())
        except (socket.timeout, TimeoutError) as e:
            last = NetError("timeout", str(e) or "timed out")
        except urllib.error.URLError as e:
            reason = e.reason
            kind = "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "connection"
            last = NetError(kind, str(reason))
        except (ConnectionError, OSError) as e:
            last = NetError("connection", str(e))
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    raise last


def _parse(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"error": "non_json_response"}
