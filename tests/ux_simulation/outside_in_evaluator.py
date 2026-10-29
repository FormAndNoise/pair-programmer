"""Standalone Outside-In UX Evaluator simulating end-user interactions in the sandbox."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from agent_core.types import UserStory
from sandbox.runner import ExecutionResult, SandboxRunner


@dataclass
class UXScorecard:
    """Quantitative and qualitative scorecard of user experience evaluation."""
    story_id: str
    story_title: str
    contract: str
    passed: bool
    exit_code: int
    duration_ms: float
    output_summary: str
    no_uncaught_tracebacks: bool
    feedback: str


class OutsideInUXEvaluator:
    """Simulates real human interaction against generated sandbox artifacts."""

    def __init__(self, runner: SandboxRunner) -> None:
        self.runner = runner

    def evaluate_story_contract(self, story: UserStory) -> UXScorecard:
        """Evaluate whether the story's human interaction contract holds in reality."""
        contract = story.human_interaction_contract.strip()
        start_time = time.perf_counter()

        # 1. Check if contract describes a Python function invocation:
        # e.g.: `func(args)` returns `expected`
        py_match = re.search(r"`([a-zA-Z0-9_]+)\((.*?)\)`\s*returns\s*`?(.*?)`?\.?$", contract)
        if py_match:
            func_name = py_match.group(1)
            args_str = py_match.group(2)
            expected_raw = py_match.group(3).strip().rstrip(".")
            return self._evaluate_python_contract(
                story=story,
                func_name=func_name,
                args_str=args_str,
                expected_raw=expected_raw,
                start_time=start_time,
            )

        # 2. Check if contract describes a CLI shell command:
        # e.g.: `agent --help` or Running `command`
        cli_match = re.search(r"`([^`]+)`", contract)
        if cli_match:
            cmd = cli_match.group(1)
            return self._evaluate_cli_contract(
                story=story,
                cmd=cmd,
                start_time=start_time,
            )

        # 3. Fallback: Run compile and test check
        exec_res = self.runner.run_command("python -m unittest")
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        no_tracebacks = "Traceback (most recent call last)" not in exec_res.stderr

        return UXScorecard(
            story_id=story.id,
            story_title=story.title,
            contract=contract,
            passed=exec_res.is_success,
            exit_code=exec_res.exit_code,
            duration_ms=round(duration_ms, 2),
            output_summary=exec_res.format_output()[:200],
            no_uncaught_tracebacks=no_tracebacks,
            feedback="Fallback test suite executed." if exec_res.is_success else "Test suite failed.",
        )

    def _evaluate_python_contract(
        self,
        story: UserStory,
        func_name: str,
        args_str: str,
        expected_raw: str,
        start_time: float,
    ) -> UXScorecard:
        """Dynamically discover which sandbox module defines func_name and execute contract."""
        module_name = self._find_module_with_function(func_name)
        if not module_name:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return UXScorecard(
                story_id=story.id,
                story_title=story.title,
                contract=story.human_interaction_contract,
                passed=False,
                exit_code=-1,
                duration_ms=round(duration_ms, 2),
                output_summary=f"Function '{func_name}' not found in any workspace module.",
                no_uncaught_tracebacks=True,
                feedback=f"UX Barrier: User cannot import '{func_name}' from sandbox modules.",
            )

        # Execute test runner harness in sandbox
        py_code = (
            f"import sys; "
            f"from {module_name} import {func_name}; "
            f"actual = {func_name}({args_str}); "
            f"expected = {expected_raw}; "
            f"print('ACTUAL:', repr(actual)); "
            f"assert actual == expected, f'Expected {{expected}}, got {{actual}}'; "
        )

        res = self.runner.run_command(f'python -c "{py_code}"')
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        no_tracebacks = "Traceback (most recent call last)" not in res.stderr
        passed = res.is_success

        feedback = (
            f"Contract verified: `{func_name}({args_str})` evaluated to `{expected_raw}` correctly."
            if passed
            else f"UX Contract Violation: Execution failed with exit code {res.exit_code}."
        )

        return UXScorecard(
            story_id=story.id,
            story_title=story.title,
            contract=story.human_interaction_contract,
            passed=passed,
            exit_code=res.exit_code,
            duration_ms=round(duration_ms, 2),
            output_summary=res.format_output()[:300],
            no_uncaught_tracebacks=no_tracebacks,
            feedback=feedback,
        )

    def _evaluate_cli_contract(
        self,
        story: UserStory,
        cmd: str,
        start_time: float,
    ) -> UXScorecard:
        """Run terminal command in sandbox and evaluate usability signals."""
        res = self.runner.run_command(cmd)
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        no_tracebacks = "Traceback (most recent call last)" not in res.stderr

        passed = res.is_success and no_tracebacks
        feedback = (
            f"CLI interaction `{cmd}` completed successfully with clean output."
            if passed
            else f"CLI interaction `{cmd}` returned error or crash traceback."
        )

        return UXScorecard(
            story_id=story.id,
            story_title=story.title,
            contract=story.human_interaction_contract,
            passed=passed,
            exit_code=res.exit_code,
            duration_ms=round(duration_ms, 2),
            output_summary=res.format_output()[:300],
            no_uncaught_tracebacks=no_tracebacks,
            feedback=feedback,
        )

    def _find_module_with_function(self, func_name: str) -> Optional[str]:
        """Search python files in sandbox workspace for 'def func_name'."""
        pattern = re.compile(rf"def\s+{re.escape(func_name)}\s*\(")
        for py_file in self.runner.workspace_dir.rglob("*.py"):
            if py_file.name.startswith("test_"):
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if pattern.search(content):
                    rel_path = py_file.relative_to(self.runner.workspace_dir)
                    # Convert path to module name: e.g. src/utils.py -> src.utils
                    mod_parts = list(rel_path.with_suffix("").parts)
                    return ".".join(mod_parts)
            except Exception:
                continue
        return None
