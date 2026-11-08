"""Outside-In UX Evaluator simulating end-user interactions in the sandbox.

Moved from ``tests/ux_simulation/outside_in_evaluator.py`` so the pair
programming engine can run it as the real fourth Definition of Done gate
(the ``user_experience_verified`` field). The evaluator replays each story's
``human_interaction_contract`` against the artifacts the models wrote in the
sandbox and reports whether the human interaction actually holds.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional
from agent_core.types import UserStory
from sandbox.runner import SandboxRunner


def build_sandbox_python_env(runner: SandboxRunner) -> Dict[str, str]:
    """Build PYTHONPATH environment mapping supporting both src/ and flat layouts."""
    sep = ":" if hasattr(runner, "docker_binary") else os.pathsep
    paths = ["src", "."] if (runner.workspace_dir / "src").is_dir() else ["."]
    return {"PYTHONPATH": sep.join(paths)}


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

        # 2. Check if contract describes a safe CLI python invocation:
        # e.g.: `python agent.py --help`
        cli_match = re.search(r"`([^`]+)`", contract)
        if cli_match:
            cmd = cli_match.group(1).strip()
            if self._is_safe_cli_command(cmd):
                return self._evaluate_cli_contract(
                    story=story,
                    cmd=cmd,
                    start_time=start_time,
                )

        # 3. Fallback: Run compile and test check
        py_env = build_sandbox_python_env(self.runner)
        tests_dir = self.runner.workspace_dir / "tests"
        fallback_cmd = (
            "python -m unittest discover -s tests"
            if tests_dir.is_dir() and any(tests_dir.rglob("test*.py"))
            else "python -m unittest"
        )
        exec_res = self.runner.run_command(fallback_cmd, trusted_env=py_env)
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        no_tracebacks = "Traceback (most recent call last)" not in exec_res.stderr
        ran_any = "Ran 0 tests" not in f"{exec_res.stdout}\n{exec_res.stderr}"
        passed = exec_res.is_success and ran_any

        return UXScorecard(
            story_id=story.id,
            story_title=story.title,
            contract=contract,
            passed=passed,
            exit_code=exec_res.exit_code,
            duration_ms=round(duration_ms, 2),
            output_summary=exec_res.format_output()[:200],
            no_uncaught_tracebacks=no_tracebacks,
            feedback="Fallback test suite executed." if passed else "Test suite failed.",
        )

    @staticmethod
    def _is_safe_cli_command(cmd: str) -> bool:
        """Validate that a CLI contract is a direct python invocation without shell metacharacters."""
        stripped = cmd.strip()
        if not re.match(r"^python3?\s+[\w\-./\\]+", stripped):
            return False
        # Disallow shell operators, pipes, redirects, variables, and chaining
        disallowed = ("&", ";", "|", ">", "<", "$", "`", "\n", "\r")
        if any(token in stripped for token in disallowed):
            return False
        return True

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

        # Execute test runner harness in sandbox safely via base64 encoded payload
        py_code = (
            f"import sys; "
            f"from {module_name} import {func_name}; "
            f"actual = {func_name}({args_str}); "
            f"expected = {expected_raw}; "
            f"print('ACTUAL:', repr(actual)); "
            f"assert actual == expected, f'Expected {{expected}}, got {{actual}}'; "
        )
        encoded_b64 = base64.b64encode(py_code.encode("utf-8")).decode("ascii")
        res = self.runner.run_command(
            f"python -c \"import base64; exec(base64.b64decode('{encoded_b64}').decode('utf-8'))\"",
            trusted_env=build_sandbox_python_env(self.runner),
        )
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
        res = self.runner.run_command(cmd, trusted_env=build_sandbox_python_env(self.runner))
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
        for py_file in sorted(self.runner.workspace_dir.rglob("*.py")):
            try:
                resolved = self.runner.resolve_path(py_file)
                rel_path = resolved.relative_to(self.runner.workspace_dir)
                if (
                    resolved.name.startswith("test_")
                    or rel_path.parts[0] == "tests"
                    or any(part.startswith(".") or part == "__pycache__" for part in rel_path.parts)
                ):
                    continue
                content = resolved.read_text(encoding="utf-8", errors="ignore")
                if pattern.search(content):
                    mod_parts = list(rel_path.with_suffix("").parts)
                    if len(mod_parts) > 1 and mod_parts[0] == "src":
                        mod_parts = mod_parts[1:]
                    return ".".join(mod_parts)
            except Exception:
                continue
        return None
