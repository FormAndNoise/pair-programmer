"""Core coding primitives operating safely within the sandbox."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Optional
from agent_core.types import ToolResult
from sandbox.runner import SandboxRunner


def read_file(
    runner: SandboxRunner,
    path: str,
    offset: int = 1,
    limit: Optional[int] = 500,
) -> ToolResult:
    """Read file content with line numbers and optional line-range limits."""
    try:
        resolved = runner.resolve_path(path)
        if not resolved.exists():
            return ToolResult(
                call_id="",
                name="read_file",
                output=f"Error: File '{path}' does not exist.",
                is_error=True,
            )
        if resolved.is_dir():
            return ToolResult(
                call_id="",
                name="read_file",
                output=f"Error: '{path}' is a directory, not a file. Use list_dir instead.",
                is_error=True,
            )

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        start_idx = max(0, offset - 1)
        end_idx = min(total_lines, start_idx + limit) if limit else total_lines

        selected_lines = lines[start_idx:end_idx]
        formatted_content = "".join(
            f"{i + start_idx + 1:4d} | {line}" for i, line in enumerate(selected_lines)
        )

        header = f"=== File: {path} (Showing lines {start_idx + 1}-{end_idx} of {total_lines}) ===\n"
        return ToolResult(
            call_id="",
            name="read_file",
            output=header + formatted_content,
            is_error=False,
        )

    except PermissionError as e:
        return ToolResult(call_id="", name="read_file", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id="",
            name="read_file",
            output=f"Unexpected read failure: {e}",
            is_error=True,
        )


def write_file(
    runner: SandboxRunner,
    path: str,
    content: str,
    overwrite: bool = True,
) -> ToolResult:
    """Write or overwrite file contents in the sandbox."""
    try:
        resolved = runner.resolve_path(path)
        if resolved.exists() and not overwrite:
            return ToolResult(
                call_id="",
                name="write_file",
                output=f"Error: File '{path}' already exists and overwrite is set to False.",
                is_error=True,
            )

        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)

        return ToolResult(
            call_id="",
            name="write_file",
            output=f"Successfully written {len(content)} characters to '{path}'.",
            is_error=False,
        )

    except PermissionError as e:
        return ToolResult(call_id="", name="write_file", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id="",
            name="write_file",
            output=f"Failed to write file '{path}': {e}",
            is_error=True,
        )


def patch_file(
    runner: SandboxRunner,
    path: str,
    target_snippet: str,
    replacement_snippet: str,
) -> ToolResult:
    """Safely replace a target code snippet with replacement code and return a diff."""
    try:
        resolved = runner.resolve_path(path)
        if not resolved.exists():
            return ToolResult(
                call_id="",
                name="patch_file",
                output=f"Error: Target file '{path}' does not exist.",
                is_error=True,
            )

        with open(resolved, "r", encoding="utf-8") as f:
            original = f.read()

        match_count = original.count(target_snippet)
        if match_count == 0:
            return ToolResult(
                call_id="",
                name="patch_file",
                output=(
                    f"Error: Target snippet not found in '{path}'. "
                    "Ensure exact whitespace and indentation match."
                ),
                is_error=True,
            )
        if match_count > 1:
            return ToolResult(
                call_id="",
                name="patch_file",
                output=(
                    f"Error: Target snippet found {match_count} times in '{path}'. "
                    "Include more surrounding context to make target uniquely identifiable."
                ),
                is_error=True,
            )

        updated = original.replace(target_snippet, replacement_snippet, 1)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(updated)

        # Generate readable diff
        diff = list(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        diff_text = "".join(diff) or "[No visible line changes]"

        return ToolResult(
            call_id="",
            name="patch_file",
            output=f"Successfully patched '{path}'.\n\nUnified Diff:\n{diff_text}",
            is_error=False,
        )

    except PermissionError as e:
        return ToolResult(call_id="", name="patch_file", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id="",
            name="patch_file",
            output=f"Failed to patch file '{path}': {e}",
            is_error=True,
        )


def list_dir(
    runner: SandboxRunner,
    path: str = ".",
    recursive: bool = False,
    max_depth: int = 2,
) -> ToolResult:
    """List files and subdirectories within a sandbox folder."""
    try:
        resolved = runner.resolve_path(path)
        if not resolved.exists():
            return ToolResult(
                call_id="",
                name="list_dir",
                output=f"Error: Directory '{path}' does not exist.",
                is_error=True,
            )
        if not resolved.is_dir():
            return ToolResult(
                call_id="",
                name="list_dir",
                output=f"Error: '{path}' is a file, not a directory.",
                is_error=True,
            )

        entries = []
        if not recursive:
            for item in sorted(resolved.iterdir()):
                rel = item.relative_to(runner.workspace_dir)
                kind = "DIR " if item.is_dir() else "FILE"
                size = f"{item.stat().st_size:8d} B" if item.is_file() else "         "
                entries.append(f"[{kind}] {size}  {rel}")
        else:
            for root, dirs, files in os.walk(resolved):
                root_path = Path(root)
                rel_root = root_path.relative_to(resolved)
                depth = len(rel_root.parts)
                if depth >= max_depth:
                    dirs.clear()  # Do not recurse deeper
                for d in sorted(dirs):
                    rel = (root_path / d).relative_to(runner.workspace_dir)
                    entries.append(f"[DIR ]            {rel}")
                for f in sorted(files):
                    p = root_path / f
                    rel = p.relative_to(runner.workspace_dir)
                    entries.append(f"[FILE] {p.stat().st_size:8d} B  {rel}")

        output = f"=== Directory: {path} ({len(entries)} items) ===\n" + "\n".join(entries)
        return ToolResult(call_id="", name="list_dir", output=output, is_error=False)

    except PermissionError as e:
        return ToolResult(call_id="", name="list_dir", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id="",
            name="list_dir",
            output=f"Failed to list directory '{path}': {e}",
            is_error=True,
        )


def run_command(
    runner: SandboxRunner,
    command: str,
    timeout_seconds: Optional[int] = None,
) -> ToolResult:
    """Run a terminal command inside the sandbox."""
    exec_res = runner.run_command(command, timeout_seconds=timeout_seconds)
    formatted = exec_res.format_output()
    return ToolResult(
        call_id="",
        name="run_command",
        output=formatted,
        is_error=not exec_res.is_success,
        execution_time_ms=exec_res.duration_ms,
    )
