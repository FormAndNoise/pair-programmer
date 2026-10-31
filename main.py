"""Main executable entrypoint for the Cross-Model Pair Programming Coding Agent."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live

# Ensure src is on sys.path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from agent_core.events import EventBus
from agent_core.types import AgentConfig, ToolCall, UserStory
from agile.backlog import BacklogManager
from agile.pair_engine import PairProgrammingEngine
from gateway.client import ModelGateway
from gateway.mock import ScriptedMockGateway
from sandbox.runner import SandboxRunner
from sandbox.docker_runner import DockerSandboxRunner
from sandbox.git_manager import GitManager
from tools.registry import ToolRegistry
from tui.app import PairProgrammingTUI
from tui.keyboard import TerminalKeyboardController


def _build_runner(args, sandbox_path: Path) -> SandboxRunner | DockerSandboxRunner:
    """Select and construct the sandbox runner based on CLI flags and environment.

    Reads SANDBOX_MODE from the environment when --docker is not specified.
    Prints the selected mode to stderr so the user sees it on startup.
    """
    use_docker = args.docker or os.getenv("SANDBOX_MODE", "local_process") == "docker"
    if use_docker:
        runner = DockerSandboxRunner(
            workspace_dir=sandbox_path,
            image=os.getenv("SANDBOX_IMAGE", "python:3.11-slim"),
        )
        print(f"[Sandbox] Mode: Docker  image={runner.image}  workspace={sandbox_path}")
    else:
        runner = SandboxRunner(workspace_dir=sandbox_path)
        print(f"[Sandbox] Mode: local_process  workspace={sandbox_path}")
    return runner


def _git_enabled(args) -> bool:
    """Return True when Git automation is on, from --git or GIT_AUTOMATION=1."""
    return bool(getattr(args, "git", False)) or os.getenv("GIT_AUTOMATION", "0").lower() in ("1", "true", "yes")


def _attach_git_manager(console: Console, bus: EventBus, sandbox_path: Path) -> GitManager | None:
    """Attach a GitManager to the bus. Return None when git is not installed."""
    if not GitManager.is_available():
        console.print("[bold red][Git] git executable not found. Git automation is off.[/]")
        return None
    manager = GitManager(
        sandbox_path,
        bus=bus,
        base_branch=os.getenv("GIT_BASE_BRANCH", "main"),
    )
    console.print(f"[Git] Automation on  base={manager.base_branch}  git_dir={manager.git_dir}")
    return manager


def run_demo_mock_sprint(console: Console, sandbox_dir: Path, use_docker: bool = False, use_git: bool = False) -> None:
    """Run an interactive zero-cost simulated sprint demonstrating dual-model pair programming."""
    bus = EventBus()
    tui = PairProgrammingTUI(bus=bus, console=console)
    git_mgr = _attach_git_manager(console, bus, sandbox_dir) if use_git else None
    if use_docker:
        runner = DockerSandboxRunner(
            workspace_dir=sandbox_dir,
            image=os.getenv("SANDBOX_IMAGE", "python:3.11-slim"),
        )
    else:
        runner = SandboxRunner(sandbox_dir)
    registry = ToolRegistry(runner)
    mock_gateway = ScriptedMockGateway()

    config = AgentConfig(
        model_a="anthropic/claude-3-5-sonnet",
        model_b="gemini/gemini-2-5-flash",
        sandbox_dir=str(sandbox_dir),
    )

    backlog_mgr = BacklogManager.from_epic("Build Text Processing Suite")
    backlog_mgr.add_story(
        title="Word Frequency Counter",
        human_interaction_contract="`count_words('apple banana apple')` returns `{'apple': 2, 'banana': 1}`.",
        acceptance_criteria=["Handles empty string", "Case-insensitive counting"],
    )
    backlog_mgr.add_story(
        title="Slug Generator",
        human_interaction_contract="`slugify('Hello World!')` returns `'hello-world'`.",
        acceptance_criteria=["Replaces spaces with hyphens", "Strips punctuation"],
    )

    # Script Story 1 (Word Frequency)
    # 1. Model A writes test
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
        content="I am writing the unit tests for word frequency counting with clean assertions.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "test_word.py", "content": test_word_code})],
    )

    # 2. Model B writes implementation
    impl_word_code = (
        "from collections import Counter\n"
        "def count_words(text: str) -> dict:\n"
        "    if not text.strip(): return {}\n"
        "    words = text.lower().split()\n"
        "    return dict(Counter(words))\n"
    )
    mock_gateway.queue_text_response(
        model=config.model_b,
        content="Implemented word counting using collections.Counter. Handling lowercased tokens.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "word_counter.py", "content": impl_word_code})],
    )

    # 3. Model A reviews
    mock_gateway.queue_text_response(
        model=config.model_a,
        content="The implementation is clean, idiomatic, and defensive. LGTM.",
    )

    # Script Story 2 (Slugify - ROTATED ROLES)
    # 4. Model B writes test (Now Model B is Test Author!)
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
        content="Model B taking turn as Test Author: Writing tests for slugify.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "test_slug.py", "content": test_slug_code})],
    )

    # 5. Model A writes implementation (Now Model A is Implementer!)
    impl_slug_code = (
        "import re\n"
        "def slugify(text: str) -> str:\n"
        "    cleaned = re.sub(r'[^a-zA-Z0-9\\s]', '', text).strip().lower()\n"
        "    return re.sub(r'\\s+', '-', cleaned)\n"
    )
    mock_gateway.queue_text_response(
        model=config.model_a,
        content="Model A taking turn as Implementer: Implemented regex slugification.",
        tool_calls=[ToolCall(name="write_file", arguments={"path": "slug_util.py", "content": impl_slug_code})],
    )

    # 6. Model B reviews
    mock_gateway.queue_text_response(
        model=config.model_b,
        content="Regex handles punctuation and whitespace cleanly. LGTM.",
    )

    engine = PairProgrammingEngine(
        config=config,
        backlog_mgr=backlog_mgr,
        runner=runner,
        registry=registry,
        gateway=mock_gateway,
        bus=bus,
    )

    # Run in live terminal layout with non-blocking keyboard controller
    kb = TerminalKeyboardController(engine=engine, bus=bus, tui=tui, runner=runner, git_manager=git_mgr)
    with Live(tui.render_layout(), console=console, refresh_per_second=4) as live, kb:
        bus.subscribe_all(lambda e: live.update(tui.render_layout()))
        engine.run_sprint()
        time.sleep(1)

    console.print("\n[bold green]Sprint Execution Finished Successfully![/]")
    console.print(f"Artifacts written to sandbox: [cyan]{sandbox_dir}[/]\n")


def main() -> None:
    """Main CLI entry point."""
    load_dotenv()
    console = Console()

    parser = argparse.ArgumentParser(description="Dual-Model Cross-Vendor Pair Programming Agent")
    parser.add_argument("--mock", action="store_true", help="Run simulated sprint using ScriptedMockGateway (zero API cost)")
    parser.add_argument("--simulate-ux", action="store_true", help="Execute complete automated desktop AI UX simulation and scorecard")
    parser.add_argument("--docker", action="store_true", help="Use Docker container sandbox instead of local subprocess (requires Docker daemon)")
    parser.add_argument("--git", action="store_true", help="Automate conventional story branches, per-model commits, and --no-ff merges")
    parser.add_argument("--epic", type=str, default="Build Core CLI", help="Objective or Epic to implement")
    parser.add_argument("--max-stories", type=int, default=3, help="Maximum stories generated by Product Owner decomposition")
    parser.add_argument("--no-decompose", action="store_true", help="Skip Product Owner LLM epic decomposition and use a single story")
    parser.add_argument("--web", action="store_true", help="Run the browser collaboration studio API")
    parser.add_argument("--web-host", type=str, default="127.0.0.1", help="Host for the web API")
    parser.add_argument("--web-port", type=int, default=8000, help="Port for the web API")
    parser.add_argument("--model-a", type=str, default=os.getenv("MODEL_A", "anthropic/claude-3-5-sonnet"))
    parser.add_argument("--model-b", type=str, default=os.getenv("MODEL_B", "gemini/gemini-2-5-flash"))
    parser.add_argument("--sandbox", type=str, default="./sandbox_workspace", help="Path to isolated sandbox workspace")

    args = parser.parse_args()
    sandbox_path = Path(args.sandbox).resolve()

    if args.simulate_ux:
        from tests.ux_simulation.run_simulation import execute_desktop_ai_simulation
        success, _ = execute_desktop_ai_simulation(console=console)
        if not success:
            sys.exit(1)
        return

    if args.mock:
        console.print("[bold yellow]Running Demo Sprint with Scripted Dual-Model Mock Pair...[/]")
        run_demo_mock_sprint(console, sandbox_path, use_docker=args.docker, use_git=_git_enabled(args))
        return

    # Live Mode
    console.print("[bold cyan]Dual-Model Pair Programming Agent[/]")
    console.print(f"Model A (Driver/Test): [green]{args.model_a}[/]")
    console.print(f"Model B (Navigator/Impl): [green]{args.model_b}[/]")
    console.print(f"Contained Sandbox: [green]{sandbox_path}[/]\n")

    bus = EventBus()
    tui = PairProgrammingTUI(bus=bus, console=console)
    runner = _build_runner(args, sandbox_path)
    registry = ToolRegistry(runner)
    gateway = ModelGateway()
    git_mgr = _attach_git_manager(console, bus, sandbox_path) if _git_enabled(args) else None

    config = AgentConfig(
        model_a=args.model_a,
        model_b=args.model_b,
        sandbox_dir=str(sandbox_path),
    )

    backlog_mgr = BacklogManager.from_epic(args.epic)
    if args.no_decompose or args.web:
        backlog_mgr.add_story(
            title="Initialize Feature & Spec",
            human_interaction_contract=f"Implement basic interface and verification tests for: {args.epic}",
        )
    else:
        console.print(f"[cyan][Product Owner][/] Decomposing epic into up to {args.max_stories} Outside-In stories...")
        backlog_mgr.decompose_epic(
            gateway=gateway,
            model=args.model_a,
            max_stories=args.max_stories,
            bus=bus,
        )

    engine = PairProgrammingEngine(
        config=config,
        backlog_mgr=backlog_mgr,
        runner=runner,
        registry=registry,
        gateway=gateway,
        bus=bus,
    )

    if args.web:
        from server.bridge import WebEventBridge, create_fastapi_app

        try:
            import uvicorn
        except ImportError as exc:
            raise SystemExit("Web mode requires FastAPI and Uvicorn. Install the project dependencies first.") from exc

        web_app = create_fastapi_app(WebEventBridge(bus), engine)
        if web_app is None:
            raise SystemExit("Web mode requires FastAPI. Install the project dependencies first.")
        console.print(f"[bold cyan]Pair Programming Web Studio API[/] at http://{args.web_host}:{args.web_port}")
        console.print(f"Epic: [green]{args.epic}[/]")
        console.print("Start the Vite frontend in a second terminal with `cd frontend; npm run dev`.\n")
        uvicorn.run(web_app, host=args.web_host, port=args.web_port)
        return

    kb = TerminalKeyboardController(engine=engine, bus=bus, tui=tui, runner=runner, git_manager=git_mgr)
    with Live(tui.render_layout(), console=console, refresh_per_second=4) as live, kb:
        bus.subscribe_all(lambda e: live.update(tui.render_layout()))
        engine.run_sprint()


if __name__ == "__main__":
    main()
