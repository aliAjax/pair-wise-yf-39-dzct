from datetime import date, datetime, timedelta
import math

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


OBSERVATION_CONTENT_FIELDS = ("species", "location", "lat", "lon")
OBSERVATION_IDENTITY_FIELDS = ("event_id", "observed_at")


def _normalize_observed_at(value):
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValidationError("observed_at must be a valid date (YYYY-MM-DD)")
    return text


def _coordinate(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")
    if math.isnan(number) or math.isinf(number):
        raise ValidationError(field + " must be a finite number")
    return number


def _prepare_observation_item(raw, seen):
    """Validate one offline observation and normalise its identity/content.

    Identity is (event_id, observed_at). Content is the set of fields the
    patrol device owns; anything the duty officer may have corrected is part
    of content, never silently overwritten.
    """
    if not isinstance(raw, dict):
        raise ValidationError("each observation must be an object")
    item = dict(raw)
    RuleEngine._require(item, RuleEngine.CREATE_REQUIRED["observation"])
    event_id = str(item["event_id"]).strip()
    observed_at = _normalize_observed_at(item["observed_at"])
    if not event_id:
        raise ValidationError("event_id must not be blank")
    for field in ("species", "location"):
        if not isinstance(item.get(field), str):
            raise ValidationError(field + " must be a string")
    content = {
        "species": item["species"],
        "location": item["location"],
        "lat": _coordinate(item["lat"], "lat"),
        "lon": _coordinate(item["lon"], "lon"),
    }
    lat, lon = content["lat"], content["lon"]
    if not -90 <= lat <= 90:
        raise ValidationError("lat must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise ValidationError("lon must be between -180 and 180")
    identity = (event_id, observed_at)
    if identity in seen:
        raise ValidationError(
            "duplicate observation inside batch: event %s at %s" % identity
        )
    seen.add(identity)
    payload = dict(item)
    payload["event_id"] = event_id
    payload["observed_at"] = observed_at
    payload.update(content)
    return {"identity": identity, "content": content, "payload": payload}


def validate_observation_batch_envelope(actor, body):
    """Validate the batch envelope; never touches storage."""
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    RuleEngine._ensure_role(actor, RuleEngine.CREATE_ROLES["observation"])
    device_id = body.get("device_id")
    batch_no = body.get("batch_no")
    if not device_id or not isinstance(device_id, str):
        raise ValidationError("device_id is required")
    if not batch_no or not isinstance(batch_no, str):
        raise ValidationError("batch_no is required")
    observations = body.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValidationError("observations must be a non-empty list")
    return device_id, batch_no, observations


def prepare_observations(observations):
    """Validate and normalise every batch item; returns identity-keyed items."""
    seen = set()
    return [_prepare_observation_item(item, seen) for item in observations]


def plan_observation_batch(prepared, existing_by_identity):
    """Classify every item as create/skip/conflict against server state.

    Same identity (event_id + observed_at) means the same observation.
    Identical content skips; differing content is a whole-batch conflict.
    """
    creates, skips, conflicts = [], [], []
    for item in prepared:
        event_id, observed_at = item["identity"]
        current = existing_by_identity.get(item["identity"])
        if current is None:
            creates.append(item)
            continue
        server_content = {
            field: (
                float(current["data"].get(field))
                if field in ("lat", "lon")
                else current["data"].get(field)
            )
            for field in OBSERVATION_CONTENT_FIELDS
        }
        record = {
            "event_id": event_id,
            "observed_at": observed_at,
            "incoming": item["content"],
            "server_entity_id": current["id"],
            "server_version": current["version"],
            "server_status": current["status"],
            "server": server_content,
        }
        if server_content == item["content"]:
            skips.append(
                {
                    "event_id": event_id,
                    "observed_at": observed_at,
                    "entity_id": current["id"],
                    "version": current["version"],
                    "status": current["status"],
                }
            )
        else:
            conflicts.append(record)
    return {"creates": creates, "skips": skips, "conflicts": conflicts}


def _validate_observation(actor, data, lookup):
    rows = lookup("observation", "event_id", data.get("event_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("observed_at") == data.get("observed_at"):
            raise ConflictError("duplicate observation event")
    if not data.get("species"):
        raise ValidationError("species is required")


def _validate_sample(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] not in ("submitted", "sampled"):
        raise ValidationError("sample requires a submitted observation")


def _validate_lab_result(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "negative"):
        raise ValidationError("lab result must be positive or negative")


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def is_cluster(observations, max_days=14, radius_km=10):
    if len(observations) < 3:
        return False
    points = observations[:3]
    same_window = all(
        abs(_date_ordinal(points[0].get("observed_at")) - _date_ordinal(item.get("observed_at"))) <= max_days
        for item in points[1:]
    )
    close = all(
        _haversine_km(points[0]["lat"], points[0]["lon"], item["lat"], item["lon"]) <= radius_km
        for item in points[1:]
    )
    return same_window and close


CUSTOM_CREATE = {'observation': _validate_observation, 'sample': _validate_sample}
CUSTOM_TRANSITIONS = {('sample', 'lab_result'): _validate_lab_result}


class RuleEngine:
    ALIASES = {'observations': 'observation', 'samples': 'sample', 'clusters': 'cluster'}
    INITIAL_STATUS = {'observation': 'captured', 'sample': 'collected', 'cluster': 'draft'}
    TRANSITIONS = {'observation': {'submit': (('captured',), 'submitted'), 'reject': (('submitted',), 'rejected'), 'link_sample': (('submitted',), 'sampled')}, 'sample': {'send_lab': (('collected',), 'in_lab'), 'lab_result': (('in_lab',), 'resulted'), 'retest': (('resulted',), 'in_lab'), 'close': (('resulted',), 'closed')}, 'cluster': {'confirm_cluster': (('draft',), 'confirmed'), 'dismiss': (('draft',), 'dismissed')}}
    CREATE_REQUIRED = {'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'), 'sample': ('observation_id', 'sample_code'), 'cluster': ('region',)}
    ACTION_REQUIRED = {('observation', 'submit'): ('location', 'observed_at'), ('observation', 'reject'): ('reason',), ('observation', 'link_sample'): ('sample_id',), ('sample', 'send_lab'): ('lab_id',), ('sample', 'lab_result'): ('result', 'result_at'), ('sample', 'retest'): ('reason',), ('sample', 'close'): ('outcome',), ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'), ('cluster', 'dismiss'): ('reason',)}
    CREATE_ROLES = {'observation': ('admin', 'field'), 'sample': ('admin', 'field'), 'cluster': ('admin', 'epidemiologist')}
    ROLE_ACTIONS = {'submit': ('admin', 'field'), 'reject': ('admin', 'epidemiologist'), 'link_sample': ('admin', 'field'), 'send_lab': ('admin', 'field'), 'lab_result': ('admin', 'lab'), 'retest': ('admin', 'lab'), 'close': ('admin', 'epidemiologist'), 'confirm_cluster': ('admin', 'epidemiologist'), 'dismiss': ('admin', 'epidemiologist')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
