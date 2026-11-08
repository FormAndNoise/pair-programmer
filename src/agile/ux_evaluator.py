"""Outside-In UX Evaluator simulating end-user interactions in the sandbox.

Moved from ``tests/ux_simulation/outside_in_evaluator.py`` so the pair
programming engine can run it as the real fourth Definition of Done gate
(the ``user_experience_verified`` field). The evaluator replays each story's
``human_interaction_contract`` against the artifacts the models wrote in the
sandbox and reports whether the human interaction actually holds.
"""

from __future__ import annotations

import ast
import base64
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
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
        """Evaluate whether the story's human interaction contract holds in reality.

        Supported Contract Grammar (fails closed on unrecognized or non-literal contracts):
        1. Python callable: `` `[module.]func(literal_arg1, ...)` returns `literal_expected` ``
        2. Safe Python CLI: `` `python[3] script.py [args...]` ``
        """
        contract = story.human_interaction_contract.strip()
        start_time = time.perf_counter()

        # 1. Check if contract describes a Python function invocation:
        # e.g.: `func(args)` returns `expected` or `mod.func(args)` returns `expected`
        py_match = re.search(
            r"`([a-zA-Z_][a-zA-Z0-9_.]*)\((.*?)\)`\s*returns\s*`?(.*?)`?\.?$",
            contract,
        )
        if py_match:
            target_qualname = py_match.group(1)
            args_str = py_match.group(2)
            expected_raw = py_match.group(3).strip().rstrip(".")
            return self._evaluate_python_contract(
                story=story,
                target_qualname=target_qualname,
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

        # 3. Fail closed when contract does not match the explicit callable or CLI grammar
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        return UXScorecard(
            story_id=story.id,
            story_title=story.title,
            contract=contract,
            passed=False,
            exit_code=-1,
            duration_ms=round(duration_ms, 2),
            output_summary="Unrecognized or unsafe human_interaction_contract grammar.",
            no_uncaught_tracebacks=True,
            feedback=(
                "UX Contract Violation: Contract does not match supported callable "
                "(`func(...) returns ...`) or safe CLI (`python script.py ...`) grammar."
            ),
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

    @staticmethod
    def _parse_contract_literals(args_str: str, expected_raw: str) -> Tuple[Tuple[Any, ...], Dict[str, Any], Any]:
        """Parse function arguments and expected value strictly as AST literals.

        Raises ValueError if any argument or expected expression contains executable code.
        """
        call_src = f"_call({args_str})"
        try:
            tree = ast.parse(call_src, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"Invalid literal syntax in arguments: {exc}") from exc

        if not isinstance(tree.body, ast.Call):
            raise ValueError("Arguments must form a simple argument list.")
        pos_args = tuple(ast.literal_eval(arg) for arg in tree.body.args)
        kw_args: Dict[str, Any] = {}
        for kw in tree.body.keywords:
            if kw.arg is None:
                raise ValueError("Keyword unpacking (**kwargs) is not permitted in contracts.")
            kw_args[kw.arg] = ast.literal_eval(kw.value)

        try:
            expected_val = ast.literal_eval(expected_raw)
        except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
            raise ValueError(f"Expected return value must be a safe Python literal: {exc}") from exc

        return pos_args, kw_args, expected_val

    def _evaluate_python_contract(
        self,
        story: UserStory,
        target_qualname: str,
        args_str: str,
        expected_raw: str,
        start_time: float,
    ) -> UXScorecard:
        """Resolve module, validate literal args/expected, and execute contract safely."""
        try:
            pos_args, kw_args, expected_val = self._parse_contract_literals(args_str, expected_raw)
        except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return UXScorecard(
                story_id=story.id,
                story_title=story.title,
                contract=story.human_interaction_contract,
                passed=False,
                exit_code=-1,
                duration_ms=round(duration_ms, 2),
                output_summary=f"Rejected non-literal contract expression: {exc}",
                no_uncaught_tracebacks=True,
                feedback=f"UX Contract Violation: Non-literal or executable expression rejected ({exc}).",
            )

        if "." in target_qualname:
            module_name, func_name = target_qualname.rsplit(".", 1)
            if not all(part.isidentifier() for part in module_name.split(".")) or not func_name.isidentifier():
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                return UXScorecard(
                    story_id=story.id,
                    story_title=story.title,
                    contract=story.human_interaction_contract,
                    passed=False,
                    exit_code=-1,
                    duration_ms=round(duration_ms, 2),
                    output_summary=f"Invalid module/function identifier '{target_qualname}'.",
                    no_uncaught_tracebacks=True,
                    feedback="UX Contract Violation: Invalid module or function identifier.",
                )
        else:
            func_name = target_qualname
            matching_modules = self._find_modules_with_function(func_name)
            if not matching_modules:
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
            if len(matching_modules) > 1:
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                return UXScorecard(
                    story_id=story.id,
                    story_title=story.title,
                    contract=story.human_interaction_contract,
                    passed=False,
                    exit_code=-1,
                    duration_ms=round(duration_ms, 2),
                    output_summary=(
                        f"Ambiguous function '{func_name}' found in multiple modules: "
                        f"{', '.join(matching_modules)}"
                    ),
                    no_uncaught_tracebacks=True,
                    feedback=(
                        f"UX Contract Violation: Ambiguous function '{func_name}' defined in "
                        f"multiple modules ({', '.join(matching_modules)})."
                    ),
                )
            module_name = matching_modules[0]

        nonce = uuid.uuid4().hex
        payload_repr = repr((module_name, func_name, pos_args, kw_args, expected_val, nonce))
        encoded_b64 = base64.b64encode(payload_repr.encode("utf-8")).decode("ascii")
        runner_script = (
            "import ast, base64, importlib, sys; "
            f"mod_name, fn_name, args, kwargs, expected, nonce = ast.literal_eval(base64.b64decode('{encoded_b64}').decode('utf-8')); "
            "mod = importlib.import_module(mod_name); "
            "fn = getattr(mod, fn_name); "
            "actual = fn(*args, **kwargs); "
            "print('ACTUAL:', repr(actual)); "
            "assert actual == expected, f'Expected {expected!r}, got {actual!r}'; "
            "print('UX_OK:' + nonce)"
        )
        res = self.runner.run_command(
            f'python -c "{runner_script}"',
            trusted_env=build_sandbox_python_env(self.runner),
        )
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        no_tracebacks = "Traceback (most recent call last)" not in res.stderr
        passed = res.is_success and f"UX_OK:{nonce}" in res.stdout

        feedback = (
            f"Contract verified: `{target_qualname}({args_str})` evaluated to `{expected_raw}` correctly."
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

    def _find_modules_with_function(self, func_name: str) -> List[str]:
        """Find all non-test sandbox modules that define top-level function ``func_name``."""
        matches: List[str] = []
        for py_file in sorted(self.runner.workspace_dir.rglob("*.py")):
            try:
                resolved = self.runner.resolve_path(py_file)
                rel_path = resolved.relative_to(self.runner.workspace_dir)
                if (
                    resolved.name.startswith("test_")
                    or resolved.name.endswith("_test.py")
                    or rel_path.parts[0] == "tests"
                    or any(part.startswith(".") or part == "__pycache__" for part in rel_path.parts)
                ):
                    continue
                content = resolved.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content)
                has_fn = any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name
                    for node in tree.body
                )
                if has_fn:
                    mod_parts = list(rel_path.with_suffix("").parts)
                    if len(mod_parts) > 1 and mod_parts[0] == "src":
                        mod_parts = mod_parts[1:]
                    mod_name = ".".join(mod_parts)
                    if mod_name not in matches:
                        matches.append(mod_name)
            except Exception:
                continue
        return matches

    def _find_module_with_function(self, func_name: str) -> Optional[str]:
        """Return single unambiguous module defining func_name, or None."""
        matches = self._find_modules_with_function(func_name)
        return matches[0] if len(matches) == 1 else None
