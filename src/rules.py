from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 离线批次回传：观察的自然键与参与内容比对的字段
BATCH_OBSERVATION_FIELDS = (
    "event_id",
    "species",
    "location",
    "observed_at",
    "lat",
    "lon",
)


def observation_natural_key(data):
    """同一条观察的判定：事件号相同且观察日期相同（忽略时间部分）。"""
    return (str(data.get("event_id")), str(data.get("observed_at"))[:10])


def observation_fingerprint(data):
    """同一自然键下“内容是否相同”的判定，只比较观察本身的业务字段。

    补正过的物种或坐标会改变指纹，从而被识别为与服务器版本冲突，
    避免覆盖值班员已核对的内容。
    """
    normalized = {}
    for field in BATCH_OBSERVATION_FIELDS:
        value = data.get(field)
        if field in ("lat", "lon") and value is not None:
            value = float(value)
        if field == "observed_at" and value is not None:
            value = str(value)[:10]
        normalized[field] = value
    return normalized


def _same_fingerprint(incoming, server_data):
    candidate = observation_fingerprint(incoming)
    stored = observation_fingerprint(server_data)
    if candidate != stored:
        return False
    # 服务器上值班员补录的额外字段也视为内容差异，防止离线包静默覆盖核对结果
    for field in server_data:
        if field not in candidate and server_data[field] not in (None, ""):
            return False
    return True


def _is_iso_date(value):
    try:
        date.fromisoformat(str(value)[:10])
        return True
    except ValueError:
        return False


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

    def validate_batch(self, actor, batch):
        """校验离线批次请求的形状、角色和逐条观察字段，返回规整后的批次。

        这一层只做输入校验（不查库）；批内去重和与服务器版本的比对在
        plan_batch 中完成。
        """
        self._ensure_role(actor, ("admin", "field"))
        if not isinstance(batch, dict):
            raise ValidationError("batch must be a JSON object")
        device_id = batch.get("device_id")
        batch_no = batch.get("batch_no")
        if not device_id:
            raise ValidationError("missing required field: device_id")
        if batch_no in (None, ""):
            raise ValidationError("missing required field: batch_no")
        observations = batch.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ValidationError("observations must be a non-empty list")
        normalized = []
        for index, item in enumerate(observations):
            if not isinstance(item, dict):
                raise ValidationError("observation #%s must be a JSON object" % index)
            data = {field: item.get(field) for field in BATCH_OBSERVATION_FIELDS if field in item}
            self._require(data, BATCH_OBSERVATION_FIELDS)
            if not _is_iso_date(data["observed_at"]):
                raise ValidationError(
                    "observation #%s observed_at must be a date (YYYY-MM-DD)" % index
                )
            for coordinate in ("lat", "lon"):
                try:
                    data[coordinate] = float(data[coordinate])
                except (TypeError, ValueError):
                    raise ValidationError(
                        "observation #%s %s must be a number" % (index, coordinate)
                    )
            data["event_id"] = str(data["event_id"])
            data["species"] = str(data["species"])
            data["location"] = str(data["location"])
            data["observed_at"] = str(data["observed_at"])[:10]
            normalized.append(data)
        return {
            "device_id": str(device_id),
            "batch_no": str(batch_no),
            "observations": normalized,
        }

    def plan_batch(self, observations, server_lookup):
        """在批内去重的基础上，把每条观察分类为 create / skip / conflict。

        自然键（event_id + 观察日期）相同即同一条观察：
        - 批内重复且内容相同：折叠为一条；内容不同：校验失败，整批不可受理；
        - 与服务器已有观察内容相同：跳过；内容不同：记为冲突，附带服务器版本。
        任何冲突都要求整批退回，不能只拦冲突那一条。
        返回 {"creates": [...], "skips": [...], "conflicts": [...]}。
        """
        grouped = {}
        for index, data in enumerate(observations):
            key = observation_natural_key(data)
            if key in grouped:
                previous_index, previous = grouped[key]
                if observation_fingerprint(previous) != observation_fingerprint(data):
                    raise ValidationError(
                        "observations #%s and #%s share event_id/observed_at "
                        "but have different content" % (previous_index, index)
                    )
                # 批内完全重复，折叠
                continue
            grouped[key] = (index, data)

        creates, skips, conflicts = [], [], []
        for index, data in grouped.values():
            rows = server_lookup("observation", "event_id", data["event_id"]) or []
            server_row = None
            for row in rows:
                if str(row["data"].get("observed_at", ""))[:10] == data["observed_at"]:
                    server_row = row
                    break
            if server_row is None:
                creates.append({"index": index, "data": data})
                continue
            server_data = server_row["data"]
            if _same_fingerprint(data, server_data):
                skips.append(
                    {
                        "index": index,
                        "event_id": data["event_id"],
                        "observed_at": data["observed_at"],
                        "reason": "identical",
                        "entity_id": server_row["id"],
                    }
                )
            else:
                conflicts.append(
                    {
                        "index": index,
                        "event_id": data["event_id"],
                        "observed_at": data["observed_at"],
                        "incoming": data,
                        "server_version": server_row["version"],
                        "server_entity_id": server_row["id"],
                        "server": {
                            field: server_data.get(field)
                            for field in BATCH_OBSERVATION_FIELDS
                        },
                    }
                )
        return {"creates": creates, "skips": skips, "conflicts": conflicts}

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
