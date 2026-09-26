import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class WithdrawalExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.biobank = Actor("banker", "biobank")
        self.participant = self.service.create(
            self.admin, "participant", {"name": "Participant One"}
        )
        self.consent = self.service.create(
            self.admin,
            "consent",
            {"participant_id": self.participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.admin,
            self.consent["id"],
            "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _make_sample(self, code, store=True, loan=False):
        sample = self.service.create(
            self.admin,
            "sample",
            {
                "participant_id": self.participant["id"],
                "sample_code": code,
                "collected_at": "2026-01-01",
            },
        )
        if store:
            self.service.transition(
                self.biobank,
                sample["id"],
                "store",
                {"freezer": "F1", "position": "A1", "consent_id": self.consent["id"]},
            )
        if loan:
            self.service.transition(
                self.biobank,
                sample["id"],
                "loan",
                {"recipient": "Lab", "purpose": "analysis", "due_at": "2026-12-01"},
            )
        return sample

    def _approve(self, sample_ids):
        withdrawal = self.service.create(
            self.biobank,
            "withdrawal",
            {"participant_id": self.participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.admin,
            withdrawal["id"],
            "approve",
            {"reason": "participant request", "sample_ids": sample_ids},
        )
        return withdrawal

    def test_execute_cascades_consents_and_samples(self):
        stored = self._make_sample("B-001")
        loaned = self._make_sample("B-002", loan=True)
        collected = self._make_sample("B-003", store=False)
        withdrawal = self._approve([stored["id"], loaned["id"], collected["id"]])

        result = self.service.transition(
            self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )

        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.service.get(self.consent["id"])["status"], "withdrawn")
        self.assertEqual(self.service.get(stored["id"])["status"], "destroyed")
        self.assertEqual(self.service.get(collected["id"])["status"], "destroyed")
        self.assertEqual(self.service.get(loaned["id"])["status"], "pending_return")

        report = result["data"]["execution"]["report"]
        affected = {item["id"]: item for item in report["affected"]}
        self.assertEqual(affected[self.consent["id"]]["to_status"], "withdrawn")
        self.assertEqual(affected[stored["id"]]["to_status"], "destroyed")
        self.assertEqual(affected[loaned["id"]]["to_status"], "pending_return")

    def test_pending_return_sample_is_destroyed_on_return(self):
        loaned = self._make_sample("B-010", loan=True)
        withdrawal = self._approve([loaned["id"]])
        self.service.transition(
            self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        self.assertEqual(self.service.get(loaned["id"])["status"], "pending_return")

        returned = self.service.transition(
            self.biobank, loaned["id"], "return", {"returned_at": "2026-03-05"}
        )
        self.assertEqual(returned["status"], "destroyed")
        self.assertIn("destroy_reason", returned["data"])

    def test_normal_return_still_moves_back_to_stored(self):
        loaned = self._make_sample("B-011", loan=True)
        returned = self.service.transition(self.biobank, loaned["id"], "return", {})
        self.assertEqual(returned["status"], "stored")

    def test_execute_rejected_when_consent_changed_after_approval(self):
        sample = self._make_sample("B-020")
        withdrawal = self._approve([sample["id"]])
        self.service.transition(
            self.admin, self.consent["id"], "supersede", {"reason": "new version"}
        )

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
            )
        self.assertIn(self.consent["id"], str(ctx.exception))
        self.assertEqual(self.service.get(withdrawal["id"])["status"], "approved")
        self.assertEqual(self.service.get(sample["id"])["status"], "stored")

    def test_execute_rejected_when_sample_status_changed_after_approval(self):
        sample = self._make_sample("B-021")
        withdrawal = self._approve([sample["id"]])
        self.service.transition(
            self.biobank,
            sample["id"],
            "loan",
            {"recipient": "Lab", "purpose": "analysis", "due_at": "2026-12-01"},
        )

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
            )
        message = str(ctx.exception)
        self.assertIn(sample["id"], message)
        self.assertIn("stored -> on_loan", message)

    def test_execute_rejected_when_new_consent_activated_after_approval(self):
        sample = self._make_sample("B-022")
        withdrawal = self._approve([sample["id"]])
        extra = self.service.create(
            self.admin,
            "consent",
            {"participant_id": self.participant["id"], "scope": ["research"]},
        )
        self.service.transition(
            self.admin,
            extra["id"],
            "activate",
            {"scope": ["research"], "version": "v2", "expires_at": "2099-01-01"},
        )

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
            )
        self.assertIn("became active after approval", str(ctx.exception))

    def test_same_execute_request_retry_returns_first_result(self):
        sample = self._make_sample("B-030")
        withdrawal = self._approve([sample["id"]])
        first = self.service.transition(
            self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        second = self.service.transition(
            self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(
            first["data"]["execution"]["report"],
            second["data"]["execution"]["report"],
        )

        with self.assertRaises(ConflictError):
            self.service.transition(
                self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-04-01"}
            )

    def test_impact_preview_before_and_after_execution(self):
        stored = self._make_sample("B-040")
        loaned = self._make_sample("B-041", loan=True)
        withdrawal = self._approve([stored["id"], loaned["id"]])

        preview = self.service.get_withdrawal_impact(withdrawal["id"])
        self.assertEqual(preview["phase"], "preview")
        self.assertEqual(preview["conflicts"], [])
        self.assertEqual(preview["consents"][0]["planned_action"], "withdraw")
        planned = {item["id"]: item["planned_action"] for item in preview["samples"]}
        self.assertEqual(planned[stored["id"]], "destroy")
        self.assertEqual(planned[loaned["id"]], "pending_return")

        self.service.transition(
            self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        done = self.service.get_withdrawal_impact(withdrawal["id"])
        self.assertEqual(done["phase"], "executed")
        affected = {item["id"]: item for item in done["report"]["affected"]}
        self.assertEqual(affected[stored["id"]]["to_status"], "destroyed")
        self.assertEqual(affected[loaned["id"]]["to_status"], "pending_return")

    def test_impact_preview_requires_approved_withdrawal(self):
        withdrawal = self.service.create(
            self.biobank,
            "withdrawal",
            {"participant_id": self.participant["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.get_withdrawal_impact(withdrawal["id"])

    def test_approve_rejects_sample_of_another_participant(self):
        other = self.service.create(self.admin, "participant", {"name": "Other Person"})
        foreign = self.service.create(
            self.admin,
            "sample",
            {
                "participant_id": other["id"],
                "sample_code": "X-001",
                "collected_at": "2026-01-01",
            },
        )
        withdrawal = self.service.create(
            self.biobank,
            "withdrawal",
            {"participant_id": self.participant["id"], "requested_at": "2026-03-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                withdrawal["id"],
                "approve",
                {"reason": "participant request", "sample_ids": [foreign["id"]]},
            )

    def test_execution_writes_audit_for_cascaded_entities(self):
        stored = self._make_sample("B-050")
        loaned = self._make_sample("B-051", loan=True)
        withdrawal = self._approve([stored["id"], loaned["id"]])
        self.service.transition(
            self.biobank, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )

        consent_actions = [
            row["action"] for row in self.service.audit_log(self.consent["id"])
        ]
        self.assertIn("withdraw", consent_actions)
        stored_actions = [
            row["action"] for row in self.service.audit_log(stored["id"])
        ]
        self.assertIn("destroy", stored_actions)
        loaned_actions = [
            row["action"] for row in self.service.audit_log(loaned["id"])
        ]
        self.assertIn("mark_pending_return", loaned_actions)


if __name__ == "__main__":
    unittest.main()
