"""Tests for domain models and contracts in agent_core.types."""

import unittest
from agent_core.types import (
    AgentConfig,
    DefinitionOfDone,
    Message,
    PairRole,
    Role,
    SprintBacklog,
    StoryStatus,
    ToolCall,
    ToolResult,
    UserStory,
)


class TestAgentTypes(unittest.TestCase):
    def test_tool_call_and_result(self):
        tc = ToolCall(name="read_file", arguments={"path": "main.py"})
        self.assertEqual(tc.name, "read_file")
        self.assertEqual(tc.arguments["path"], "main.py")
        self.assertTrue(tc.id.startswith("call_"))

        tr = ToolResult(call_id=tc.id, name=tc.name, output="def main(): pass", is_error=False)
        self.assertEqual(tr.call_id, tc.id)
        self.assertFalse(tr.is_error)
        self.assertIn("def main", tr.output)

    def test_message_provider_format(self):
        msg = Message(
            role=Role.ASSISTANT,
            content="I will inspect the file.",
            tool_calls=[ToolCall(name="read_file", arguments={"path": "test.py"})],
            model_name="claude-3-5-sonnet",
            pair_role=PairRole.DRIVER,
        )
        payload = msg.to_provider_format()
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["content"], "I will inspect the file.")
        self.assertEqual(len(payload["tool_calls"]), 1)
        self.assertEqual(payload["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(payload["tool_calls"][0]["function"]["arguments"], '{"path": "test.py"}')

    def test_tool_role_message_provider_format_includes_call_id_and_output(self):
        result = ToolResult(call_id="call_abc123", name="read_file", output="1 | x = 1\n")
        msg = Message(role=Role.TOOL, content=result.output, tool_result=result)
        payload = msg.to_provider_format()
        self.assertEqual(
            payload,
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": "1 | x = 1\n",
            },
        )

    def test_definition_of_done_logic(self):
        dod = DefinitionOfDone()
        self.assertFalse(dod.is_complete)

        dod.syntax_valid = True
        dod.tests_passed = True
        dod.cross_review_approved = True
        self.assertFalse(dod.is_complete)  # UX verification still false

        dod.user_experience_verified = True
        self.assertTrue(dod.is_complete)

    def test_sprint_backlog_advancement(self):
        story1 = UserStory(
            title="CLI Help Flag",
            human_interaction_contract="Running `agent --help` prints human-friendly documentation.",
        )
        story2 = UserStory(
            title="Token Validation",
            human_interaction_contract="Invalid token reports actionable error message.",
        )

        backlog = SprintBacklog(
            epic_goal="Create Initial CLI Interface",
            stories=[story1, story2],
        )

        self.assertEqual(backlog.active_story.title, "CLI Help Flag")
        self.assertFalse(backlog.is_complete)

        story1.status = StoryStatus.DONE
        next_story = backlog.advance()
        self.assertIsNotNone(next_story)
        self.assertEqual(next_story.title, "Token Validation")
        self.assertEqual(backlog.active_story.title, "Token Validation")

        story2.status = StoryStatus.DONE
        for story in (story1, story2):
            story.dod = DefinitionOfDone(
                syntax_valid=True,
                tests_passed=True,
                cross_review_approved=True,
                user_experience_verified=True,
            )
        self.assertTrue(backlog.is_complete)


if __name__ == "__main__":
    unittest.main()
