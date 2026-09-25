from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def submit_batch(self, actor, batch):
        """离线观察批次整批回传。

        - 同一设备批次号再次到达：沿用第一次的应答（含首次退回的应答）；
        - 自然键相同且内容相同的观察跳过；
        - 自然键相同但内容不同：整批退回，列出冲突项和服务器版本；
        - 整个判定与写入在一个立即事务内完成，不会只落库无冲突的那几条。
        返回 (http_status, response_body)。
        """
        normalized = self.rules.validate_batch(actor, batch)
        device_id = normalized["device_id"]
        batch_no = normalized["batch_no"]

        # 快速路径：多数重传在此直接返回首次应答
        receipt = self.repository.get_batch_receipt(device_id, batch_no)
        if receipt:
            return receipt["status_code"], receipt["response"]

        connection = self.repository.begin_transaction()
        try:
            # 拿到写锁后再复查：并发的同批次请求可能已在此期间提交了首次应答
            locked_receipt = self.repository.tx_get_batch_receipt(
                connection, device_id, batch_no
            )
            if locked_receipt:
                self.repository.rollback_transaction(connection)
                return locked_receipt["status_code"], locked_receipt["response"]

            def lookup(kind, field, value):
                return self.repository.tx_lookup(connection, kind, field, value)

            plan = self.rules.plan_batch(normalized["observations"], lookup)
            if plan["conflicts"]:
                response = {
                    "status": "rejected",
                    "device_id": device_id,
                    "batch_no": batch_no,
                    "reason": "conflict",
                    "message": "整批退回：存在与服务器已核对内容不一致的观察",
                    "conflicts": plan["conflicts"],
                    "accepted": [],
                    "skipped": [],
                }
                self.repository.tx_save_batch_receipt(
                    connection, device_id, batch_no, 409, response, actor.user_id
                )
                self.repository.commit_transaction(connection)
                return 409, response

            accepted = []
            for item in plan["creates"]:
                entity_id = str(uuid4())
                self.repository.tx_insert_observation(
                    connection, entity_id, item["data"], actor.user_id
                )
                self.repository.tx_append_audit(
                    connection,
                    entity_id,
                    actor.user_id,
                    actor.role,
                    {"kind": "observation", "batch_no": batch_no, "device_id": device_id},
                )
                accepted.append(
                    {
                        "index": item["index"],
                        "entity_id": entity_id,
                        "event_id": item["data"]["event_id"],
                        "observed_at": item["data"]["observed_at"],
                    }
                )
            response = {
                "status": "accepted",
                "device_id": device_id,
                "batch_no": batch_no,
                "accepted": accepted,
                "skipped": plan["skips"],
            }
            self.repository.tx_save_batch_receipt(
                connection, device_id, batch_no, 200, response, actor.user_id
            )
            self.repository.commit_transaction(connection)
            return 200, response
        except Exception:
            self.repository.rollback_transaction(connection)
            raise

    def get_batch_result(self, device_id, batch_no):
        receipt = self.repository.get_batch_receipt(device_id, batch_no)
        if not receipt:
            raise NotFoundError(
                "batch not found: %s/%s" % (device_id, batch_no)
            )
        return receipt["status_code"], receipt["response"]
