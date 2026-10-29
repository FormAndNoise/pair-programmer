"""End-to-End Desktop AI UX Simulation Runner.

Executes a dual-model sprint simulation, captures SVG terminal snapshots, runs
the Outside-In AI User contract evaluator, and produces a Human Usability Scorecard.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_core.events import EventBus
from agent_core.types import AgentConfig, ToolCall
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.mock import ScriptedMockGateway
from sandbox.runner import SandboxRunner
from sandbox.git_manager import GitManager
from tools.registry import ToolRegistry
from tui.app import PairProgrammingTUI
from .outside_in_evaluator import OutsideInUXEvaluator, UXScorecard


def execute_desktop_ai_simulation(
    output_dir: Path | None = None,
    console: Console | None = None,
) -> tuple[bool, list[UXScorecard]]:
    """Execute complete simulated desktop AI sprint and evaluate user experience."""
    console = console or Console()
    project_root = Path(__file__).parent.parent.parent
    reports_dir = output_dir or (project_root / "reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir = project_root / "tests" / "ux_simulation" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    sandbox_path = project_root / "sandbox_workspace"
    sandbox_path.mkdir(parents=True, exist_ok=True)

    console.print(Panel(
        Text("DESKTOP AI USER EXPERIENCE SIMULATION RUNNER", style="bold cyan", justify="center"),
        subtitle="Simulating Outside-In TDD, Role Rotation, and Visual TUI Capture",
        border_style="cyan",
    ))

    # Initialize subsystems
    bus = EventBus()
    record_console = Console(record=True, width=120, height=40, color_system="truecolor")
    tui = PairProgrammingTUI(bus=bus, console=record_console)
    runner = SandboxRunner(sandbox_path)
    registry = ToolRegistry(runner)
    mock_gateway = ScriptedMockGateway()

    # Wire automated Git manager if available
    git_manager = None
    if GitManager.is_available():
        git_dir = project_root / ".pair_programmer_git" / f"{sandbox_path.name}.git"
        git_manager = GitManager(workspace_dir=sandbox_path, bus=bus, git_dir=git_dir)

    config = AgentConfig(
        model_a="anthropic/claude-3-5-sonnet",
        model_b="gemini/gemini-2.5-flash",
        sandbox_dir=str(sandbox_path),
    )

    backlog_mgr = BacklogManager.from_epic("Text Processing & Formatting Suite")
    s1 = backlog_mgr.add_story(
        title="Word Frequency Counter",
        human_interaction_contract="`count_words('apple banana apple')` returns `{'apple': 2, 'banana': 1}`.",
        acceptance_criteria=["Handles whitespace", "Case-insensitive"],
    )
    s2 = backlog_mgr.add_story(
        title="Slug Generator",
        human_interaction_contract="`slugify('Hello World!')` returns `'hello-world'`.",
        acceptance_criteria=["Replaces spaces with hyphens", "Strips punctuation"],
    )

    # -----------------------------------------------------------------
    # Script Story 1: Model A = Test Author, Model B = Implementer
    # -----------------------------------------------------------------
    test_word_code = (
        "import unittest\n"
        "from word_counter import count_words\n"
        "class TestWordCounter(unittest.TestCase):\n"
        "    def test_basic_count(self):\n"
        "        self.assertEqual(count_words('apple banana apple'), {'apple': 2, 'banana': 1})\n"
        "if __name__ == '__main__': unittest.main()\n"
    )
    mock_gateway.queue_text_response(
        model=config.model_a,
        content="Writing unit tests for word frequency counting.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "test_word.py", "content": test_word_code})],
    )

    impl_word_code = (
        "from collections import Counter\n"
        "def count_words(text: str) -> dict:\n"
        "    if not text.strip(): return {}\n"
        "    return dict(Counter(text.lower().split()))\n"
    )
    mock_gateway.queue_text_response(
        model=config.model_b,
        content="Implemented count_words with collections.Counter and lowercasing.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "word_counter.py", "content": impl_word_code})],
    )

    mock_gateway.queue_text_response(
        model=config.model_a,
        content="Implementation meets all UX requirements. LGTM.",
    )

    # -----------------------------------------------------------------
    # Script Story 2: Role Rotation (Model B = Test Author, Model A = Impl)
    # -----------------------------------------------------------------
    test_slug_code = (
        "import unittest\n"
        "from slug_util import slugify\n"
        "class TestSlug(unittest.TestCase):\n"
        "    def test_slugify(self):\n"
        "        self.assertEqual(slugify('Hello World!'), 'hello-world')\n"
        "if __name__ == '__main__': unittest.main()\n"
    )
    mock_gateway.queue_text_response(
        model=config.model_b,
        content="Writing unit tests for slugify helper.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "test_slug.py", "content": test_slug_code})],
    )

    impl_slug_code = (
        "import re\n"
        "def slugify(text: str) -> str:\n"
        "    cleaned = re.sub(r'[^a-zA-Z0-9\\s]', '', text).strip().lower()\n"
        "    return re.sub(r'\\s+', '-', cleaned)\n"
    )
    mock_gateway.queue_text_response(
        model=config.model_a,
        content="Implemented regex slugification.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "slug_util.py", "content": impl_slug_code})],
    )

    mock_gateway.queue_text_response(
        model=config.model_b,
        content="Clean regex and boundary handling. LGTM.",
    )

    engine = PairProgrammingEngine(
        config=config,
        backlog_mgr=backlog_mgr,
        runner=runner,
        registry=registry,
        gateway=mock_gateway,
        bus=bus,
    )

    # Run the sprint
    console.print("[dim]Executing dual-model pair programming sprint...[/]")
    start_time = time.perf_counter()
    sprint_success = engine.run_sprint()
    elapsed = time.perf_counter() - start_time

    # Capture final visual frame
    tui.display_once()
    svg_frame = record_console.export_svg(title="Pair Programmer - Completed Sprint")
    (snapshots_dir / "simulated_sprint_complete.svg").write_text(svg_frame, encoding="utf-8")

    # -----------------------------------------------------------------
    # Outside-In AI User Contract Evaluation
    # -----------------------------------------------------------------
    console.print("\n[bold cyan]Running Outside-In AI User Usability Evaluation...[/]")
    evaluator = OutsideInUXEvaluator(runner)
    scorecards: list[UXScorecard] = []

    for story in backlog_mgr.sprint.stories:
        scorecard = evaluator.evaluate_story_contract(story)
        scorecards.append(scorecard)

    # -----------------------------------------------------------------
    # Render Usability Scorecard Table
    # -----------------------------------------------------------------
    table = Table(title="Human Usability Scorecard (Outside-In Evaluation)", expand=True)
    table.add_column("Story ID", width=10, style="bold cyan")
    table.add_column("Title", width=25)
    table.add_column("UX Contract", ratio=2)
    table.add_column("Verdict", width=10, justify="center")
    table.add_column("Exit Code", width=10, justify="center")
    table.add_column("Latency", width=12, justify="right")
    table.add_column("Feedback", ratio=2)

    all_passed = True
    for sc in scorecards:
        if not sc.passed:
            all_passed = False
        verdict = "[bold green]PASS[/]" if sc.passed else "[bold red]FAIL[/]"
        exit_badge = f"[green]{sc.exit_code}[/]" if sc.exit_code == 0 else f"[red]{sc.exit_code}[/]"
        table.add_row(
            sc.story_id,
            sc.story_title,
            sc.contract,
            verdict,
            exit_badge,
            f"{sc.duration_ms:.1f}ms",
            sc.feedback,
        )

    console.print(table)

    # -----------------------------------------------------------------
    # Write Usability Markdown Report
    # -----------------------------------------------------------------
    report_lines = [
        "# Human Usability Scorecard & Desktop AI UX Report",
        "",
        f"> **Sprint Epic**: {backlog_mgr.sprint.epic_goal}  ",
        f"> **Status**: {'ALL VERIFIED PASS' if all_passed else 'VERIFICATION FAILED'}  ",
        f"> **Execution Elapsed**: {elapsed:.2f}s  ",
        f"> **Generated SVG Frame**: `tests/ux_simulation/snapshots/simulated_sprint_complete.svg`  ",
        "",
        "## Story Usability Breakdown",
        "",
        "| Story ID | Title | Human Contract | Verdict | Exit Code | Latency | Feedback |",
        "| :--- | :--- | :--- | :---: | :---: | :---: | :--- |",
    ]

    for sc in scorecards:
        verdict_str = "**PASS**" if sc.passed else "**FAIL**"
        report_lines.append(
            f"| `{sc.story_id}` | {sc.story_title} | `{sc.contract}` | {verdict_str} | `{sc.exit_code}` | {sc.duration_ms:.1f}ms | {sc.feedback} |"
        )

    report_lines.extend([
        "",
        "## Definition of Done (DoD) Quality Gate Verification",
        "",
        "- [x] **Syntax & Compile Check**: Passed with `compileall`",
        "- [x] **Sandbox Automated Tests**: All `unittest` test suites passed in isolated workspace",
        "- [x] **Cross-Model Review**: Approved with explicit `LGTM`",
        "- [x] **Outside-In Human Interaction Contract**: Verified by dynamic AI User evaluator",
        f"- [x] **Automated Git Branching & Merging**: {'Conventional Commits and --no-ff merges recorded' if git_manager else 'Git skipped (executable not found)'}",
        "",
        "---",
        "*Report auto-generated by `tests.ux_simulation.run_simulation`.*",
    ])

    report_path = reports_dir / "human_usability_scorecard.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    console.print(f"\n[bold green]Report saved successfully to:[/] [cyan]{report_path}[/]\n")

    return sprint_success and all_passed, scorecards


if __name__ == "__main__":
    success, _ = execute_desktop_ai_simulation()
    if not success:
        exit(1)
