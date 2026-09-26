from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
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

    def transition(self, actor, entity_id, action, data=None, expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "withdrawal" and action in ("plan_execute", "execute"):
            payload = dict(data or {})
            expected = self._expected_block(payload, expected_version)
            if action == "plan_execute":
                return self.plan_withdrawal(actor, entity_id, payload, expected)
            return self.execute_withdrawal(actor, entity_id, payload, expected, idempotency_key)
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

    @staticmethod
    def _expected_block(data, expected_version):
        expected = {}
        nested = data.pop("expected_versions", None) if data else None
        if nested is not None:
            if not isinstance(nested, dict):
                raise ValidationError("expected_versions must be an object")
            expected.update(nested)
        if expected_version is not None:
            expected["withdrawal_version"] = int(expected_version)
        return expected

    def plan_withdrawal(self, actor, withdrawal_id, data, expected=None):
        withdrawal = self._require_withdrawal(withdrawal_id)
        plan = self._build_plan(actor, withdrawal, data, expected)
        return {
            "stage": "impact",
            "withdrawal": self._public_view(withdrawal),
            **{key: plan[key] for key in ("consents", "samples", "expected_versions")},
        }

    def execute_withdrawal(self, actor, withdrawal_id, data, expected=None, idempotency_key=None):
        if idempotency_key:
            stored = self.repository.get_execution_result(idempotency_key)
            if stored:
                if stored["entity_id"] != withdrawal_id:
                    raise ConflictError(
                        "idempotency key is bound to another withdrawal: " + stored["entity_id"]
                    )
                return stored["result"]

        withdrawal = self._require_withdrawal(withdrawal_id)
        plan = self._build_plan(actor, withdrawal, data, expected)
        return self._run_withdrawal(actor, withdrawal, plan, data, idempotency_key)

    def _require_withdrawal(self, withdrawal_id):
        withdrawal = self.repository.get_entity(withdrawal_id)
        if not withdrawal:
            raise NotFoundError("entity not found: " + withdrawal_id)
        if self.rules.normalize_kind(withdrawal["kind"]) != "withdrawal":
            raise InvalidTransition("entity %s is not a withdrawal" % withdrawal_id)
        return withdrawal

    def _build_plan(self, actor, withdrawal, data, expected):
        participant_id = withdrawal["data"].get("participant_id")
        consents = [
            consent
            for consent in self.repository.list_entities(kind="consent")
            if consent["data"].get("participant_id") == participant_id
        ]
        sample_ids = list(withdrawal["data"].get("sample_ids", []))
        samples = self._load_samples(sample_ids)
        return self.rules.build_withdrawal_plan(
            actor, withdrawal, data, consents, samples, expected
        )

    def _load_samples(self, sample_ids):
        samples = []
        for sample_id in sample_ids:
            sample = self.repository.get_entity(sample_id)
            if sample:
                samples.append(sample)
        return samples

    def _run_withdrawal(self, actor, withdrawal, plan, data, idempotency_key):
        reason = withdrawal["data"].get("reason")
        consents_by_id = {
            consent["id"]: consent
            for consent in self.repository.list_entities(kind="consent")
        }
        samples_by_id = {
            sample["id"]: sample
            for sample in self._load_samples(plan["expected_versions"]["sample_versions"].keys())
        }

        result_consents = []
        result_samples = []
        with self.repository.unit_of_work() as uow:
            for item in plan["consents"]:
                consent = consents_by_id[item["id"]]
                merged = dict(consent["data"])
                merged.update({
                    "withdrawn_reason": reason,
                    "withdrawn_withdrawal_id": withdrawal["id"],
                    "withdrawn_at": plan["executed_at"],
                })
                updated = uow.update(consent, consent["version"], "withdrawn", merged)
                uow.audit(
                    consent["id"], actor.user_id, actor.role, "withdraw",
                    consent["status"], "withdrawn",
                    {"withdrawal_id": withdrawal["id"], "reason": reason},
                )
                result_consents.append(self._public_view(updated))

            for item in plan["samples"]:
                sample = samples_by_id[item["id"]]
                to_status = item["to_status"]
                merged = dict(sample["data"])
                if to_status == "return_pending":
                    merged["pending_return_for_withdrawal"] = withdrawal["id"]
                else:
                    merged["destroyed_reason"] = reason
                    merged["destroyed_withdrawal_id"] = withdrawal["id"]
                    merged["destroyed_at"] = plan["executed_at"]
                action = self.rules.SAMPLE_EXECUTE_ACTION[sample["status"]]
                updated = uow.update(sample, sample["version"], to_status, merged)
                uow.audit(
                    sample["id"], actor.user_id, actor.role, action,
                    sample["status"], to_status,
                    {"withdrawal_id": withdrawal["id"], "reason": reason},
                )
                result_samples.append(self._public_view(updated))

            withdrawal_merged = dict(withdrawal["data"])
            withdrawal_merged.update({
                "executed_by": actor.user_id,
                "executed_at": plan["executed_at"],
                "consent_ids": [item["id"] for item in plan["consents"]],
                "destroyed_sample_ids": [
                    item["id"] for item in plan["samples"] if item["effect"] == "destroy"
                ],
                "pending_return_sample_ids": [
                    item["id"] for item in plan["samples"] if item["effect"] == "return_pending"
                ],
            })
            updated_withdrawal = uow.update(
                withdrawal, withdrawal["version"], "executed", withdrawal_merged
            )
            uow.audit(
                withdrawal["id"], actor.user_id, actor.role, "execute",
                "approved", "executed",
                {
                    "executed_at": plan["executed_at"],
                    "consent_ids": withdrawal_merged["consent_ids"],
                    "destroyed_sample_ids": withdrawal_merged["destroyed_sample_ids"],
                    "pending_return_sample_ids": withdrawal_merged["pending_return_sample_ids"],
                },
            )

            result = {
                "stage": "executed",
                "withdrawal": self._public_view(updated_withdrawal),
                "consents": result_consents,
                "samples": result_samples,
                "summary": {
                    "consents_withdrawn": len(result_consents),
                    "samples_destroyed": len(withdrawal_merged["destroyed_sample_ids"]),
                    "samples_pending_return": len(withdrawal_merged["pending_return_sample_ids"]),
                },
            }
            if idempotency_key:
                uow.save_execution_result(idempotency_key, withdrawal["id"], result)

        return result

    @staticmethod
    def _public_view(entity):
        return {
            "id": entity["id"],
            "kind": entity["kind"],
            "status": entity["status"],
            "version": entity["version"],
            "data": entity["data"],
            "updated_at": entity["updated_at"],
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
