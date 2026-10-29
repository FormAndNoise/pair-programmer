"""Automated desktop simulation of operator steering, pause, and resume workflows."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from agent_core.events import AgentEvent, EventBus, EventType
from agent_core.types import AgentConfig, ToolCall, UserStory
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.runner import SandboxRunner
from server.bridge import WebEventBridge
from tools.registry import ToolRegistry


class TestOperatorSteeringSimulation(unittest.TestCase):
    """Simulates real-time operator intervention during an active coding sprint."""

    def setUp(self) -> None:
        self.bus = EventBus()
        self.bridge = WebEventBridge(self.bus)

    def test_operator_guidance_injection_lifecycle(self) -> None:
        """Simulate an operator typing steering guidance into the active session."""
        steering_events = []
        self.bus.subscribe(
            EventType.OPERATOR_STEER,
            lambda e: steering_events.append(e.payload.get("guidance")),
        )

        client_queue = self.bridge.register_client()

        # Operator submits steering input
        guidance_text = "Ensure all error feedback formats as JSON with exit code 2."
        self.bridge.handle_operator_steer(guidance_text)

        # 1. Bus received guidance
        self.assertEqual(len(steering_events), 1)
        self.assertEqual(steering_events[0], guidance_text)

        # 2. Web bridge forwarded to connected operator client
        self.assertFalse(client_queue.empty())
        event_dict = client_queue.get_nowait()
        self.assertEqual(event_dict["type"], "operator:steer")
        self.assertEqual(event_dict["payload"]["guidance"], guidance_text)

    def test_operator_pause_and_resume_cycle(self) -> None:
        """Simulate operator hitting Space/Pause and then Resuming."""
        received_actions = []
        self.bus.subscribe(
            EventType.OPERATOR_PAUSE,
            lambda e: received_actions.append("PAUSE"),
        )
        self.bus.subscribe(
            EventType.OPERATOR_RESUME,
            lambda e: received_actions.append("RESUME"),
        )

        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_PAUSE))
        self.bus.publish(AgentEvent(event_type=EventType.OPERATOR_RESUME))

        self.assertEqual(received_actions, ["PAUSE", "RESUME"])

    def test_engine_incorporates_steering_guidance_into_prompt(self) -> None:
        """Verify that operator guidance dynamically augments the implementer prompt."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            runner = SandboxRunner(workspace)
            registry = ToolRegistry(runner)
            gateway = ScriptedMockGateway()
            config = AgentConfig(model_a="claude-3-5", model_b="gemini-2-5")

            backlog = BacklogManager.from_epic("Steering Test")
            story = backlog.add_story(
                title="Json Output Feature",
                human_interaction_contract="`format_data()` returns json string.",
            )

            engine = PairProgrammingEngine(
                config=config,
                backlog_mgr=backlog,
                runner=runner,
                registry=registry,
                gateway=gateway,
                bus=self.bus,
            )

            # 1. Queue Model A (Test Author)
            gateway.queue_text_response(
                model="claude-3-5",
                content="Writing test.",
                tool_calls=[ToolCall(name="write_file", arguments={"path": "test_fn.py", "content": "import unittest\nclass T(unittest.TestCase):\n def test(self): pass\nif __name__ == '__main__': unittest.main()"})],
            )

            # Operator injects steering BEFORE or DURING Red Phase
            self.bridge.handle_operator_steer("CRITICAL: Include ISO-8601 timestamps in output.")

            # 2. Queue Model B (Implementer)
            gateway.queue_text_response(
                model="gemini-2-5",
                content="Implementing with timestamp.",
                tool_calls=[ToolCall(name="write_file", arguments={"path": "fn.py", "content": "def format_data(): return '{}'"})],
            )

            # 3. Queue Model A review
            gateway.queue_text_response(model="claude-3-5", content="LGTM")

            success = engine.execute_story_cycle(story)
            self.assertTrue(success)

            # Assert that gateway call history for Model B received the operator steering note!
            model_b_calls = [c for c in gateway.call_history if c["model"] == "gemini-2-5"]
            self.assertTrue(len(model_b_calls) > 0)
            sent_prompt = model_b_calls[0]["messages"][0].content
            self.assertIn("Operator Steering Guidance:", sent_prompt)
            self.assertIn("CRITICAL: Include ISO-8601 timestamps", sent_prompt)


if __name__ == "__main__":
    unittest.main()
