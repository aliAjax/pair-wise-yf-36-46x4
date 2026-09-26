from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_participant(actor, data, lookup):
    if len(data.get("name", "")) < 2:
        raise ValidationError("participant name is required")


def _validate_consent(actor, data, lookup):
    participant = _find_one(lookup, "participant", "id", data.get("participant_id"))
    if not participant or participant["status"] == "closed":
        raise ValidationError("consent requires an active participant")
    if not data.get("scope"):
        raise ValidationError("consent scope is required")


def _validate_sample_store(actor, entity, data, lookup):
    consent = _find_one(lookup, "consent", "id", data.get("consent_id"))
    if not consent or consent["status"] != "active":
        raise ValidationError("storage requires active consent")
    if "research" not in consent["data"].get("scope", []):
        raise ValidationError("consent does not include research use")
    return {"stored_at": "2026-09-24T00:00:00Z"}


def _return_target(entity):
    """Returning a normal loan puts the sample back; a pending-return sample is destroyed."""
    if entity["status"] == "return_pending":
        return "destroyed"
    return "stored"


def _validate_sample_return(actor, entity, data, lookup):
    if _return_target(entity) == "destroyed":
        return {"destroyed_after_return": True}
    return {}


def _validate_withdrawal_approve(actor, entity, data, lookup):
    sample_ids = data.get("sample_ids") or []
    if len(set(sample_ids)) != len(sample_ids):
        raise ConflictError("sample_ids contains duplicates")
    participant_id = entity["data"].get("participant_id")
    for sample_id in sample_ids:
        sample = _find_one(lookup, "sample", "id", sample_id)
        if not sample:
            raise ValidationError("unknown sample: " + str(sample_id))
        if sample["data"].get("participant_id") != participant_id:
            raise ValidationError(
                "sample %s belongs to another participant" % sample_id
            )
    return {"approved_by": actor.user_id}


CUSTOM_CREATE = {'participant': _validate_participant, 'consent': _validate_consent}
CUSTOM_TRANSITIONS = {
    ('sample', 'store'): _validate_sample_store,
    ('sample', 'return'): _validate_sample_return,
    ('withdrawal', 'approve'): _validate_withdrawal_approve,
}


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'samples': 'sample', 'withdrawals': 'withdrawal'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'sample': 'collected', 'withdrawal': 'requested'}
    TRANSITIONS = {
        'participant': {'close_participant': (('registered',), 'closed')},
        'consent': {'activate': (('draft',), 'active'), 'supersede': (('active',), 'superseded'), 'withdraw': (('active',), 'withdrawn')},
        'sample': {
            'store': (('collected',), 'stored'),
            'loan': (('stored',), 'on_loan'),
            'return': (('on_loan', 'return_pending'), 'stored'),
            'mark_pending_return': (('on_loan',), 'return_pending'),
            'anonymize': (('stored',), 'anonymized'),
            'destroy': (('stored',), 'destroyed'),
        },
        'withdrawal': {'approve': (('requested',), 'approved'), 'execute': (('approved',), 'executed')},
    }
    CREATE_REQUIRED = {'participant': ('name',), 'consent': ('participant_id', 'scope'), 'sample': ('participant_id', 'sample_code', 'collected_at'), 'withdrawal': ('participant_id', 'requested_at')}
    ACTION_REQUIRED = {('consent', 'activate'): ('scope', 'version', 'expires_at'), ('consent', 'supersede'): ('reason',), ('consent', 'withdraw'): ('reason',), ('sample', 'store'): ('freezer', 'position', 'consent_id'), ('sample', 'loan'): ('recipient', 'purpose', 'due_at'), ('sample', 'anonymize'): ('reason',), ('sample', 'destroy'): ('reason',), ('withdrawal', 'approve'): ('reason', 'sample_ids'), ('withdrawal', 'execute'): ('executed_at',)}
    CREATE_ROLES = {'participant': ('admin', 'biobank'), 'consent': ('admin', 'committee'), 'sample': ('admin', 'biobank'), 'withdrawal': ('admin', 'biobank')}
    ROLE_ACTIONS = {'close_participant': ('admin', 'biobank'), 'activate': ('admin', 'committee'), 'supersede': ('admin', 'committee'), 'withdraw': ('admin', 'committee'), 'store': ('admin', 'biobank'), 'loan': ('admin', 'biobank'), 'return': ('admin', 'biobank'), 'mark_pending_return': ('admin', 'biobank'), 'anonymize': ('admin', 'biobank'), 'destroy': ('admin', 'biobank'), 'approve': ('admin', 'committee'), 'execute': ('admin', 'biobank')}

    # Effects applied when an approved withdrawal is executed.
    SAMPLE_EXECUTE = {'stored': 'destroyed', 'on_loan': 'return_pending'}
    SAMPLE_EXECUTE_ACTION = {'stored': 'destroy', 'on_loan': 'mark_pending_return'}
    SAMPLE_ACTIONABLE = ('stored', 'on_loan')

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

    def next_status(self, kind, action, current_status):
        kind = self.normalize_kind(kind)
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, default_next = transition
        if current_status not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, current_status)
            )
        if kind == "sample" and action == "return":
            return _return_target({"status": current_status})
        return default_next

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        next_status = self.next_status(kind, action, entity["status"])
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

    def ensure_action_allowed(self, actor, action):
        allowed_roles = self.ROLE_ACTIONS.get(action, ("admin",))
        self._ensure_role(actor, allowed_roles)

    def build_withdrawal_plan(self, actor, withdrawal, data, consents, samples, expected=None):
        """Compute the impact scope of executing a withdrawal and detect conflicts.

        consents/samples are the current entities for the same participant. Returns a
        plan describing every consents to withdraw and samples to destroy or hold for
        return. Raises ConflictError with per-entity details when the versions or
        states seen before execution have drifted.
        """
        self.ensure_action_allowed(actor, "execute")
        executed_at = (data or {}).get("executed_at")
        if not executed_at:
            raise ValidationError("missing required field: executed_at")
        expected = expected or {}
        conflicts = []

        if withdrawal["status"] != "approved":
            raise InvalidTransition(
                "cannot execute withdrawal from status %s" % withdrawal["status"]
            )
        withdrawal_version = expected.get("withdrawal_version")
        if withdrawal_version is not None and int(withdrawal_version) != withdrawal["version"]:
            conflicts.append(self._conflict(
                "withdrawal", withdrawal["id"],
                "withdrawal version changed: expected %s, found %s"
                % (withdrawal_version, withdrawal["version"]),
                current_version=withdrawal["version"], current_status=withdrawal["status"],
            ))

        participant_id = withdrawal["data"].get("participant_id")
        sample_ids = list(withdrawal["data"].get("sample_ids", []))
        samples_by_id = {sample["id"]: sample for sample in samples}

        planned_samples = []
        for sample_id in sample_ids:
            sample = samples_by_id.get(sample_id)
            if not sample:
                conflicts.append(self._conflict(
                    "sample", sample_id, "approved sample no longer exists",
                    current_version=None, current_status=None,
                ))
                continue
            if sample["data"].get("participant_id") != participant_id:
                conflicts.append(self._conflict(
                    "sample", sample_id, "sample belongs to another participant",
                    current_version=sample["version"], current_status=sample["status"],
                ))
                continue
            effect = self.SAMPLE_EXECUTE.get(sample["status"])
            if not effect:
                conflicts.append(self._conflict(
                    "sample", sample_id,
                    "sample status changed to %s, cannot process during withdrawal"
                    % sample["status"],
                    current_version=sample["version"], current_status=sample["status"],
                ))
                continue
            planned_samples.append(sample)

        planned_consents = [
            consent
            for consent in consents
            if consent["data"].get("participant_id") == participant_id
            and consent["status"] == "active"
        ]

        if "consent_versions" in expected:
            conflicts.extend(self._check_expected_versions(
                "consent", expected.get("consent_versions") or {},
                planned_consents,
                lambda consent: consent["status"] == "active",
                {consent["id"]: consent for consent in consents},
            ))
        if "sample_versions" in expected:
            conflicts.extend(self._check_expected_versions(
                "sample", expected.get("sample_versions") or {},
                planned_samples,
                lambda sample: sample["status"] in self.SAMPLE_ACTIONABLE,
                samples_by_id,
            ))

        if conflicts:
            # One record per entity: a state change also bumps the version, so keep
            # the structural conflict message and merge the version numbers.
            merged = {}
            order = []
            for item in conflicts:
                key = (item["kind"], item["id"])
                if key not in merged:
                    merged[key] = dict(item)
                    order.append(key)
                    continue
                first = merged[key]
                expected_match = "version changed: expected" in item["message"]
                if expected_match:
                    first["message"] += "; " + item["message"]
                else:
                    first["message"] = item["message"] + "; " + first["message"]
            raise ConflictError(
                "withdrawal execution conflicts with current state",
                [merged[key] for key in order],
            )

        sample_items = [
            {
                "id": sample["id"],
                "sample_code": sample["data"].get("sample_code"),
                "from_status": sample["status"],
                "to_status": self.SAMPLE_EXECUTE[sample["status"]],
                "effect": "return_pending" if sample["status"] == "on_loan" else "destroy",
                "version": sample["version"],
            }
            for sample in planned_samples
        ]
        consent_items = [
            {
                "id": consent["id"],
                "scope": consent["data"].get("scope"),
                "consent_version": consent["data"].get("version"),
                "from_status": consent["status"],
                "to_status": "withdrawn",
                "effect": "withdraw",
                "version": consent["version"],
            }
            for consent in planned_consents
        ]
        expected_versions = {
            "withdrawal_version": withdrawal["version"],
            "consent_versions": {consent["id"]: consent["version"] for consent in planned_consents},
            "sample_versions": {sample["id"]: sample["version"] for sample in planned_samples},
        }
        return {
            "withdrawal_id": withdrawal["id"],
            "participant_id": participant_id,
            "executed_at": executed_at,
            "consents": consent_items,
            "samples": sample_items,
            "expected_versions": expected_versions,
        }

    @staticmethod
    def _conflict(kind, entity_id, message, current_version=None, current_status=None):
        item = {
            "kind": kind,
            "id": entity_id,
            "message": message,
        }
        if current_version is not None:
            item["current_version"] = current_version
        if current_status is not None:
            item["current_status"] = current_status
        return item

    @staticmethod
    def _check_expected_versions(kind, expected_map, planned, is_actionable, by_id):
        conflicts = []
        for entity_id, raw_version in (expected_map or {}).items():
            expected_version = int(raw_version)
            entity = by_id.get(entity_id)
            if not entity:
                conflicts.append(RuleEngine._conflict(
                    kind, entity_id,
                    "%s in expected scope no longer exists" % kind,
                ))
                continue
            if entity["version"] != expected_version:
                conflicts.append(RuleEngine._conflict(
                    kind, entity_id,
                    "%s version changed: expected %s, found %s"
                    % (kind, expected_version, entity["version"]),
                    current_version=entity["version"], current_status=entity["status"],
                ))
                continue
            if not is_actionable(entity):
                conflicts.append(RuleEngine._conflict(
                    kind, entity_id,
                    "%s status changed to %s before execution"
                    % (kind, entity["status"]),
                    current_version=entity["version"], current_status=entity["status"],
                ))
        for entity in planned:
            if entity["id"] not in expected_map:
                conflicts.append(RuleEngine._conflict(
                    kind, entity["id"],
                    "new %s %s in scope was not part of the planned impact"
                    % (kind, entity["status"]),
                    current_version=entity["version"], current_status=entity["status"],
                ))
        return conflicts


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
