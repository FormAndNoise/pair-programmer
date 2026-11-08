"""Tests for WebEventBridge and WebSocket event streaming."""

import asyncio
import os
import unittest
from unittest import mock
from agent_core.events import AgentEvent, EventBus, EventType
from server.bridge import WebEventBridge


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
    """Verify FastAPI application factory and endpoints when FastAPI is available."""

    def setUp(self):
        self.auth_env = mock.patch.dict(os.environ, {"PAIR_PROGRAMMER_TOKEN": "test-token"})
        self.auth_env.start()
        self.addCleanup(self.auth_env.stop)
        self.bus = EventBus()
        self.bridge = WebEventBridge(self.bus)

    def test_create_fastapi_app_without_engine(self):
        from server.bridge import create_fastapi_app
        app = create_fastapi_app(self.bridge, engine=None)
        # When fastapi is not installed, it returns None gracefully
        # When installed, it returns a FastAPI app instance
        if app is not None:
            self.assertEqual(app.title, "Pair Programming Agent API")
            self.assertEqual(app.state.session_status, "ready")

    def test_fastapi_endpoints_via_testclient(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi or httpx not available in current environment")

        from server.bridge import create_fastapi_app

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

        # GET /api/status
        res = client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["session_status"], "ready")
        self.assertEqual(data["epic"], "Test Epic")

        # GET /api/events
        res = client.get("/api/events")
        self.assertEqual(res.status_code, 200)
        self.assertIn("events", res.json())

        # POST /api/operator/steer
        res = client.post("/api/operator/steer", json={"guidance": "Test steering"})
        self.assertEqual(res.status_code, 200)

        # POST /api/sprint/stop when not running raises 409
        res = client.post("/api/sprint/stop")
        self.assertEqual(res.status_code, 409)

        # POST /api/sprint/pause when not running raises 409
        res = client.post("/api/sprint/pause")
        self.assertEqual(res.status_code, 409)


if __name__ == "__main__":
    unittest.main()
