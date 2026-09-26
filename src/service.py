from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine


CASCADE_ACTIONS = {
    ("consent", "withdrawn"): "withdraw",
    ("sample", "destroyed"): "destroy",
    ("sample", "pending_return"): "mark_pending_return",
}


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
        if entity["kind"] == "withdrawal" and action == "execute":
            return self.execute_withdrawal(actor, entity, dict(data or {}), expected_version)
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

    def execute_withdrawal(self, actor, entity, data, expected_version=None):
        if entity["status"] == "executed":
            stored = (entity["data"].get("execution") or {}).get("request")
            if stored == data:
                return entity
            raise ConflictError("withdrawal already executed with a different request")
        next_status, patch = self.rules.validate_transition(
            actor, entity, "execute", dict(data), self._lookup
        )
        assessment = self.rules.plan_withdrawal_execution(entity, patch, self._lookup)
        plan = assessment["plan"]
        report = {
            "participant_id": assessment["participant_id"],
            "affected": [
                {
                    "id": target["id"],
                    "kind": target["kind"],
                    "from_status": target["status"],
                    "to_status": to_status,
                }
                for target, to_status, _ in plan
            ],
            "skipped": [
                {"id": item["id"], "kind": item["kind"], "status": item["status"]}
                for item in assessment["samples"]
                if item["planned_action"] == "none"
            ],
        }
        merged = dict(entity["data"])
        merged.update(patch)
        merged["execution"] = {
            "request": dict(data),
            "executed_by": actor.user_id,
            "report": report,
        }
        expected = (
            int(expected_version) if expected_version is not None else entity["version"]
        )
        updates = [
            {
                "id": entity["id"],
                "expected_version": expected,
                "status": next_status,
                "data": merged,
            }
        ]
        audits = [
            self._audit_entry(entity, actor, "execute", next_status,
                              {"patch": patch, "report": report})
        ]
        for target, to_status, target_patch in plan:
            merged_target = dict(target["data"])
            merged_target.update(target_patch)
            updates.append(
                {
                    "id": target["id"],
                    "expected_version": target["version"],
                    "status": to_status,
                    "data": merged_target,
                }
            )
            audits.append(
                self._audit_entry(
                    target,
                    actor,
                    CASCADE_ACTIONS.get((target["kind"], to_status), "cascade"),
                    to_status,
                    {"withdrawal_id": entity["id"], "patch": target_patch},
                )
            )
        try:
            self.repository.apply_transaction(updates, audits)
        except ConflictError:
            replay = self.repository.get_entity(entity["id"])
            if (
                replay
                and replay["status"] == "executed"
                and (replay["data"].get("execution") or {}).get("request") == data
            ):
                return replay
            raise
        return self.repository.get_entity(entity["id"])

    def get_withdrawal_impact(self, withdrawal_id):
        entity = self.repository.get_entity(withdrawal_id)
        if not entity or entity["kind"] != "withdrawal":
            raise NotFoundError("withdrawal not found: " + withdrawal_id)
        if entity["status"] == "executed":
            execution = entity["data"].get("execution") or {}
            return {
                "withdrawal_id": entity["id"],
                "status": entity["status"],
                "phase": "executed",
                "participant_id": entity["data"].get("participant_id"),
                "report": execution.get("report", {}),
            }
        if entity["status"] != "approved":
            raise InvalidTransition(
                "cannot preview impact from status " + entity["status"]
            )
        assessment = self.rules.assess_withdrawal_execution(entity, self._lookup)
        return {
            "withdrawal_id": entity["id"],
            "status": entity["status"],
            "phase": "preview",
            "participant_id": assessment["participant_id"],
            "consents": assessment["consents"],
            "samples": assessment["samples"],
            "conflicts": assessment["conflicts"],
        }

    @staticmethod
    def _audit_entry(entity, actor, action, to_status, detail):
        return {
            "entity_id": entity["id"],
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": action,
            "from_status": entity["status"],
            "to_status": to_status,
            "detail": detail,
        }

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
