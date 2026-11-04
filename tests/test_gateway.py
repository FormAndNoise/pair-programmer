"""Tests for Model Gateway and ScriptedMockGateway."""

import os
import unittest
from unittest import mock
from agent_core.types import Message, Role, ToolCall, ToolDefinition
import gateway.client as gateway_module
from gateway.client import ModelGateway
from gateway.mock import ScriptedMockGateway


ANTHROPIC_RESPONSE = {
    "content": [
        {"type": "text", "text": "I wrote the failing tests."},
        {"type": "tool_use", "id": "toolu_01", "name": "write_file", "input": {"path": "test_foo.py", "content": "def test_foo(): pass"}},
    ]
}

GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {"text": "Implemented the feature."},
                    {"functionCall": {"name": "write_file", "args": {"path": "foo.py", "content": "def foo(): return 1"}}},
                ]
            }
        }
    ]
}


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeHttpClient:
    def __init__(self, response):
        self._response = response
        self.captured_request = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.captured_request = {"url": url, "headers": headers, "json": json}
        return self._response


class TestDirectAnthropicRoute(unittest.TestCase):
    def setUp(self):
        self.gateway = ModelGateway(max_retries=1)
        self.messages = [Message(role=Role.USER, content="Write tests for story 1.")]

    def test_anthropic_request_format_and_response_parsing(self):
        fake_client = _FakeHttpClient(_FakeResponse(ANTHROPIC_RESPONSE))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}), \
                mock.patch.object(gateway_module.httpx, "Client", return_value=fake_client):
            result = self.gateway._send_via_direct_api(
                model="anthropic/claude-3-5-sonnet-20241022",
                messages=self.messages,
            )

        request = fake_client.captured_request
        self.assertEqual(request["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(request["headers"]["x-api-key"], "test-anthropic-key")
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")

        body = request["json"]
        self.assertEqual(body["model"], "claude-3-5-sonnet-20241022")
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(body["messages"][0]["content"][0]["text"], "Write tests for story 1.")

        self.assertEqual(result.role, Role.ASSISTANT)
        self.assertEqual(result.content, "I wrote the failing tests.")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].id, "toolu_01")
        self.assertEqual(result.tool_calls[0].name, "write_file")
        self.assertEqual(result.tool_calls[0].arguments["path"], "test_foo.py")

    def test_anthropic_tools_and_system_prompt_serialization(self):
        fake_client = _FakeHttpClient(_FakeResponse(ANTHROPIC_RESPONSE))
        tool = ToolDefinition(
            name="read_file",
            description="Read a file.",
            parameters_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}), \
                mock.patch.object(gateway_module.httpx, "Client", return_value=fake_client):
            self.gateway._send_via_direct_api(
                model="anthropic/claude-3-5-sonnet-20241022",
                messages=self.messages,
                tools=[tool],
                system_prompt="You are the Test Author.",
            )

        body = fake_client.captured_request["json"]
        self.assertEqual(body["system"], "You are the Test Author.")
        self.assertEqual(body["tools"][0]["name"], "read_file")
        self.assertEqual(body["tools"][0]["input_schema"], tool.parameters_schema)


class TestDirectGeminiRoute(unittest.TestCase):
    def setUp(self):
        self.gateway = ModelGateway(max_retries=1)
        self.messages = [Message(role=Role.USER, content="Turn tests green.")]

    def test_gemini_request_format_and_response_parsing(self):
        fake_client = _FakeHttpClient(_FakeResponse(GEMINI_RESPONSE))
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-gemini-key"}), \
                mock.patch.object(gateway_module.httpx, "Client", return_value=fake_client):
            result = self.gateway._send_via_direct_api(
                model="gemini/gemini-2.5-flash",
                messages=self.messages,
            )

        request = fake_client.captured_request
        self.assertEqual(
            request["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(request["headers"]["x-goog-api-key"], "test-gemini-key")

        body = request["json"]
        self.assertEqual(body["contents"][0]["role"], "user")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "Turn tests green.")

        self.assertEqual(result.role, Role.ASSISTANT)
        self.assertEqual(result.content, "Implemented the feature.")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "write_file")
        self.assertEqual(result.tool_calls[0].arguments["path"], "foo.py")
        self.assertTrue(result.tool_calls[0].id.startswith("call_"))

    def test_gemini_tools_and_system_prompt_serialization(self):
        fake_client = _FakeHttpClient(_FakeResponse(GEMINI_RESPONSE))
        tool = ToolDefinition(
            name="list_dir",
            description="List a directory.",
            parameters_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key"}), \
                mock.patch.object(gateway_module.httpx, "Client", return_value=fake_client):
            self.gateway._send_via_direct_api(
                model="gemini/gemini-2.5-flash",
                messages=self.messages,
                tools=[tool],
                system_prompt="You are the Implementer.",
            )

        body = fake_client_captured(fake_client)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "You are the Implementer.")
        declarations = body["tools"][0]["functionDeclarations"]
        self.assertEqual(declarations[0]["name"], "list_dir")
        self.assertEqual(declarations[0]["parameters"], tool.parameters_schema)


def fake_client_captured(fake_client):
    return fake_client.captured_request["json"]


class TestDirectRouting(unittest.TestCase):
    def test_openai_provider_still_uses_openai_route(self):
        gateway = ModelGateway(max_retries=1)
        fake_client = _FakeHttpClient(
            _FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        )
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}), \
                mock.patch.object(gateway_module.httpx, "Client", return_value=fake_client):
            result = gateway._send_via_direct_api(
                model="openai/gpt-4o",
                messages=[Message(role=Role.USER, content="Hi")],
            )

        request = fake_client.captured_request
        self.assertEqual(request["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-openai-key")
        self.assertEqual(result.content, "ok")

    def test_openrouter_provider_uses_openrouter_key_and_url(self):
        gateway = ModelGateway(max_retries=1)
        fake_client = _FakeHttpClient(
            _FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-test"
            with mock.patch.object(gateway_module.httpx, "Client", return_value=fake_client):
                gateway._send_via_direct_api(
                    model="openrouter/anthropic/claude-3.5-sonnet",
                    messages=[Message(role=Role.USER, content="Hi")],
                )

        request = fake_client.captured_request
        self.assertEqual(request["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-or-v1-test")
        self.assertEqual(request["json"]["model"], "anthropic/claude-3.5-sonnet")


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
