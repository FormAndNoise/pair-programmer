"""Tests for WebEventBridge and WebSocket event streaming."""

import asyncio
from collections import OrderedDict
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from agent_core.events import AgentEvent, EventBus, EventType
from agent_core.types import UserStory
from server.bridge import (
    SprintRunCoordinator,
    UNAUTH_REST_RATE_LIMIT,
    WS_HANDSHAKE_RATE_LIMIT,
    WebEventBridge,
    _load_or_create_auth_token,
    create_fastapi_app,
    evaluate_rest_access,
    evaluate_ws_handshake,
    stream_ws_events_with_idle_timeout,
)


class TestWebEventBridge(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.bridge = WebEventBridge(self.bus)

    def test_client_registration_and_event_forwarding(self):
        client_q = self.bridge.register_client()
        self.assertEqual(len(self.bridge._connected_queues), 1)

        # Publish an event on the bus
        self.bus.publish(
            AgentEvent(
                event_type=EventType.STORY_START,
                source_model="claude-3-5",
                payload={"story_id": "US-01", "title": "Build Auth API"},
            )
        )

        # Verify client queue received the event payload
        self.assertFalse(client_q.empty())
        item = client_q.get_nowait()
        self.assertEqual(item["type"], "story:start")
        self.assertEqual(item["source_model"], "claude-3-5")
        self.assertEqual(item["payload"]["story_id"], "US-01")

        # Unregister
        self.bridge.unregister_client(client_q)
        self.assertEqual(len(self.bridge._connected_queues), 0)

    def test_operator_steering_injection(self):
        received_steering = []
        self.bus.subscribe(
            EventType.OPERATOR_STEER,
            lambda e: received_steering.append(e.payload["guidance"]),
        )

        self.bridge.handle_operator_steer("Avoid editing the database schema.")
        self.assertEqual(len(received_steering), 1)
        self.assertEqual(received_steering[0], "Avoid editing the database schema.")

    def test_status_summary(self):
        self.bus.publish(AgentEvent(event_type=EventType.SPRINT_START))
        self.bus.publish(AgentEvent(event_type=EventType.STORY_START))

        summary = self.bridge.get_status_summary()
        self.assertEqual(summary["total_events"], 2)
        self.assertEqual(summary["last_event"], "story:start")


class TestFastAPIFactory(unittest.TestCase):
    """Verify FastAPI application factory, rate-limiting, WS security, and sprint coordination."""

    def setUp(self):
        self.auth_env = mock.patch.dict(os.environ, {"PAIR_PROGRAMMER_TOKEN": "test-token"})
        self.auth_env.start()
        self.addCleanup(self.auth_env.stop)
        self.bus = EventBus()
        self.bridge = WebEventBridge(self.bus)

    def test_create_fastapi_app_without_engine(self):
        app = create_fastapi_app(self.bridge, engine=None)
        if app is not None:
            self.assertEqual(app.title, "Pair Programming Agent API")
            self.assertEqual(app.state.session_status, "ready")

    def test_fastapi_endpoints_via_testclient(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi or httpx not available in current environment")

        mock_engine = mock.MagicMock()
        mock_engine.config.model_a = "model-a"
        mock_engine.config.model_b = "model-b"
        mock_engine.backlog_mgr.sprint.epic_goal = "Test Epic"
        mock_engine.backlog_mgr.sprint.current_index = 0
        mock_engine.backlog_mgr.sprint.is_complete = False
        mock_engine.backlog_mgr.sprint.stories = []
        mock_engine.is_paused = False
        mock_engine.stop_requested = False

        app = create_fastapi_app(self.bridge, engine=mock_engine)
        self.assertIsNotNone(app)
        client = TestClient(app, headers={"Authorization": "Bearer test-token"})

        res = client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["session_status"], "ready")
        self.assertEqual(data["epic"], "Test Epic")

        res = client.get("/api/events")
        self.assertEqual(res.status_code, 200)
        self.assertIn("events", res.json())

        res = client.post("/api/operator/steer", json={"guidance": "Test steering"})
        self.assertEqual(res.status_code, 200)

        res = client.post("/api/sprint/stop")
        self.assertEqual(res.status_code, 409)

        res = client.post("/api/sprint/pause")
        self.assertEqual(res.status_code, 409)

    def test_unauthenticated_rest_flood_does_not_starve_authenticated_client(self):
        """R-16: Unauthenticated REST flood consumes unauth bucket and never starves authenticated requests."""
        buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        lock = threading.Lock()
        codes = []
        for _ in range(UNAUTH_REST_RATE_LIMIT + 5):
            status, _, _ = evaluate_rest_access(
                method="GET",
                origin=None,
                authorization="Bearer wrong-token",
                remote="127.0.0.1",
                auth_token="test-token",
                rate_buckets=buckets,
                rate_lock=lock,
            )
            codes.append(status)
        self.assertIn(401, codes)
        self.assertIn(429, codes)

        # Authenticated request from the same IP is unaffected and succeeds with 200
        auth_status, _, _ = evaluate_rest_access(
            method="GET",
            origin=None,
            authorization="Bearer test-token",
            remote="127.0.0.1",
            auth_token="test-token",
            rate_buckets=buckets,
            rate_lock=lock,
        )
        self.assertEqual(auth_status, 200)

    def test_websocket_handshake_rate_limit_subprotocol_redaction_and_idle_timeout(self):
        """R-10 / R-16: Rapid WS handshakes are rate-limited, bearer subprotocols are redacted, and idle sockets close."""
        buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        lock = threading.Lock()

        # First valid handshake succeeds and redacts the bearer subprotocol in scope
        scope = {"subprotocols": ["pair-programmer", "bearer.test-token"]}
        allowed, code, reason, accepted = evaluate_ws_handshake(
            scope=scope,
            origin="http://localhost:5173",
            remote="127.0.0.1",
            auth_token="test-token",
            rate_buckets=buckets,
            rate_lock=lock,
        )
        self.assertTrue(allowed)
        self.assertEqual(accepted, "pair-programmer")
        self.assertEqual(scope["subprotocols"], ["pair-programmer", "bearer.[REDACTED]"])

        # Exhaust WS handshake bucket with rapid failed auth attempts
        for _ in range(WS_HANDSHAKE_RATE_LIMIT):
            s = {"subprotocols": ["pair-programmer", "bearer.wrong-token"]}
            ok, c, r, _ = evaluate_ws_handshake(
                scope=s,
                origin="http://localhost:5173",
                remote="127.0.0.1",
                auth_token="test-token",
                rate_buckets=buckets,
                rate_lock=lock,
            )
            self.assertFalse(ok)
            self.assertEqual(c, 1008)
            self.assertEqual(s["subprotocols"], ["pair-programmer", "bearer.[REDACTED]"])

        # Subsequent handshake within window is denied with rate-limit reason
        s_after = {"subprotocols": ["pair-programmer", "bearer.test-token"]}
        ok_after, code_after, reason_after, _ = evaluate_ws_handshake(
            scope=s_after,
            origin="http://localhost:5173",
            remote="127.0.0.1",
            auth_token="test-token",
            rate_buckets=buckets,
            rate_lock=lock,
        )
        self.assertFalse(ok_after)
        self.assertEqual(code_after, 1008)
        self.assertIn("Too many handshake attempts", reason_after)

        # Verify idle WebSocket closes automatically after idle_timeout
        class _FakeWebSocket:
            def __init__(self):
                self.closed_with = None
                self.sent = []

            async def send_text(self, text: str):
                self.sent.append(text)

            async def close(self, code: int = 1000, reason: str = ""):
                self.closed_with = (code, reason)

        async def _verify_idle():
            ws = _FakeWebSocket()
            q: asyncio.Queue = asyncio.Queue()
            await stream_ws_events_with_idle_timeout(ws, q, idle_timeout=0.03)
            self.assertIsNotNone(ws.closed_with)
            self.assertEqual(ws.closed_with[0], 1000)
            self.assertIn("Idle timeout", ws.closed_with[1])

        asyncio.run(_verify_idle())

    def test_token_file_race_and_unified_run_id(self):
        """R-17: _load_or_create_auth_token recovers from paused/empty writer and start_sprint uses one run_id."""
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp) / ".pair_programmer"
            token_dir.mkdir(parents=True, exist_ok=True)
            token_file = token_dir / "token"
            token_file.write_text("", encoding="utf-8")

            def delayed_writer():
                time.sleep(0.025)
                token_file.write_text("concurrent-writer-token-xyz\n", encoding="utf-8")

            writer = threading.Thread(target=delayed_writer)
            writer.start()
            resolved = _load_or_create_auth_token(token_path=token_file, retry_attempts=20, retry_delay=0.005)
            writer.join()
            self.assertEqual(resolved, "concurrent-writer-token-xyz")

            # Simulate a crashed writer leaving a permanently empty file -> atomic recovery
            token_file.write_text("", encoding="utf-8")
            recovered = _load_or_create_auth_token(token_path=token_file, retry_attempts=2, retry_delay=0.001)
            self.assertTrue(len(recovered) >= 16)
            self.assertEqual(token_file.read_text(encoding="utf-8").strip(), recovered)

        # Verify SprintRunCoordinator uses one unified run_id and rejects concurrent starts
        captured_run_ids = []
        mock_engine = mock.MagicMock()
        mock_engine.stop_requested = False

        def fake_run_sprint(run_id=None):
            captured_run_ids.append(run_id)
            time.sleep(0.05)
            raise RuntimeError("boom")

        mock_engine.run_sprint.side_effect = fake_run_sprint
        coordinator = SprintRunCoordinator(self.bridge, mock_engine)
        stories = [
            UserStory(
                title="Story 1",
                human_interaction_contract="`f(1)` -> `1`",
                acceptance_criteria=["works"],
            )
        ]

        async def _verify_coordinator():
            result = await coordinator.start("Verify unified run_id", stories)
            returned_run_id = result["run_id"]
            # Second concurrent start while running must raise RuntimeError (409 in HTTP handler)
            with self.assertRaises(RuntimeError):
                await coordinator.start("Duplicate start", stories)
            if coordinator.sprint_task is not None:
                await coordinator.sprint_task
            self.assertEqual(coordinator.session_status, "failed")
            self.assertEqual(captured_run_ids, [returned_run_id])
            error_events = [e for e in self.bus.get_history() if e.event_type == EventType.ERROR]
            self.assertTrue(any(e.payload.get("run_id") == returned_run_id for e in error_events))

        asyncio.run(_verify_coordinator())


if __name__ == "__main__":
    unittest.main()
