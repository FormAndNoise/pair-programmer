"""Core coding primitives operating safely within the sandbox."""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Optional
from agent_core.types import ToolResult
from sandbox.runner import SandboxRunner

MAX_WRITE_BYTES = 1_000_000       # 1 MB content limit
MAX_READ_BYTES = 2_000_000        # 2 MB content limit
MAX_LIST_DIR_ENTRIES = 1000       # Max entries returned by list_dir
MAX_TOOL_OUTPUT_CHARS = 85_000    # Keep well below ToolResult.output 100k limit


def _resolve_call_id(call_id: Optional[str]) -> str:
    cleaned = (call_id or "").strip()
    return cleaned if cleaned else f"call_{uuid.uuid4().hex}"


def _reject_symlink(runner: SandboxRunner, target_path: str) -> None:
    """Raise PermissionError if target_path or any intermediate parent is a symlink."""
    if "\x00" in str(target_path):
        raise PermissionError("Invalid path: NUL byte detected.")
    raw = Path(target_path)
    candidate = raw if raw.is_absolute() else (runner.workspace_dir / raw)

    # Walk from workspace_dir down to candidate, checking lstat on every existing component
    try:
        rel_parts = candidate.absolute().relative_to(runner.workspace_dir).parts
    except ValueError:
        # Let runner.resolve_path raise the canonical Path traversal PermissionError
        runner.resolve_path(target_path)
        rel_parts = ()

    cur = runner.workspace_dir
    if cur.is_symlink():
        raise PermissionError("Sandbox workspace root must not be a symbolic link.")
    for part in rel_parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or cur.is_symlink():
            raise PermissionError(
                f"Symlink access rejected: '{target_path}' traverses symbolic link '{cur.name}'."
            )

    if candidate.is_symlink():
        raise PermissionError(
            f"Symlink access rejected: '{target_path}' is a symbolic link."
        )


def _verify_parent_dir_safe(runner: SandboxRunner, parent: Path, target_path: str) -> None:
    """Verify parent directory is inside workspace and not a symlink before/after mkdir."""
    _reject_symlink(runner, target_path)
    st_l = os.lstat(parent)
    st_s = os.stat(parent)
    if stat.S_ISLNK(st_l.st_mode) or not os.path.samestat(st_l, st_s):
        raise PermissionError(
            f"Symlink traversal rejected: parent directory of '{target_path}' is a symbolic link."
        )
    parent.resolve().relative_to(runner.workspace_dir)


def _read_workspace_file_safe(runner: SandboxRunner, resolved: Path, raw_path: str) -> tuple[str, int]:
    """Open and read a workspace file using O_NOFOLLOW (when available) and inode verification."""
    _reject_symlink(runner, raw_path)
    st_before = os.lstat(resolved)
    if stat.S_ISLNK(st_before.st_mode):
        raise PermissionError(f"Symlink access rejected: '{raw_path}' is a symbolic link.")
    if stat.S_ISDIR(st_before.st_mode):
        raise IsADirectoryError(raw_path)
    if st_before.st_size > MAX_READ_BYTES:
        raise ValueError(
            f"Error: File '{raw_path}' size ({st_before.st_size} bytes) exceeds limit of {MAX_READ_BYTES} bytes."
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(resolved, flags)
    try:
        st_fd = os.fstat(fd)
        st_after = os.lstat(resolved)
        if (
            stat.S_ISLNK(st_after.st_mode)
            or not os.path.samestat(st_before, st_fd)
            or not os.path.samestat(st_fd, st_after)
        ):
            raise PermissionError(
                f"TOCTOU / symlink swap detected while opening '{raw_path}'."
            )
        _reject_symlink(runner, raw_path)
        resolved.resolve().relative_to(runner.workspace_dir)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace", closefd=False) as f:
            content = f.read(MAX_READ_BYTES + 1)
        if len(content.encode("utf-8", errors="replace")) > MAX_READ_BYTES:
            raise ValueError(
                f"Error: File '{raw_path}' exceeds maximum size of {MAX_READ_BYTES} bytes."
            )
        return content, st_fd.st_size
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def read_file(
    runner: SandboxRunner,
    path: str,
    offset: int = 1,
    limit: Optional[int] = 500,
    call_id: Optional[str] = None,
) -> ToolResult:
    """Read file content with line numbers and optional line-range limits."""
    cid = _resolve_call_id(call_id)
    try:
        if offset < 0 or (limit is not None and limit < 0):
            return ToolResult(
                call_id=cid,
                name="read_file",
                output="Error: offset and limit must be zero or greater.",
                is_error=True,
            )
        _reject_symlink(runner, path)
        resolved = runner.resolve_path(path)
        if not resolved.exists():
            return ToolResult(
                call_id=cid,
                name="read_file",
                output=f"Error: File '{path}' does not exist.",
                is_error=True,
            )
        if resolved.is_dir():
            return ToolResult(
                call_id=cid,
                name="read_file",
                output=f"Error: '{path}' is a directory, not a file. Use list_dir instead.",
                is_error=True,
            )

        try:
            raw_text, _ = _read_workspace_file_safe(runner, resolved, path)
        except ValueError as ve:
            return ToolResult(call_id=cid, name="read_file", output=str(ve), is_error=True)

        lines = raw_text.splitlines(keepends=True)

        total_lines = len(lines)
        start_idx = max(0, offset - 1)
        end_idx = total_lines if limit is None else min(total_lines, start_idx + limit)

        selected_lines = lines[start_idx:end_idx]
        formatted_content = "".join(
            f"{i + start_idx + 1:4d} | {line}" for i, line in enumerate(selected_lines)
        )

        header = f"=== File: {path} (Showing lines {start_idx + 1}-{end_idx} of {total_lines}) ===\n"
        return ToolResult(
            call_id=cid,
            name="read_file",
            output=header + formatted_content,
            is_error=False,
        )

    except PermissionError as e:
        return ToolResult(call_id=cid, name="read_file", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id=cid,
            name="read_file",
            output=f"Unexpected read failure: {e}",
            is_error=True,
        )


def write_file(
    runner: SandboxRunner,
    path: str,
    content: str,
    overwrite: bool = True,
    call_id: Optional[str] = None,
) -> ToolResult:
    """Write or overwrite file contents in the sandbox atomically."""
    cid = _resolve_call_id(call_id)
    try:
        if "\x00" in content:
            return ToolResult(
                call_id=cid,
                name="write_file",
                output="Error: write_file only supports text; NUL byte detected in content.",
                is_error=True,
            )

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_WRITE_BYTES:
            return ToolResult(
                call_id=cid,
                name="write_file",
                output=f"Error: Content length ({len(content_bytes)} bytes) exceeds maximum limit of {MAX_WRITE_BYTES} bytes.",
                is_error=True,
            )

        _reject_symlink(runner, path)
        resolved = runner.resolve_path(path)
        if resolved.exists() and not overwrite:
            return ToolResult(
                call_id=cid,
                name="write_file",
                output=f"Error: File '{path}' already exists and overwrite is set to False.",
                is_error=True,
            )

        resolved.parent.mkdir(parents=True, exist_ok=True)
        _verify_parent_dir_safe(runner, resolved.parent, path)
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=resolved.parent,
                mode="w",
                encoding="utf-8",
                delete=False,
                prefix=".tmp_write_",
            ) as f:
                tmp_path = Path(f.name)
                tmp_path.resolve().relative_to(runner.workspace_dir)
                f.write(content)
            _verify_parent_dir_safe(runner, resolved.parent, path)
            os.replace(tmp_path, resolved)
            tmp_path = None
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

        return ToolResult(
            call_id=cid,
            name="write_file",
            output=f"Successfully written {len(content_bytes)} bytes ({len(content)} characters) to '{path}'.",
            is_error=False,
        )

    except PermissionError as e:
        return ToolResult(call_id=cid, name="write_file", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id=cid,
            name="write_file",
            output=f"Failed to write file '{path}': {e}",
            is_error=True,
        )


def patch_file(
    runner: SandboxRunner,
    path: str,
    target_snippet: str,
    replacement_snippet: str,
    call_id: Optional[str] = None,
) -> ToolResult:
    """Safely replace a target code snippet with replacement code and return a diff."""
    cid = _resolve_call_id(call_id)
    try:
        repl_bytes = replacement_snippet.encode("utf-8")
        if len(repl_bytes) > MAX_WRITE_BYTES:
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output=f"Error: Replacement snippet ({len(repl_bytes)} bytes) exceeds maximum limit of {MAX_WRITE_BYTES} bytes.",
                is_error=True,
            )

        if "\x00" in target_snippet or "\x00" in replacement_snippet:
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output="Error: patch_file only supports text; binary content detected in snippets.",
                is_error=True,
            )

        _reject_symlink(runner, path)
        resolved = runner.resolve_path(path)
        if not resolved.exists():
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output=f"Error: Target file '{path}' does not exist.",
                is_error=True,
            )

        try:
            original, _ = _read_workspace_file_safe(runner, resolved, path)
        except ValueError as ve:
            return ToolResult(call_id=cid, name="patch_file", output=str(ve), is_error=True)

        if "\x00" in original:
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output=f"Error: Cannot patch binary file '{path}'.",
                is_error=True,
            )

        match_count = original.count(target_snippet)
        if match_count == 0:
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output=(
                    f"Error: Target snippet not found in '{path}'. "
                    "Ensure exact whitespace and indentation match."
                ),
                is_error=True,
            )
        if match_count > 1:
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output=(
                    f"Error: Target snippet found {match_count} times in '{path}'. "
                    "Include more surrounding context to make target uniquely identifiable."
                ),
                is_error=True,
            )

        updated = original.replace(target_snippet, replacement_snippet, 1)
        updated_bytes = updated.encode("utf-8")
        if len(updated_bytes) > MAX_WRITE_BYTES:
            return ToolResult(
                call_id=cid,
                name="patch_file",
                output=(
                    f"Error: Patched file size ({len(updated_bytes)} bytes) exceeds "
                    f"maximum limit of {MAX_WRITE_BYTES} bytes."
                ),
                is_error=True,
            )

        resolved.parent.mkdir(parents=True, exist_ok=True)
        _verify_parent_dir_safe(runner, resolved.parent, path)
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=resolved.parent,
                mode="w",
                encoding="utf-8",
                delete=False,
                prefix=".tmp_patch_",
            ) as f:
                tmp_path = Path(f.name)
                tmp_path.resolve().relative_to(runner.workspace_dir)
                f.write(updated)
            _verify_parent_dir_safe(runner, resolved.parent, path)
            os.replace(tmp_path, resolved)
            tmp_path = None
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

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
        if len(diff_text) > MAX_TOOL_OUTPUT_CHARS:
            diff_text = diff_text[:MAX_TOOL_OUTPUT_CHARS] + "\n... [DIFF TRUNCATED] ..."

        return ToolResult(
            call_id=cid,
            name="patch_file",
            output=f"Successfully patched '{path}' ({len(updated_bytes)} bytes).\n\nUnified Diff:\n{diff_text}",
            is_error=False,
        )

    except PermissionError as e:
        return ToolResult(call_id=cid, name="patch_file", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id=cid,
            name="patch_file",
            output=f"Failed to patch file '{path}': {e}",
            is_error=True,
        )


def list_dir(
    runner: SandboxRunner,
    path: str = ".",
    recursive: bool = False,
    max_depth: int = 2,
    call_id: Optional[str] = None,
) -> ToolResult:
    """List files and subdirectories within a sandbox folder."""
    cid = _resolve_call_id(call_id)
    try:
        _reject_symlink(runner, path)
        resolved = runner.resolve_path(path)
        if not resolved.exists():
            return ToolResult(
                call_id=cid,
                name="list_dir",
                output=f"Error: Directory '{path}' does not exist.",
                is_error=True,
            )
        if not resolved.is_dir():
            return ToolResult(
                call_id=cid,
                name="list_dir",
                output=f"Error: '{path}' is a file, not a directory.",
                is_error=True,
            )

        entries = []
        truncated = False
        if not recursive:
            with os.scandir(resolved) as it:
                raw_items = sorted(it, key=lambda e: e.name)
            for entry in raw_items:
                if entry.is_symlink():
                    continue
                if len(entries) >= MAX_LIST_DIR_ENTRIES:
                    truncated = True
                    break
                try:
                    item_path = Path(entry.path)
                    rel = item_path.relative_to(runner.workspace_dir)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    kind = "DIR " if is_dir else "FILE"
                    size = (
                        f"{entry.stat(follow_symlinks=False).st_size:8d} B"
                        if (not is_dir and entry.is_file(follow_symlinks=False))
                        else "         "
                    )
                    entries.append(f"[{kind}] {size}  {rel}")
                except OSError:
                    continue
        else:
            for root, dirs, files in os.walk(resolved, followlinks=False):
                root_path = Path(root)
                rel_root = root_path.relative_to(resolved)
                depth = len(rel_root.parts)
                # Filter out symlinked directories so they are neither listed nor traversed
                dirs[:] = sorted(d for d in dirs if not (root_path / d).is_symlink())
                if depth >= max_depth:
                    dirs.clear()  # Do not recurse deeper
                for d in dirs:
                    if len(entries) >= MAX_LIST_DIR_ENTRIES:
                        truncated = True
                        break
                    rel = (root_path / d).relative_to(runner.workspace_dir)
                    entries.append(f"[DIR ]            {rel}")
                if truncated:
                    break
                for f in sorted(files):
                    p = root_path / f
                    if p.is_symlink():
                        continue
                    if len(entries) >= MAX_LIST_DIR_ENTRIES:
                        truncated = True
                        break
                    try:
                        rel = p.relative_to(runner.workspace_dir)
                        st = os.lstat(p)
                        entries.append(f"[FILE] {st.st_size:8d} B  {rel}")
                    except OSError:
                        continue
                if truncated:
                    break

        body = "\n".join(entries)
        if truncated:
            body += f"\n... [TRUNCATED: listing capped at {MAX_LIST_DIR_ENTRIES} entries] ..."
        if len(body) > MAX_TOOL_OUTPUT_CHARS:
            body = body[:MAX_TOOL_OUTPUT_CHARS] + "\n... [TRUNCATED: output size limit reached] ..."
        output = f"=== Directory: {path} ({len(entries)} items shown) ===\n" + body
        return ToolResult(call_id=cid, name="list_dir", output=output, is_error=False)

    except PermissionError as e:
        return ToolResult(call_id=cid, name="list_dir", output=str(e), is_error=True)
    except Exception as e:
        return ToolResult(
            call_id=cid,
            name="list_dir",
            output=f"Failed to list directory '{path}': {e}",
            is_error=True,
        )


def run_command(
    runner: SandboxRunner,
    command: str,
    timeout_seconds: Optional[int] = None,
    call_id: Optional[str] = None,
) -> ToolResult:
    """Run a terminal command inside the sandbox."""
    cid = _resolve_call_id(call_id)
    try:
        effective_timeout = (
            timeout_seconds
            if (timeout_seconds is not None and timeout_seconds > 0)
            else getattr(runner, "default_timeout", getattr(runner, "timeout_seconds", 60))
        )
        exec_res = runner.run_command(command, timeout_seconds=effective_timeout)
        formatted = exec_res.format_output()
        return ToolResult(
            call_id=cid,
            name="run_command",
            output=formatted,
            is_error=not exec_res.is_success,
            execution_time_ms=exec_res.duration_ms,
        )
    except Exception as e:
        return ToolResult(
            call_id=cid,
            name="run_command",
            output=f"Command execution error: {e}",
            is_error=True,
        )
