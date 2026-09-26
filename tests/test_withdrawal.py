import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WithdrawalExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.committee = Actor("ethics", "committee")
        self.biobank = Actor("keeper", "biobank")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_participant(self, name="Participant"):
        participant = self.service.create(
            self.actor, "participant", {"name": name}
        )
        return participant["id"]

    def _activate_consent(self, participant_id, version):
        consent = self.service.create(
            self.committee, "consent",
            {"participant_id": participant_id, "scope": ["research"]},
        )
        return self.service.transition(
            self.committee, consent["id"], "activate",
            {"scope": ["research"], "version": version, "expires_at": "2099-01-01"},
        )

    def _stored_sample(self, participant_id, consent_id, code):
        sample = self.service.create(
            self.biobank, "sample",
            {"participant_id": participant_id, "sample_code": code,
             "collected_at": "2026-01-01"},
        )
        return self.service.transition(
            self.biobank, sample["id"], "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent_id},
        )

    def _approved_withdrawal(self, participant_id, sample_ids):
        withdrawal = self.service.create(
            self.biobank, "withdrawal",
            {"participant_id": participant_id, "requested_at": "2026-03-01"},
        )
        return self.service.transition(
            self.committee, withdrawal["id"], "approve",
            {"reason": "participant request", "sample_ids": sample_ids},
        )

    def test_execute_withdraws_all_active_consents_and_processes_samples(self):
        participant = self._setup_participant()
        consent_v1 = self._activate_consent(participant, "v1")
        self.service.transition(
            self.committee, consent_v1["id"], "supersede", {"reason": "new version"}
        )
        consent_v2 = self._activate_consent(participant, "v2")
        draft = self.service.create(
            self.committee, "consent",
            {"participant_id": participant, "scope": ["research"]},
        )

        stored = self._stored_sample(participant, consent_v2["id"], "B-1")
        loaned = self._stored_sample(participant, consent_v2["id"], "B-2")
        loaned = self.service.transition(
            self.biobank, loaned["id"], "loan",
            {"recipient": "Lab X", "purpose": "study", "due_at": "2026-10-01"},
        )

        withdrawal = self._approved_withdrawal(
            participant, [stored["id"], loaned["id"]]
        )

        result = self.service.transition(
            self.biobank, withdrawal["id"], "execute",
            {"executed_at": "2026-03-02"},
        )

        self.assertEqual(result["withdrawal"]["status"], "executed")
        # Only the effective (active) consent is withdrawn; superseded and draft stay.
        self.assertEqual(self.service.get(consent_v1["id"])["status"], "superseded")
        self.assertEqual(self.service.get(consent_v2["id"])["status"], "withdrawn")
        self.assertEqual(self.service.get(draft["id"])["status"], "draft")
        withdrawn_consents = {item["id"] for item in result["consents"]}
        self.assertEqual(withdrawn_consents, {consent_v2["id"]})

        # Stored sample destroyed, loaned sample held pending return.
        self.assertEqual(self.service.get(stored["id"])["status"], "destroyed")
        self.assertEqual(self.service.get(loaned["id"])["status"], "return_pending")
        self.assertEqual(result["summary"],
                         {"consents_withdrawn": 1, "samples_destroyed": 1,
                          "samples_pending_return": 1})

        # Returning a pending-return sample destroys it directly.
        returned = self.service.transition(
            self.biobank, loaned["id"], "return", {"note": "came back"}
        )
        self.assertEqual(returned["status"], "destroyed")
        self.assertTrue(returned["data"].get("destroyed_after_return"))
        self.assertTrue(
            returned["data"].get("pending_return_for_withdrawal"),
            withdrawal["id"],
        )

    def test_plan_lists_impact_scope_then_execute_applies_expected_versions(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])

        plan = self.service.transition(
            self.biobank, withdrawal["id"], "plan_execute",
            {"executed_at": "2026-03-02"},
        )
        self.assertEqual(plan["stage"], "impact")
        self.assertEqual(plan["withdrawal"]["status"], "approved")
        self.assertEqual(plan["samples"][0]["from_status"], "stored")
        self.assertEqual(plan["samples"][0]["to_status"], "destroyed")
        self.assertEqual(plan["consents"][0]["to_status"], "withdrawn")
        expected = plan["expected_versions"]

        # Execute with the versions captured from the impact preview.
        result = self.service.transition(
            self.biobank, withdrawal["id"], "execute",
            {"executed_at": "2026-03-02", "expected_versions": expected},
        )
        self.assertEqual(result["stage"], "executed")
        self.assertEqual(result["samples"][0]["status"], "destroyed")

    def test_consent_version_change_between_plan_and_execute_is_rejected(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])

        plan = self.service.transition(
            self.biobank, withdrawal["id"], "plan_execute",
            {"executed_at": "2026-03-02"},
        )
        expected = plan["expected_versions"]

        # Consent is superseded after the impact preview but before execution.
        self.service.transition(
            self.committee, consent["id"], "supersede", {"reason": "drift"}
        )

        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.biobank, withdrawal["id"], "execute",
                {"executed_at": "2026-03-02", "expected_versions": expected},
            )
        details = caught.exception.details
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["kind"], "consent")
        self.assertEqual(details[0]["id"], consent["id"])
        self.assertIn("version changed", details[0]["message"])
        self.assertEqual(details[0]["current_status"], "superseded")
        # Nothing changed after the rejected execution.
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")
        self.assertEqual(self.service.get(sample["id"])["status"], "stored")

    def test_sample_state_change_between_plan_and_execute_is_rejected(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])

        plan = self.service.transition(
            self.biobank, withdrawal["id"], "plan_execute",
            {"executed_at": "2026-03-02"},
        )

        # Sample is anonymized after the preview: no longer destroyable by withdrawal.
        self.service.transition(
            self.biobank, sample["id"], "anonymize", {"reason": "manual change"}
        )
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.biobank, withdrawal["id"], "execute",
                {"executed_at": "2026-03-02",
                 "expected_versions": plan["expected_versions"]},
            )
        sample_messages = [
            item["message"]
            for item in caught.exception.details
            if item["id"] == sample["id"]
        ]
        self.assertTrue(
            any("anonymized" in message for message in sample_messages),
            sample_messages,
        )
        self.assertEqual(self.service.get(sample["id"])["status"], "anonymized")

    def test_new_active_consent_after_plan_is_reported_as_conflict(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])

        plan = self.service.transition(
            self.biobank, withdrawal["id"], "plan_execute",
            {"executed_at": "2026-03-02"},
        )
        new_consent = self._activate_consent(participant, "v2")

        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.biobank, withdrawal["id"], "execute",
                {"executed_at": "2026-03-02",
                 "expected_versions": plan["expected_versions"]},
            )
        ids = {(item["kind"], item["id"]) for item in caught.exception.details}
        self.assertIn(("consent", new_consent["id"]), ids)

    def test_retry_with_same_idempotency_key_returns_first_result(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])

        request = {
            "actor": self.biobank, "action": "execute",
            "data": {"executed_at": "2026-03-02"},
            "idempotency_key": "execute-wd-1",
        }
        first = self.service.transition(
            request["actor"], withdrawal["id"], request["action"],
            request["data"], idempotency_key=request["idempotency_key"],
        )
        # The withdrawal is now executed; replaying the same request still returns
        # the first result instead of raising an invalid-transition/conflict error.
        replay = self.service.transition(
            request["actor"], withdrawal["id"], request["action"],
            request["data"], idempotency_key=request["idempotency_key"],
        )
        self.assertEqual(first, replay)
        self.assertEqual(replay["withdrawal"]["version"], first["withdrawal"]["version"])

        # A key bound to one withdrawal cannot be reused for another.
        other_withdrawal = self._approved_withdrawal(participant, [sample["id"]])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.biobank, other_withdrawal["id"], "execute",
                {"executed_at": "2026-03-02"},
                idempotency_key="execute-wd-1",
            )

    def test_execute_without_idempotency_key_then_replay_is_invalid_transition(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])

        self.service.transition(
            self.biobank, withdrawal["id"], "execute",
            {"executed_at": "2026-03-02"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.biobank, withdrawal["id"], "execute",
                {"executed_at": "2026-03-02"},
            )

    def test_approve_rejects_sample_from_another_participant(self):
        participant = self._setup_participant("P1")
        other = self._setup_participant("P2")
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self.service.create(
            self.biobank, "withdrawal",
            {"participant_id": other, "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.committee, withdrawal["id"], "approve",
                {"reason": "x", "sample_ids": [sample["id"]]},
            )

    def test_viewer_cannot_execute_or_plan(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])
        viewer = Actor("nosey", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                viewer, withdrawal["id"], "plan_execute",
                {"executed_at": "2026-03-02"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                viewer, withdrawal["id"], "execute",
                {"executed_at": "2026-03-02"},
            )

    def test_audit_trail_covers_the_whole_cascade(self):
        participant = self._setup_participant()
        consent = self._activate_consent(participant, "v1")
        sample = self._stored_sample(participant, consent["id"], "B-1")
        withdrawal = self._approved_withdrawal(participant, [sample["id"]])
        self.service.transition(
            self.biobank, withdrawal["id"], "execute",
            {"executed_at": "2026-03-02"},
        )
        actions = {
            (entry["entity_id"], entry["action"]): (
                entry["from_status"], entry["to_status"]
            )
            for entry in self.service.audit_log()
        }
        self.assertEqual(
            actions[(consent["id"], "withdraw")], ("active", "withdrawn")
        )
        self.assertEqual(
            actions[(sample["id"], "destroy")], ("stored", "destroyed")
        )
        self.assertEqual(
            actions[(withdrawal["id"], "execute")], ("approved", "executed")
        )


if __name__ == "__main__":
    unittest.main()
