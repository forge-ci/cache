"""The service: a session over HTTP behaves exactly like the CLI underneath."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import cache_server
from cachelib.manifest import Key
from cachelib.sessions import SessionLog

KEY = {
    "repo": "obs-rust/obs-studio",
    "family": "app",
    "platform": "linux-x64",
    "identity": "rustc1.98.1-linux-x64-debug",
}
PAYLOAD = b"layer-bytes" * 100


class ServerTest(unittest.TestCase):
    """One fresh store, database and server per test: sequence numbers are state."""

    def start_server(self) -> None:
        """One fresh store, database and server — without opening a session."""
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.state = cache_server.State(root / "store", root / "sqlite.db")
        self.server = cache_server.make_server(self.state)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def setUp(self):
        self.start_server()
        self.session = self.open()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    # -- client ------------------------------------------------------------
    def call(self, method: str, path: str, body=None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def json(self, method: str, path: str, body=None):
        status, payload = self.call(method, path, body)
        return status, json.loads(payload)

    # -- fixtures ----------------------------------------------------------
    def open(self, **overrides) -> dict:
        status, payload = self.json("POST", "/sessions", {**KEY, **overrides})
        self.assertEqual(status, 201, payload)
        return payload

    def publish(self, session_id: str, payload: bytes = PAYLOAD):
        status, body = self.call("POST", f"/sessions/{session_id}/publish", raw=payload)
        return status, json.loads(body)


class LifecycleTest(ServerTest):
    def test_a_new_session_is_open_and_listed(self):
        status, payload = self.json("GET", f"/sessions/{self.session['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "open")
        self.assertEqual(payload["events"], [])

        status, listed = self.json("GET", "/sessions?repo=obs-rust/obs-studio")
        self.assertEqual(status, 200)
        self.assertIn(self.session["id"], [s["id"] for s in listed["sessions"]])

    def test_opening_without_an_identity_is_refused(self):
        status, payload = self.json(
            "POST",
            "/sessions",
            {"repo": "a/b", "family": "app", "platform": "linux-x64"},
        )
        self.assertEqual(status, 400)
        self.assertIn("identity", payload["error"])

    def test_a_miss_is_a_200_with_hit_false(self):
        status, payload = self.json("POST", f"/sessions/{self.session['id']}/restore")
        self.assertEqual(status, 200)
        self.assertFalse(payload["hit"])
        self.assertEqual(payload["layers"], [])
        self.assertTrue(payload["signature"].startswith("miss:"))

    def test_publish_then_restore_is_a_hit_with_the_blob_available(self):
        status, published = self.publish(self.session["id"])
        self.assertEqual(status, 201, published)
        self.assertEqual((published["kind"], published["seq"]), ("full", 1))
        self.assertEqual(published["bytes"], len(PAYLOAD))

        status, restored = self.json("POST", f"/sessions/{self.session['id']}/restore")
        self.assertEqual(status, 200)
        self.assertTrue(restored["hit"])
        self.assertEqual(len(restored["layers"]), 1)
        self.assertEqual(restored["layers"][0]["digest"], published["digest"])
        self.assertEqual(restored["bytes"], len(PAYLOAD))

        status, blob = self.call("GET", f"/blobs/{published['digest']}")
        self.assertEqual(status, 200)
        self.assertEqual(blob, PAYLOAD)

    def test_a_second_publish_in_the_same_identity_is_a_delta(self):
        self.publish(self.session["id"])
        status, published = self.publish(self.session["id"], b"changed" * 10)
        self.assertEqual(status, 201)
        self.assertEqual((published["kind"], published["seq"]), ("delta", 2))

    def test_closing_seals_the_session_and_further_work_is_409(self):
        status, payload = self.json("POST", f"/sessions/{self.session['id']}/close")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "closed")

        status, payload = self.call("POST", f"/sessions/{self.session['id']}/publish", raw=b"x")
        self.assertEqual(status, 409, payload)
        status, payload = self.json("POST", f"/sessions/{self.session['id']}/restore")
        self.assertEqual(status, 409)

    def test_closing_twice_is_allowed_and_changes_nothing(self):
        self.json("POST", f"/sessions/{self.session['id']}/close")
        status, payload = self.json("POST", f"/sessions/{self.session['id']}/close")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "closed")

    def test_a_failed_session_reports_failed(self):
        self.json("POST", f"/sessions/{self.session['id']}/close", {"outcome": "failed"})
        status, payload = self.json("GET", f"/sessions/{self.session['id']}")
        self.assertEqual(payload["status"], "failed")

    def test_an_unknown_session_is_404(self):
        status, _ = self.call("GET", "/sessions/" + "0" * 32)
        self.assertEqual(status, 404)
        status, _ = self.call("POST", "/sessions/" + "0" * 32 + "/restore")
        self.assertEqual(status, 404)

    def test_a_missing_blob_is_404_not_500(self):
        status, _ = self.call("GET", "/blobs/sha256:" + "1" * 64)
        self.assertEqual(status, 404)

    def test_a_malformed_key_is_400(self):
        status, payload = self.json("POST", "/sessions", {**KEY, "platform": "linux/x64"})
        self.assertEqual(status, 400, payload)

    def test_an_empty_publish_body_is_refused(self):
        status, payload = self.call("POST", f"/sessions/{self.session['id']}/publish", raw=b"")
        self.assertEqual(status, 400)
        self.assertIn("layer bytes", json.loads(payload)["error"])

    def test_health_reports_the_store_and_database(self):
        status, payload = self.json("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("sqlite.db", payload["db"])


class SweepTest(ServerTest):
    """/sweep is the scheduled entry point, and it is dry unless told otherwise."""

    def publish_two_chains(self) -> None:
        """MAX_DELTA_CHAIN deltas then a new base: the smallest real second chain."""
        session = self.session["id"]
        for index in range(7):
            self.assertEqual(self.publish(session, f"layer-{index}".encode() * 20)[0], 201)

    def test_a_dry_run_is_the_default_and_deletes_nothing(self):
        self.publish_two_chains()
        status, payload = self.json("POST", "/sweep", {"keep": 1})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["dry_run"])
        self.assertGreaterEqual(payload["keys"], 1)
        status, listed = self.json("GET", f"/sessions/{self.session['id']}")
        self.assertEqual(status, 200)

    def test_an_explicit_sweep_bounds_the_store(self):
        self.publish_two_chains()
        status, payload = self.json("POST", "/sweep", {"keep": 1, "dry_run": False})
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["dry_run"])
        self.assertGreaterEqual(sum(len(report["evicted"]) for report in payload["reports"]), 1)
        listing = self.json("GET", "/health")[1]
        self.assertGreaterEqual(listing["keys"], 1)

    def test_a_bad_keep_is_a_400_not_a_crash(self):
        status, payload = self.json("POST", "/sweep", {"keep": 0, "dry_run": True})
        self.assertEqual(status, 400, payload)


class AuthTest(ServerTest):
    """A service reachable beyond localhost must not accept anonymous writes."""

    TOKEN = "test-token-123"

    def setUp(self):
        # No session here: with a token set, opening one would itself be a 401.
        self._saved = cache_server.AUTH_TOKEN
        cache_server.AUTH_TOKEN = self.TOKEN
        self.start_server()

    def tearDown(self):
        super().tearDown()
        cache_server.AUTH_TOKEN = self._saved

    def call_with_token(self, path: str, token: str | None):
        request = urllib.request.Request(self.base + path, method="GET")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_a_request_without_the_token_is_refused(self):
        self.assertEqual(self.call_with_token("/health", None), 401)
        self.assertEqual(self.call_with_token("/health", "wrong"), 401)

    def test_the_right_token_is_accepted(self):
        self.assertEqual(self.call_with_token("/health", self.TOKEN), 200)

    def test_opens_a_session_requires_the_token_too(self):
        # Every route is behind the check, not just the ones that look dangerous.
        status, _ = self.call("POST", "/sessions", {**KEY})
        self.assertEqual(status, 401)


class ReplayOverHttpTest(ServerTest):
    def test_a_whole_session_replays_identically(self):
        session_id = self.session["id"]
        self.json("POST", f"/sessions/{session_id}/restore")  # miss
        self.publish(session_id)  # full
        self.json("POST", f"/sessions/{session_id}/restore")  # now a hit
        self.json("POST", f"/sessions/{session_id}/close")

        status, payload = self.json("POST", f"/sessions/{session_id}/replay", {"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["identical"], [s for s in payload["steps"] if not s["matches"]])
        self.assertEqual(
            [s["op"] for s in payload["steps"]],
            ["restore", "publish", "restore", "close"],
        )

    def test_a_live_replay_reads_the_blobs(self):
        session_id = self.session["id"]
        self.publish(session_id)
        self.json("POST", f"/sessions/{session_id}/restore")
        status, payload = self.json("POST", f"/sessions/{session_id}/replay", {"dry_run": False})
        self.assertEqual(status, 200)
        restore_step = next(s for s in payload["steps"] if s["op"] == "restore")
        self.assertIn("read 1 layer(s)", restore_step["note"])


class LoggingTest(ServerTest):
    def test_every_operation_lands_in_sqlite_as_an_event(self):
        session_id = self.session["id"]
        self.json("POST", f"/sessions/{session_id}/restore")
        self.publish(session_id)
        self.json("POST", f"/sessions/{session_id}/close")

        with SessionLog(self.state.db_path) as log:
            events = log.events(session_id)
            session = log.get(session_id)
        self.assertEqual([e["op"] for e in events], ["restore", "publish", "close"])
        self.assertEqual([e["ordinal"] for e in events], [1, 2, 3])
        self.assertEqual(session.status, "closed")
        self.assertEqual(session.key, Key(**{k: KEY[k] for k in ("repo", "family", "platform")}))


if __name__ == "__main__":
    unittest.main()
