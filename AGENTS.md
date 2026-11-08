# AGENTS.md — Pair Programmer Agent Guide

## Essential Commands

```bash
# Install (editable, includes all runtime deps)
python -m pip install -e .

# Run all tests (correct invocation — pytest is dev-only, unittest is authoritative)
PYTHONPATH=src python -m unittest discover tests
# Windows PowerShell:
$env:PYTHONPATH="src" ; python -m unittest discover tests
# Expected: Ran 118 tests in ~60s OK (27 skipped if Docker daemon is not running)

# Run demo sprint (zero API cost, no keys needed)
python main.py --mock

# Run demo sprint with Docker sandbox
python main.py --mock --docker

# Run demo sprint with Git automation
python main.py --mock --git

# Live sprint with real LLMs (requires .env with API keys)
python main.py --epic "Build a markdown to HTML converter"

# Run the UX simulation scorecard
python main.py --simulate-ux

# Web collaboration studio (two terminals)
python main.py --web --epic "..."   # terminal 1
cd frontend && npm run dev          # terminal 2
```

## Architecture Overview

The system is a **dual-model XP harness**. Two architecturally distinct LLMs from competing vendors (e.g., Claude and Gemini) take turns as Test Author and Implementer across a sprint backlog. The engine is a state machine that runs without human confirmation prompts — safety is enforced by sandbox path confinement, not interactive approvals.

**Control flow (top-down):**

```
main.py
  └─ BacklogManager.from_epic()          # creates SprintBacklog
       └─ BacklogManager.decompose_epic()  # optional: PO LLM decomposes epic into UserStorys
  └─ PairProgrammingEngine.run_sprint()  # drives the state machine
       └─ execute_story_cycle(story)
            ├─ RED PHASE    → test_model writes failing tests via write_file tool
            ├─ GREEN PHASE  → impl_model writes implementation via write_file / patch_file
            ├─ REVIEW PHASE → test_model issues LGTM or critique
            └─ DoD GATE     → BacklogManager.verify_dod() runs compileall + unittest in sandbox
                 └─ REFACTOR LOOP (up to max_refactor_attempts=3) if DoD fails
```

**Role rotation rule:** Story index % 2 == 0 → model_a is Test Author, model_b is Implementer. Odd stories swap them. This is hard-coded in `pair_engine.py:execute_story_cycle`.

**Event bus decoupling:** The engine never references the TUI, WebSocket bridge, or Git manager directly. All observability flows through `EventBus.publish()`. The TUI, `WebEventBridge`, and `GitManager` each wire themselves to the bus independently. Anything that needs to react to sprint activity should subscribe to the bus, not be injected into the engine.

## Module Map

| Package | Responsibility |
|---|---|
| `src/agent_core/types.py` | All domain contracts (Pydantic models). The authoritative source for `UserStory`, `ToolCall`, `Message`, `DefinitionOfDone`, etc. No business logic. |
| `src/agent_core/events.py` | `EventBus` + `EventType` enum + `AgentEvent`. All inter-module communication goes through here. |
| `src/agile/backlog.py` | `BacklogManager`: story creation, LLM epic decomposition, DoD verification (runs `compileall` + `python -m unittest` + the Outside-In UX audit in sandbox). |
| `src/agile/ux_evaluator.py` | `OutsideInUXEvaluator`: replays each story's `human_interaction_contract` against sandbox artifacts; the real fourth DoD gate. |
| `src/agile/pair_engine.py` | `PairProgrammingEngine`: the TDD state machine, role rotation, refactor loop, operator pause/stop/steer. |
| `src/gateway/client.py` | `ModelGateway`: unified LLM caller. Prefers LiteLLM if installed; falls back to direct native routes: Anthropic Messages API, Gemini generateContent API, or OpenAI-compatible endpoint (all via `httpx`). |
| `src/gateway/mock.py` | `ScriptedMockGateway`: FIFO queue of pre-scripted responses per model name. Used in all tests. |
| `src/sandbox/runner.py` | `SandboxRunner`: path confinement via `resolve_path()`, subprocess execution with timeout watchdog. |
| `src/sandbox/docker_runner.py` | `DockerSandboxRunner`: same interface as `SandboxRunner`, runs commands in ephemeral container with `--network none`. |
| `src/sandbox/git_manager.py` | `GitManager`: creates story branches, commits with model attribution, `--no-ff` merges. Subscribes to `STORY_START` / `STORY_DONE` events. |
| `src/tools/primitives.py` | `read_file`, `write_file`, `patch_file`, `list_dir`, `run_command` — all take `runner: SandboxRunner` as first arg. Never throw; always return `ToolResult`. |
| `src/tools/registry.py` | `ToolRegistry`: introspects primitives, generates OpenAI-format JSON schema, dispatches tool calls by name. |
| `src/tui/app.py` | `PairProgrammingTUI`: Rich-based dashboard. Subscribes to bus; stateless renders via `render_layout()`. |
| `src/tui/keyboard.py` | `TerminalKeyboardController`: context manager, polls keypresses in background thread. `Space`=pause/resume, `q`=stop, `d`=inspect diff, `v`=full terminal, typing=inline steering. Uses `msvcrt` on Windows, `select` + `termios`/`tty` on POSIX. |
| `src/server/bridge.py` | `WebEventBridge` + FastAPI app factory (`create_fastapi_app`). Runs engine in `asyncio.to_thread` to avoid blocking the event loop. |

## Key Conventions & Non-Obvious Patterns

### PYTHONPATH must be `src`
The project uses `src/` layout. Nothing in `src/` imports with a `src.` prefix — all imports are relative to `src/` (e.g., `from agent_core.types import ...`). `pyproject.toml` sets `pythonpath = ["src"]` for pytest, but `main.py` manually prepends `src` to `sys.path` at startup. For any script or test runner that doesn't go through `main.py`, you must set `PYTHONPATH=src`.

### Tests use `unittest`, not pytest
The test command is `python -m unittest discover tests`, not `pytest`. While pytest can run these tests, the project's own invocations, CI expectations, and README all use `unittest`. The suite targets `tests/` directly, and `tests/ux_simulation/` contains additional simulation tests (118 tests total).

### `ScriptedMockGateway` is FIFO per model
When writing tests that exercise `PairProgrammingEngine`, you must queue responses in exact turn order, keyed by the model name string. The mock dequeues one response per `send_chat` call for that model. If the queue is exhausted, it raises `StopIteration`. Test setup in `test_e2e_mock_sprint.py` is the canonical example.

### Review approval is string-based
`PairProgrammingEngine._run_review_phase` checks `"LGTM" in response.upper()`. The mock gateway's review responses must include the literal string `LGTM` (case-insensitive) to trigger approval. If approval is missing, the refactor loop activates (up to `max_refactor_attempts=3`). In mock tests with a fully scripted gateway, the refactor loop will exhaust the queue if LGTM is absent.

### DoD verification runs inside the sandbox
`BacklogManager.verify_dod()` calls `runner.run_command("python -m compileall -q .")` and `runner.run_command("python -m unittest")` inside `sandbox_workspace/`. Tests written by the agent to `tests/` within the sandbox (not the repo's `tests/`) are what get validated. The sandbox and the repo test suite are entirely separate.

### Tool primitives never raise
All functions in `src/tools/primitives.py` catch all exceptions and return `ToolResult(is_error=True)`. Never let a primitive propagate an exception — the engine logs the error and continues the turn. This is a hard rule; break it and the harness crashes mid-sprint.

### Adding a new tool — exact steps
1. Add handler to `src/tools/primitives.py` with `runner: SandboxRunner` as first parameter.
2. Return `ToolResult`; catch all exceptions inside.
3. Register in `ToolRegistry._register_default_tools()` in `src/tools/registry.py` with JSON Schema.
4. Add tests in `tests/test_tools.py`.

### `patch_file` requires an exact, unique snippet
The `patch_file` tool (adapted from Aider's diff engine) does a literal string replace. If the `target_snippet` appears zero times or more than once in the file, it returns an error. Models using this tool must include enough surrounding context to make the target unique.

### Web mode runs the engine in a thread
`create_fastapi_app` runs `engine.run_sprint()` via `asyncio.to_thread()`. The engine itself is synchronous and blocking — do not add `async` to engine methods. The `WebEventBridge._on_agent_event` handler uses `loop.call_soon_threadsafe` to safely push events from the engine thread to the async WebSocket queues.

The web API requires `Authorization: Bearer <token>` on every `/api/*` request. Set `PAIR_PROGRAMMER_TOKEN` or use the generated token at `~/.pair_programmer/token`. The studio stores the token in the current tab's `sessionStorage`. WebSockets use the `bearer.<token>` subprotocol because browser clients cannot set handshake headers. Reject untrusted origins on WebSocket upgrades and state-changing REST requests; allow clients that omit `Origin`. Keep the CORS allowlist limited to the local Vite origins. The bridge caps REST traffic at 60 requests per minute per IP, WebSocket clients at 10, recent REST events at 300, and event history at 1000.

### Operator steering is consumed destructively
`_consume_steering_notes()` drains the steering queue and formats it into a prompt suffix. Once consumed, the guidance is gone. The guidance is injected into the next GREEN or REFACTOR phase prompt, not the RED or REVIEW phases.

### Model string format
Models are specified as `provider/model-name` (e.g., `anthropic/claude-3-5-sonnet-20241022`, `gemini/gemini-2.5-flash`). The `ModelGateway` splits on `/` to determine provider and model name. The direct (non-LiteLLM) fallback routes `anthropic`/`claude` to the Anthropic Messages API, `gemini`/`google` to the Gemini generateContent API, and everything else to the OpenAI-compatible route. `openrouter/` and `groq/` prefixes auto-select their base URLs; `OPENAI_BASE_URL` overrides all of them. Note: OpenRouter model names contain their own slash (e.g., `openrouter/anthropic/claude-3.5-sonnet`) — the split keeps the remainder as the model name. Mock gateway keys are whatever string you pass — they do not need to follow this format, but they must match exactly between `AgentConfig` and `queue_text_response` calls in tests.

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key | — |
| `GEMINI_API_KEY` | Google Gemini API key | — |
| `OPENAI_API_KEY` | OpenAI key | — |
| `OPENROUTER_API_KEY` | OpenRouter key (auto-routes to `https://openrouter.ai/api/v1`) | — |
| `PAIR_PROGRAMMER_TOKEN` | Optional bearer token for the web API; generated token is used when unset | — |
| `MODEL_A` | First model identifier | `anthropic/claude-3-5-sonnet` |
| `MODEL_B` | Second model identifier | `gemini/gemini-2.5-flash` |
| `SANDBOX_TIMEOUT_SECONDS` | Subprocess watchdog | `60` |
| `SANDBOX_MODE` | `local_process` or `docker` | `local_process` |
| `SANDBOX_IMAGE` | Docker image for sandbox | `python:3.11-slim` |
| `GIT_AUTOMATION` | `1`/`true` enables GitManager | `0` |
| `GIT_BASE_BRANCH` | Base branch for story merges | `main` |
| `OPENAI_BASE_URL` | Override for OpenAI-compatible endpoint | `https://api.openai.com/v1` |

## Writing Style (ASD-STE100)

The project enforces Simplified Technical English at ~80% compliance in docstrings, commit messages, and comments:
- Use active voice.
- Keep instructions under 20 words.
- Use unambiguous verbs: `execute`, `verify`, `dispatch`, `render`, `truncate`.
- Avoid vague jargon.

## Definition of Done (Non-Negotiable)

Before any story is closed, all four gates must pass:
1. `python -m compileall -q .` exits 0 in the sandbox.
2. `python -m unittest` exits 0 in the sandbox.
3. The non-author model's response contains `LGTM`.
4. The Outside-In UX contract audit passes. `BacklogManager.verify_dod()` runs `OutsideInUXEvaluator` (from `src/agile/ux_evaluator.py`) against the sandbox artifacts; gated by `AgentConfig.require_ux_audit` (default `True`).

## Frontend

React + Vite TypeScript app in `frontend/`. Proxies `/api` and WebSocket `/ws` to `http://127.0.0.1:8000` (configured in `vite.config.ts`). The CORS whitelist in `bridge.py` allows `localhost:5173` and `127.0.0.1:5173` only. Install: `cd frontend && npm install`. Dev: `npm run dev`.
