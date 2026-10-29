"""Tests for Model Gateway and ScriptedMockGateway."""

import unittest
from agent_core.types import Message, Role, ToolCall, ToolDefinition
from gateway.client import ModelGateway
from gateway.mock import ScriptedMockGateway


class TestGateway(unittest.TestCase):
    def test_mock_gateway_queue_and_dispatch(self):
        mock_gw = ScriptedMockGateway()

        # Queue responses for two different models
        mock_gw.queue_text_response(
            model="claude-3-5",
            content="I am Model A (Claude). I wrote the failing tests.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "tests/test_foo.py", "content": "def test_foo(): assert False"})],
        )

        mock_gw.queue_text_response(
            model="gemini-2-5",
            content="I am Model B (Gemini). I will implement the feature.",
            tool_calls=[ToolCall(name="write_file", arguments={"path": "src/foo.py", "content": "def foo(): return True"})],
        )

        # Call Model A
        res_a = mock_gw.send_chat(
            model="claude-3-5",
            messages=[Message(role=Role.USER, content="Start story 1")],
        )
        self.assertEqual(res_a.model_name, "claude-3-5")
        self.assertIn("Model A", res_a.content)
        self.assertEqual(len(res_a.tool_calls), 1)
        self.assertEqual(res_a.tool_calls[0].arguments["path"], "tests/test_foo.py")

        # Call Model B
        res_b = mock_gw.send_chat(
            model="gemini-2-5",
            messages=[Message(role=Role.USER, content="Turn tests green")],
        )
        self.assertEqual(res_b.model_name, "gemini-2-5")
        self.assertIn("Model B", res_b.content)
        self.assertEqual(len(res_b.tool_calls), 1)
        self.assertEqual(res_b.tool_calls[0].arguments["path"], "src/foo.py")

        # Verify call history recorded
        self.assertEqual(len(mock_gw.call_history), 2)

    def test_mock_gateway_empty_queue_fallback(self):
        mock_gw = ScriptedMockGateway()
        res = mock_gw.send_chat(model="gpt-4o", messages=[])
        self.assertIn("[Mock Default]", res.content)


if __name__ == "__main__":
    unittest.main()
