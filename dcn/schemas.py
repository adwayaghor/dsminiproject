"""Explicit schemas and validation for every piece of data that enters a station."""
from .util import ID_RE, NODE_RE, TS_RE, record_digest

CATEGORIES = ("flood", "fire", "earthquake", "medical emergency",
              "infrastructure failure", "other")
SEVERITIES = ("low", "medium", "high", "critical")
INCIDENT_STATUSES = ("open", "investigating", "resolved")
RESOURCE_TYPES = ("medical supplies", "food", "drinking water",
                  "rescue equipment", "shelter", "transport")
PRIORITIES = ("low", "medium", "high", "urgent")
REQUEST_STATUSES = ("pending", "acknowledged", "fulfilled", "cancelled")
# Allowed local status transitions for resource requests.
TRANSITIONS = {
    "pending": {"acknowledged", "cancelled"},
    "acknowledged": {"fulfilled", "cancelled"},
    "fulfilled": set(),
    "cancelled": set(),
}
KINDS = ("incident", "request")
MAX_TEXT = 2000

INCIDENT_EDITABLE = ("title", "description", "category", "severity", "location", "status")
REQUEST_EDITABLE = ("quantity", "priority", "status", "notes", "location")


class ValidationError(ValueError):
    def __init__(self, details, code="validation"):
        self.details = [details] if isinstance(details, str) else list(details)
        self.code = code
        super().__init__("; ".join(self.details))


def _text(d, key, errs, required=True, max_len=200, default=""):
    v = d.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        errs.append(f"{key} must be a string")
        return default
    v = v.strip()
    if required and not v:
        errs.append(f"{key} is required")
    if len(v) > max_len:
        errs.append(f"{key} must be at most {max_len} characters")
    return v


def _choice(d, key, options, errs, default=None):
    v = d.get(key, default)
    if v not in options:
        errs.append(f"{key} must be one of {list(options)}")
    return v


def validate_incident_payload(d, node_id=None):
    if not isinstance(d, dict):
        raise ValidationError("incident must be a JSON object")
    errs = []
    out = {
        "title": _text(d, "title", errs, max_len=120),
        "description": _text(d, "description", errs, required=False, max_len=MAX_TEXT),
        "category": _choice(d, "category", CATEGORIES, errs),
        "severity": _choice(d, "severity", SEVERITIES, errs),
        "location": _text(d, "location", errs, max_len=120),
        "reporting_station": _text(d, "reporting_station", errs, max_len=16,
                                   default=node_id or ""),
        "status": _choice(d, "status", INCIDENT_STATUSES, errs, default="open"),
    }
    if node_id and out["reporting_station"] != node_id:
        errs.append("reporting_station must be the station receiving the report")
    if errs:
        raise ValidationError(errs)
    return out


def validate_request_payload(d, node_id=None):
    if not isinstance(d, dict):
        raise ValidationError("request must be a JSON object")
    errs = []
    q = d.get("quantity")
    if isinstance(q, bool) or not isinstance(q, int) or not (1 <= q <= 100000):
        errs.append("quantity must be an integer between 1 and 100000")
    out = {
        "resource_type": _choice(d, "resource_type", RESOURCE_TYPES, errs),
        "quantity": q,
        "priority": _choice(d, "priority", PRIORITIES, errs),
        "status": _choice(d, "status", REQUEST_STATUSES, errs, default="pending"),
        "requesting_station": _text(d, "requesting_station", errs, max_len=16,
                                    default=node_id or ""),
        "location": _text(d, "location", errs, required=False, max_len=120),
        "notes": _text(d, "notes", errs, required=False, max_len=MAX_TEXT),
    }
    if node_id and out["requesting_station"] != node_id:
        errs.append("requesting_station must be the station receiving the request")
    if errs:
        raise ValidationError(errs)
    return out


def validate_payload(kind, payload, node_id=None):
    if kind == "incident":
        return validate_incident_payload(payload, node_id)
    if kind == "request":
        return validate_request_payload(payload, node_id)
    raise ValidationError(f"kind must be one of {list(KINDS)}")


def apply_changes(kind, current_payload, changes):
    """Validate a local update and return the new payload.

    Only whitelisted fields can change; request status obeys TRANSITIONS.
    """
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("changes must be a non-empty JSON object")
    editable = INCIDENT_EDITABLE if kind == "incident" else REQUEST_EDITABLE
    bad = [k for k in changes if k not in editable]
    if bad:
        raise ValidationError(f"fields not editable: {bad}; editable: {list(editable)}")
    new = dict(current_payload)
    new.update(changes)
    if kind == "request" and "status" in changes and changes["status"] != current_payload["status"]:
        old, nxt = current_payload["status"], changes["status"]
        if nxt not in REQUEST_STATUSES:
            raise ValidationError(f"status must be one of {list(REQUEST_STATUSES)}")
        if nxt not in TRANSITIONS[old]:
            raise ValidationError(f"illegal status transition {old} -> {nxt}",
                                  code="invalid_transition")
    return validate_payload(kind, new)  # node check skipped: stations may update peers' records


def validate_record_envelope(rec):
    """Validate a record received from a peer. Also verifies its integrity digest."""
    if not isinstance(rec, dict):
        raise ValidationError("record must be an object")
    errs = []
    need = ("id", "kind", "payload", "version", "updated_at", "writer",
            "origin", "created_at", "deleted", "digest")
    missing = [k for k in need if k not in rec]
    if missing:
        raise ValidationError(f"missing fields: {missing}")
    if not isinstance(rec["id"], str) or not ID_RE.match(rec["id"]):
        errs.append("bad id")
    if rec["kind"] not in KINDS:
        errs.append("bad kind")
    if isinstance(rec["version"], bool) or not isinstance(rec["version"], int) or rec["version"] < 1:
        errs.append("version must be an integer >= 1")
    for f in ("updated_at", "created_at"):
        if not isinstance(rec[f], str) or not TS_RE.match(rec[f]):
            errs.append(f"{f} must be an ISO-8601 UTC timestamp")
    for f in ("writer", "origin"):
        if not isinstance(rec[f], str) or not NODE_RE.match(rec[f]):
            errs.append(f"{f} must be a node id")
    if not isinstance(rec["deleted"], bool):
        errs.append("deleted must be boolean")
    if errs:
        raise ValidationError(errs)
    try:
        validate_payload(rec["kind"], rec["payload"])
    except ValidationError as e:
        raise ValidationError([f"payload: {m}" for m in e.details])
    if record_digest(rec) != rec["digest"]:
        raise ValidationError("digest mismatch (record corrupted or tampered)")
    return rec
