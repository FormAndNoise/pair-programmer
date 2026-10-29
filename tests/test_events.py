"""Tests for EventBus and event delivery."""

import unittest
from agent_core.events import AgentEvent, EventBus, EventType


class TestEventBus(unittest.TestCase):
    def test_event_bus_subscription_and_publishing(self):
        bus = EventBus()
        received_events = []

        def on_story_start(event: AgentEvent):
            received_events.append(event)

        bus.subscribe(EventType.STORY_START, on_story_start)

        evt = AgentEvent(
            event_type=EventType.STORY_START,
            source_model="claude-3-5",
            payload={"story_id": "US-01", "title": "Create CLI"},
        )
        bus.publish(evt)

        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].payload["story_id"], "US-01")
        self.assertEqual(len(bus.get_history()), 1)

    def test_event_bus_wildcard_listener(self):
        bus = EventBus()
        timeline = []

        bus.subscribe_all(lambda e: timeline.append(e.event_type))

        bus.publish(AgentEvent(event_type=EventType.SPRINT_START))
        bus.publish(AgentEvent(event_type=EventType.STORY_START))
        bus.publish(AgentEvent(event_type=EventType.TOOL_START))

        self.assertEqual(
            timeline,
            [
                EventType.SPRINT_START,
                EventType.STORY_START,
                EventType.TOOL_START,
            ],
        )


if __name__ == "__main__":
    unittest.main()
