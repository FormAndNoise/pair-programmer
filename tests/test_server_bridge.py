"""Tests for WebEventBridge and WebSocket event streaming."""

import asyncio
import unittest
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


if __name__ == "__main__":
    unittest.main()
