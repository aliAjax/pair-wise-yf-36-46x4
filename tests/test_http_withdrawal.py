import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


HEADERS = {"Content-Type": "application/json"}


def role_headers(role, extra=None):
    headers = dict(HEADERS)
    headers["X-User-Id"] = "u-" + role
    headers["X-Role"] = role
    if extra:
        headers.update(extra)
    return headers


class HttpWithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "http.db")
        self.service = DomainService(self.repo, RuleEngine())
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, payload=None, headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data, method=method, headers=headers or HEADERS,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def _action(self, entity_id, action, data, role="admin", idem=None):
        headers = role_headers(role, {"Idempotency-Key": idem} if idem else None)
        return self._request(
            "POST", "/api/entities/%s/actions" % entity_id,
            {"action": action, "data": data}, headers,
        )

    def _seed(self):
        _, participant = self._request(
            "POST", "/api/participants", {"name": "HTTP Participant"},
            role_headers("admin"),
        )
        _, consent = self._request(
            "POST", "/api/consents",
            {"participant_id": participant["id"], "scope": ["research"]},
            role_headers("committee"),
        )
        status, consent = self._action(
            consent["id"], "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2099-01-01"},
            role="committee",
        )
        _, sample = self._request(
            "POST", "/api/samples",
            {"participant_id": participant["id"], "sample_code": "H-1",
             "collected_at": "2026-01-01"},
            role_headers("biobank"),
        )
        _, sample = self._action(
            sample["id"], "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"]},
            role="biobank",
        )
        _, withdrawal = self._request(
            "POST", "/api/withdrawals",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
            role_headers("biobank"),
        )
        _, withdrawal = self._action(
            withdrawal["id"], "approve",
            {"reason": "please withdraw", "sample_ids": [sample["id"]]},
            role="committee",
        )
        return participant, consent, sample, withdrawal

    def test_plan_execute_conflict_replay_over_http(self):
        participant, consent, sample, withdrawal = self._seed()

        # 1. Impact preview before execution.
        status, plan = self._action(
            withdrawal["id"], "plan_execute", {"executed_at": "2026-03-02"},
            role="biobank",
        )
        self.assertEqual(status, 200)
        self.assertEqual(plan["stage"], "impact")
        self.assertEqual([c["id"] for c in plan["consents"]], [consent["id"]])
        self.assertEqual(plan["samples"][0]["to_status"], "destroyed")

        # 2. Version drift before confirmation -> 409 with per-entity details.
        self._action(
            consent["id"], "supersede", {"reason": "drift"}, role="committee"
        )
        status, error = self._action(
            withdrawal["id"], "execute",
            {"executed_at": "2026-03-02",
             "expected_versions": plan["expected_versions"]},
            role="biobank",
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["type"], "ConflictError")
        self.assertTrue(error["details"])
        self.assertEqual(error["details"][0]["id"], consent["id"])

        # 3. Retrying the same request with an idempotency key after a successful
        #    execution returns the first result. The superseded consent is no longer
        #    active, so the execution only destroys the listed sample.
        status, first = self._action(
            withdrawal["id"], "execute", {"executed_at": "2026-03-02"},
            role="biobank", idem="wd-exec-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(first["stage"], "executed")
        self.assertEqual(first["summary"]["consents_withdrawn"], 0)
        self.assertEqual(first["summary"]["samples_destroyed"], 1)
        status, replay = self._action(
            withdrawal["id"], "execute", {"executed_at": "2026-03-02"},
            role="biobank", idem="wd-exec-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)

        status, shown = self._request(
            "GET", "/api/entities/%s" % sample["id"],
        )
        self.assertEqual(shown["status"], "destroyed")


if __name__ == "__main__":
    unittest.main()
