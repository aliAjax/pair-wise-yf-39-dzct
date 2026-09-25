from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import (
    RuleEngine,
    plan_observation_batch,
    prepare_observations,
    validate_observation_batch_envelope,
)


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

    def upload_observation_batch(self, actor, body):
        """Offline batch upload.

        Returns (http_status, response). The same device_id + batch_no always
        replays the first answer; conflicts reject the WHOLE batch, never just
        the clashing row.
        """
        device_id, batch_no, observations = validate_observation_batch_envelope(
            actor, body
        )
        repository = self.repository
        with repository.batch_transaction() as tx:
            replay = tx.get_batch(device_id, batch_no)
            if replay is not None:
                return (
                    200 if replay["status"] == "accepted" else 409,
                    replay["response"],
                )

            # Validate and normalise every item before any write happens.
            prepared = prepare_observations(observations)
            identities = {item["identity"] for item in prepared}
            plan = plan_observation_batch(
                prepared, tx.observations_by_identity(identities)
            )

            if plan["conflicts"]:
                response = {
                    "device_id": device_id,
                    "batch_no": batch_no,
                    "result": "rejected",
                    "reason": "conflict",
                    "conflicts": plan["conflicts"],
                    "summary": {
                        "received": len(observations),
                        "created": 0,
                        "skipped": len(plan["skips"]),
                        "conflicts": len(plan["conflicts"]),
                    },
                }
                # Persist the rejection so a retried batch replays this answer.
                tx.save_batch_result(
                    device_id, batch_no, "rejected", response, actor.user_id
                )
                return 409, response

            created = []
            for item in plan["creates"]:
                payload = dict(item["payload"])
                entity_id = payload.get("id")
                if entity_id and tx.get_entity(str(entity_id)):
                    raise ConflictError("entity already exists: " + str(entity_id))
                created.append(tx.create_observation(payload, actor.user_id))

            response = {
                "device_id": device_id,
                "batch_no": batch_no,
                "result": "accepted",
                "created": [
                    {
                        "event_id": entity["data"]["event_id"],
                        "observed_at": entity["data"]["observed_at"],
                        "entity_id": entity["id"],
                        "version": entity["version"],
                        "status": entity["status"],
                    }
                    for entity in created
                ],
                "skipped": plan["skips"],
                "summary": {
                    "received": len(observations),
                    "created": len(created),
                    "skipped": len(plan["skips"]),
                    "conflicts": 0,
                },
            }
            tx.save_batch_result(
                device_id, batch_no, "accepted", response, actor.user_id
            )
            return 200, response

    def get_observation_batch(self, device_id, batch_no):
        record = self.repository.get_observation_batch(device_id, batch_no)
        if not record:
            raise NotFoundError(
                "batch not found: %s/%s" % (device_id, batch_no)
            )
        return record
