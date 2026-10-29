"""Tool registry and dispatcher for LLM function calling."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional
from agent_core.types import ToolDefinition, ToolResult
from sandbox.runner import SandboxRunner
from . import primitives


class ToolRegistry:
    """Stores available tools, generates provider schemas, and dispatches calls."""

    def __init__(self, runner: SandboxRunner) -> None:
        self.runner = runner
        self._tools: Dict[str, Callable[..., ToolResult]] = {}
        self._definitions: Dict[str, ToolDefinition] = {}

        # Auto-register core primitives
        self._register_default_tools()

    def register(
        self,
        name: str,
        description: str,
        parameters_schema: Dict[str, Any],
        handler: Callable[..., ToolResult],
    ) -> None:
        """Register a tool handler function and its JSON schema."""
        self._tools[name] = handler
        self._definitions[name] = ToolDefinition(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
        )

    def get_definitions(self) -> List[ToolDefinition]:
        """Return all registered tool definitions for provider payload injection."""
        return list(self._definitions.values())

    def dispatch(
        self,
        name: str,
        arguments: Dict[str, Any],
        call_id: str = "",
    ) -> ToolResult:
        """Execute a tool by name with provided arguments."""
        if name not in self._tools:
            return ToolResult(
                call_id=call_id,
                name=name,
                output=f"Error: Tool '{name}' is not recognized. Available tools: {list(self._tools.keys())}",
                is_error=True,
            )

        handler = self._tools[name]
        try:
            # Bind sandbox runner if first parameter expects it
            sig = inspect.signature(handler)
            first_param = next(iter(sig.parameters.values()), None)
            if first_param and (
                first_param.name == "runner"
                or first_param.annotation is SandboxRunner
            ):
                result = handler(self.runner, **arguments)
            else:
                result = handler(**arguments)

            result.call_id = call_id
            result.name = name
            return result

        except TypeError as e:
            return ToolResult(
                call_id=call_id,
                name=name,
                output=f"Invalid arguments for tool '{name}': {e}",
                is_error=True,
            )
        except Exception as e:
            return ToolResult(
                call_id=call_id,
                name=name,
                output=f"Execution exception in tool '{name}': {e}",
                is_error=True,
            )

    def _register_default_tools(self) -> None:
        """Register standard file and execution primitives."""
        self.register(
            name="read_file",
            description="Read file contents from the sandbox with line numbers and optional line-range offsets.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file in workspace."},
                    "offset": {"type": "integer", "description": "1-based starting line number (default: 1).", "default": 1},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read.", "default": 500},
                },
                "required": ["path"],
            },
            handler=primitives.read_file,
        )

        self.register(
            name="write_file",
            description="Create or overwrite a file in the sandbox workspace.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file in workspace."},
                    "content": {"type": "string", "description": "Full file content to write."},
                    "overwrite": {"type": "boolean", "description": "Whether to overwrite existing files (default: true).", "default": True},
                },
                "required": ["path", "content"],
            },
            handler=primitives.write_file,
        )

        self.register(
            name="patch_file",
            description="Replace an exact code snippet in a file with replacement code and generate a diff.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file in workspace."},
                    "target_snippet": {"type": "string", "description": "Exact text chunk to replace."},
                    "replacement_snippet": {"type": "string", "description": "New text chunk to replace it with."},
                },
                "required": ["path", "target_snippet", "replacement_snippet"],
            },
            handler=primitives.patch_file,
        )

        self.register(
            name="list_dir",
            description="List files and folders within a workspace directory.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (default: '.').", "default": "."},
                    "recursive": {"type": "boolean", "description": "Whether to list subdirectories recursively.", "default": False},
                },
                "required": [],
            },
            handler=primitives.list_dir,
        )

        self.register(
            name="run_command",
            description="Execute a terminal command (test runner, compiler, linter) in the sandbox.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command line to execute."},
                    "timeout_seconds": {"type": "integer", "description": "Optional command timeout in seconds."},
                },
                "required": ["command"],
            },
            handler=primitives.run_command,
        )
