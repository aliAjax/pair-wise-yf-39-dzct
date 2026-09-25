import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.domain import Actor, NotFoundError, PermissionDenied, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


OBS_A = {
    "event_id": "E-101",
    "species": "麋鹿",
    "location": "北湖岸",
    "observed_at": "2026-09-24",
    "lat": 30.12,
    "lon": 120.34,
}
OBS_B = {
    "event_id": "E-102",
    "species": "白鹭",
    "location": "芦苇荡",
    "observed_at": "2026-09-24",
    "lat": 30.13,
    "lon": 120.36,
}


def _batch(device="D-001", batch_no="B-1", observations=None):
    return {
        "device_id": device,
        "batch_no": batch_no,
        "observations": (
            [dict(OBS_A), dict(OBS_B)] if observations is None else observations
        ),
    }


class BatchServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.ranger = Actor("ranger-1", "field")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_batch_accepts_and_creates_captured_observations(self):
        status, body = self.service.submit_batch(self.ranger, _batch())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(len(body["accepted"]), 2)
        self.assertEqual(body["skipped"], [])
        entities = self.repo.list_entities(kind="observation")
        self.assertEqual(len(entities), 2)
        self.assertTrue(all(e["status"] == "captured" for e in entities))

    def test_same_batch_replayed_returns_first_response_without_duplicates(self):
        status1, body1 = self.service.submit_batch(self.ranger, _batch())
        status2, body2 = self.service.submit_batch(self.ranger, _batch())
        self.assertEqual((status1, body1), (status2, body2))
        self.assertEqual(len(self.repo.list_entities(kind="observation")), 2)
        # 审计只写一次
        self.assertEqual(len(self.repo.list_audit()), 2)

    def test_replayed_batch_reuses_response_even_under_different_actor(self):
        self.service.submit_batch(self.ranger, _batch())
        status, body = self.service.submit_batch(self.admin, _batch())
        self.assertEqual(status, 200)
        self.assertEqual(len(body["accepted"]), 2)
        self.assertEqual(len(self.repo.list_entities(kind="observation")), 2)

    def test_identical_observations_in_later_batch_are_skipped(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-1"))
        status, body = self.service.submit_batch(self.ranger, _batch(batch_no="B-2"))
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], [])
        self.assertEqual(len(body["skipped"]), 2)
        self.assertTrue(all(item["reason"] == "identical" for item in body["skipped"]))
        self.assertEqual(len(self.repo.list_entities(kind="observation")), 2)

    def test_observed_at_date_part_determines_identity(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-date-1"))
        # 带时间部分的 observed_at，日期相同仍视为同一条
        batch = _batch(
            batch_no="B-date-2",
            observations=[{**OBS_A, "observed_at": "2026-09-24T08:30:00"}, dict(OBS_B)],
        )
        status, body = self.service.submit_batch(self.ranger, batch)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["skipped"]), 2)

    def test_corrected_species_conflicts_and_whole_batch_is_rejected(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-1"))
        corrected = {**OBS_A, "species": "梅花鹿"}
        status, body = self.service.submit_batch(
            self.ranger, _batch(batch_no="B-3", observations=[corrected, dict(OBS_B)])
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(len(body["conflicts"]), 1)
        conflict = body["conflicts"][0]
        self.assertEqual(conflict["event_id"], "E-101")
        self.assertEqual(conflict["incoming"]["species"], "梅花鹿")
        self.assertEqual(conflict["server"]["species"], "麋鹿")
        self.assertIn("server_version", conflict)
        self.assertIn("server_entity_id", conflict)
        # 无冲突的 OBS_B 也不能落库：观察总数仍是第一批的 2 条
        self.assertEqual(len(self.repo.list_entities(kind="observation")), 2)

    def test_corrected_coordinates_conflict(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-1"))
        moved = {**OBS_B, "lat": 30.99, "lon": 120.99}
        status, body = self.service.submit_batch(
            self.ranger, _batch(batch_no="B-4", observations=[moved])
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["conflicts"][0]["server"]["lat"], 30.13)

    def test_conflicting_batch_replay_returns_first_rejection(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-1"))
        corrected = {**OBS_A, "species": "梅花鹿"}
        first = self.service.submit_batch(
            self.ranger, _batch(batch_no="B-5", observations=[corrected])
        )
        second = self.service.submit_batch(
            self.ranger, _batch(batch_no="B-5", observations=[corrected])
        )
        self.assertEqual(first, second)

    def test_duplicate_natural_key_with_different_content_inside_batch_fails(self):
        other = {**OBS_A, "species": "野猪"}
        with self.assertRaises(ValidationError):
            self.service.submit_batch(
                self.ranger, _batch(observations=[dict(OBS_A), other])
            )
        self.assertEqual(self.repo.list_entities(kind="observation"), [])

    def test_duplicate_natural_key_identical_inside_batch_is_collapsed(self):
        status, body = self.service.submit_batch(
            self.ranger, _batch(observations=[dict(OBS_A), dict(OBS_A), dict(OBS_B)])
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["accepted"]), 2)

    def test_missing_fields_are_validation_errors_and_no_receipt_stored(self):
        bad = {key: OBS_A[key] for key in OBS_A if key != "species"}
        with self.assertRaises(ValidationError):
            self.service.submit_batch(self.ranger, _batch(observations=[bad]))
        with self.assertRaises(NotFoundError):
            self.service.get_batch_result("D-001", "B-1")

    def test_empty_batch_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.submit_batch(self.ranger, _batch(observations=[]))

    def test_viewer_cannot_submit_batch(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_batch(Actor("v", "viewer"), _batch())

    def test_get_batch_result_returns_stored_response(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-7"))
        status, body = self.service.get_batch_result("D-001", "B-7")
        self.assertEqual(status, 200)
        self.assertEqual(body["batch_no"], "B-7")

    def test_rejected_receipt_is_queryable(self):
        self.service.submit_batch(self.ranger, _batch(batch_no="B-1"))
        self.service.submit_batch(
            self.ranger,
            _batch(batch_no="B-8", observations=[{**OBS_A, "species": "x"}]),
        )
        status, body = self.service.get_batch_result("D-001", "B-8")
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "rejected")

    def test_batches_from_different_devices_are_independent(self):
        # D-2 提交内容相同的观察：跳过已有观察，但批次本身独立受理
        self.service.submit_batch(self.ranger, _batch(device="D-1", batch_no="B-1"))
        status, body = self.service.submit_batch(
            self.ranger, _batch(device="D-2", batch_no="B-1")
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["accepted"], [])
        self.assertEqual(len(body["skipped"]), 2)
        self.assertEqual(len(self.repo.list_entities(kind="observation")), 2)

    def test_batches_from_different_devices_can_create_different_observations(self):
        self.service.submit_batch(self.ranger, _batch(device="D-1", batch_no="B-1"))
        other = [{**OBS_A, "event_id": "E-201"}, {**OBS_B, "event_id": "E-202"}]
        status, body = self.service.submit_batch(
            self.ranger, _batch(device="D-2", batch_no="B-1", observations=other)
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["accepted"]), 2)
        self.assertEqual(len(self.repo.list_entities(kind="observation")), 4)

    def test_batch_no_duplicate_does_not_block_other_devices(self):
        self.service.submit_batch(self.ranger, _batch(device="D-1", batch_no="B-9"))
        status, _ = self.service.submit_batch(
            self.ranger, _batch(device="D-2", batch_no="B-9")
        )
        self.assertEqual(status, 200)


class BatchHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = SQLiteRepository(Path(cls.tmp.name) / "http.db")
        cls.service = DomainService(cls.repo, RuleEngine())
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        cls.server = create_server("127.0.0.1", 0, cls.service, RuleEngine(), static_dir)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def _post(self, path, payload, role="field", user="ranger-http"):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Role": role, "X-User-Id": user},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        request = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_http_accept_replay_and_conflict_flow(self):
        batch = _batch(device="D-HTTP", batch_no="B-1")
        status, body = self._post("/api/batches", batch)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["accepted"]), 2)

        status2, body2 = self._post("/api/batches", batch)
        self.assertEqual(status2, 200)
        self.assertEqual(body2, body)

        status, body = self._get("/api/batches/D-HTTP/B-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "accepted")

        conflict_batch = _batch(
            device="D-HTTP",
            batch_no="B-2",
            observations=[{**OBS_A, "species": "野猪"}, dict(OBS_B)],
        )
        status, body = self._post("/api/batches", conflict_batch)
        self.assertEqual(status, 409)
        self.assertEqual(body["status"], "rejected")
        self.assertEqual(body["conflicts"][0]["server"]["species"], "麋鹿")

        status, body = self._get("/api/batches/D-HTTP/B-2")
        self.assertEqual(status, 409)

    def test_http_missing_batch_returns_404(self):
        status, body = self._get("/api/batches/nope/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
