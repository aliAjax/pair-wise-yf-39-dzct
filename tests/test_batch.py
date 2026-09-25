import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from src.domain import Actor, PermissionDenied, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def observation(event_id, observed_at="2026-09-24", **overrides):
    item = {
        "event_id": event_id,
        "observed_at": observed_at,
        "species": "麋鹿",
        "location": "北湖",
        "lat": 30.01,
        "lon": 120.01,
    }
    item.update(overrides)
    return item


class ObservationBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.field = Actor("patrol-01", "field")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _upload(self, device_id, batch_no, items, actor=None):
        return self.service.upload_observation_batch(
            actor or self.field,
            {"device_id": device_id, "batch_no": batch_no, "observations": items},
        )

    def test_first_batch_accepted_and_creates(self):
        status, response = self._upload(
            "DEV-07", "B-1", [observation("E-1"), observation("E-2")]
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["result"], "accepted")
        self.assertEqual(response["summary"],
                         {"received": 2, "created": 2, "skipped": 0, "conflicts": 0})
        entities = self.service.list("observation")
        self.assertEqual(len(entities), 2)

    def test_same_device_batch_no_replays_first_answer(self):
        first_status, first = self._upload(
            "DEV-07", "B-2", [observation("E-10")]
        )
        # Retry after network jitter, with tampered content: answer must be
        # replayed verbatim, not re-evaluated or overwritten.
        retried_status, retried = self._upload(
            "DEV-07", "B-2", [observation("E-10", species="白鹤")]
        )
        self.assertEqual(first_status, retried_status)
        self.assertEqual(first, retried)
        self.assertEqual(len(self.service.list("observation")), 1)

        stored = self.service.get_observation_batch("DEV-07", "B-2")
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(stored["response"], first)

    def test_identity_duplicate_inside_batch_rejected(self):
        with self.assertRaises(ValidationError):
            self._upload("DEV-07", "B-3", [observation("E-1"), observation("E-1")])
        self.assertEqual(self.service.list("observation"), [])

    def test_same_identity_same_content_is_skipped(self):
        self._upload("DEV-07", "B-4", [observation("E-20")])
        status, response = self._upload(
            "DEV-07", "B-4-RETRY", [observation("E-20")]
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["summary"]["created"], 0)
        self.assertEqual(response["summary"]["skipped"], 1)
        self.assertEqual(response["skipped"][0]["event_id"], "E-20")
        self.assertEqual(len(self.service.list("observation")), 1)

    def test_content_difference_rejects_whole_batch_with_server_version(self):
        # Device uploads the observation; duty officer later corrects species
        # and coordinates while reviewing.
        self._upload("DEV-07", "B-5", [observation("E-30")])
        entity = self.service.list("observation")[0]
        corrected = dict(entity["data"], species="白鹤", lat=30.09, lon=120.09)
        self.service.transition(
            self.admin, entity["id"], "submit",
            {"location": corrected["location"],
             "observed_at": corrected["observed_at"],
             "species": "白鹤", "lat": 30.09, "lon": 120.09},
        )

        # Retried batch mixes a conflicting item with a brand-new item.
        items = [
            observation("E-30", species="麋鹿", lat=30.01, lon=120.01),
            observation("E-31"),
        ]
        status, response = self._upload("DEV-07", "B-5-OLD", items)
        self.assertEqual(status, 409)
        self.assertEqual(response["result"], "rejected")
        self.assertEqual(response["reason"], "conflict")
        self.assertEqual(response["summary"]["conflicts"], 1)
        conflict = response["conflicts"][0]
        self.assertEqual(conflict["event_id"], "E-30")
        self.assertEqual(conflict["server_entity_id"], entity["id"])
        self.assertEqual(conflict["server_version"], 2)
        self.assertEqual(conflict["server"]["species"], "白鹤")
        self.assertEqual(conflict["server"]["lat"], 30.09)
        self.assertEqual(conflict["incoming"]["species"], "麋鹿")
        # Whole batch bounced: the unrelated new observation was not written.
        self.assertEqual(len(self.service.list("observation")), 1)

        # The rejection itself is the recorded answer; re-sending replays it.
        again_status, again = self._upload("DEV-07", "B-5-OLD", items)
        self.assertEqual(again_status, 409)
        self.assertEqual(again, response)

    def test_malformed_envelope_does_not_start_batch(self):
        with self.assertRaises(ValidationError):
            self.service.upload_observation_batch(
                self.field, {"device_id": "DEV-07", "batch_no": "B-6", "observations": []}
            )
        with self.assertRaises(ValidationError):
            self.service.upload_observation_batch(
                self.field,
                {"device_id": "DEV-07", "batch_no": "B-6",
                 "observations": [{"event_id": "E-40"}]},
            )
        self.assertIsNone(self.repo.get_observation_batch("DEV-07", "B-6"))

    def test_field_role_required(self):
        with self.assertRaises(PermissionDenied):
            self._upload("DEV-07", "B-7", [observation("E-50")],
                         actor=Actor("guest", "viewer"))


class ObservationBatchHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        service = DomainService(repo, RuleEngine())
        server = create_server("127.0.0.1", 0, service, RuleEngine(),
                               str(Path(__file__).resolve().parent.parent / "static"))
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.server = server
        self.base = "http://127.0.0.1:%s" % server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _post(self, body):
        request = urllib.request.Request(
            self.base + "/api/observation-batches",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-User-Id": "patrol-01", "X-Role": "field"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_http_accept_reject_and_replay(self):
        accepted = {
            "device_id": "DEV-1", "batch_no": "BH-1",
            "observations": [observation("H-1", location="南坡", lat=31.0, lon=121.0)],
        }
        status, body = self._post(accepted)
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["created"], 1)
        self.assertEqual(self._post(accepted)[0], 200)

        conflicting = dict(accepted, batch_no="BH-2")
        conflicting["observations"][0]["lat"] = 31.99
        status, body = self._post(conflicting)
        self.assertEqual(status, 409)
        self.assertEqual(body["summary"]["conflicts"], 1)
        self.assertEqual(body["conflicts"][0]["server"]["lat"], 31.0)
        # Repeated delivery replays the stored rejection.
        self.assertEqual(self._post(conflicting), (409, body))

    def test_http_query_batch_result(self):
        body = {"device_id": "DEV-2", "batch_no": "BQ-1",
                "observations": [observation("Q-1")]}
        self._post(body)
        with urllib.request.urlopen(
            self.base + "/api/observation-batches?device_id=DEV-2&batch_no=BQ-1"
        ) as response:
            stored = json.loads(response.read())
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(stored["response"]["summary"]["created"], 1)


if __name__ == "__main__":
    unittest.main()
