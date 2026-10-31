# Pair Programmer: Dual-Model Cross-Vendor XP Coding Harness

> **Autonomous Extreme Programming Studio**  
> *Two distinct foundation model architectures from competing providers take turns as Driver and Navigator to design, test, and implement software inside an isolated sandbox.*

---

## 1. Concept & Vision

`Pair Programmer` is an autonomous coding agent runtime designed around **Extreme Programming (XP)**, **Ping-Pong Test-Driven Development (TDD)**, and an **Outside-In (UI/UX-First)** engineering philosophy.

Most modern AI agent platforms suffer from three systemic flaws:
1. **Persona Hallucination**: Assigning different "system personas" (e.g., "Senior Architect" vs. "Junior QA") to the *same* underlying model. This shares identical training biases, token blind spots, and alignment drift.
2. **Backend-First Plumbing**: Writing database schemas and internal functions first, treating human usability and error feedback as an afterthought.
3. **Approval Fatigue**: Prompting the human operator with interactive `[y/N]` confirmation prompts for every terminal command, which destroys developer flow and encourages mindless approval.

`Pair Programmer` solves these flaws through a unified architectural formula:

$$\text{Agent Execution} = \text{Agile Engine}\Big(\text{Model}_A \rightleftharpoons \text{Model}_B,\, \text{Sandbox}(\text{Workspace}),\, \text{Outside-In DoD}\Big)$$

### Core Tenets

* **True Cross-Vendor Model Pairing**: The system coordinates two architecturally distinct models from competing companies (e.g., **Anthropic Claude 3.5 Sonnet** and **Google Gemini 2.5 Flash/Pro**). One model writes the failing tests; the competing model writes the implementation; the first model audits the code.
* **Ping-Pong TDD & Role Rotation**: On every sprint story, the roles rotate. Model A drives Story $N$, and Model B drives Story $N+1$.
* **Unsupervised Contained Execution**: The agent runs without supervision. Safety is enforced through hard workspace path confinement, sub-process watchdogs, and sandbox boundaries—not interactive human confirmations.
* **Outside-In (Human-First) Development**: Software exists for humans to interact with machines. The Human Experience Contract (CLI flags, outputs, error messages, component states) must be defined and tested *before* writing business logic.

---

## 2. Derivation & Architectural Lineage

The harness evolved from foundational autonomous agent research (specifically the ReAct loop: *Reason + Act*), restructured through software engineering rigor:

```
[Traditional Agent]   User Prompt ──> ReAct Loop ──> Unchecked File Edits ──> Exit
                                          │
[Pair Programmer]     User Epic   ──> Sprint Backlog ──> Outside-In UX Contract
                                                              │
                                          ┌───────────────────┴───────────────────┐
                                          ▼                                       ▼
                                  Model A (Claude)                        Model B (Gemini)
                                 Writes Failing Tests                   Implements Clean Code
                                    (RED Phase)                             (GREEN Phase)
                                          │                                       │
                                          └───────────────────┬───────────────────┘
                                                              ▼
                                                   Cross-Model Review (LGTM)
                                                              ▼
                                              Definition of Done (DoD) Gate
                                           (Syntax + Sandbox Tests + UX Audit)
                                                              ▼
                                                      Next Story Rotation
```

1. **State Machine over Ad-Hoc Prompting**: Instead of relying on a single prompt to maintain discipline, the control flow is enforced by a state machine (`PairProgrammingEngine`).
2. **Contract-First Domain Models**: All events, messages, tools, and sprint artifacts are strictly typed dataclasses/Pydantic schemas (`agent_core.types`). No module imports another module circularly.
3. **Event-Driven UI Decoupling**: The core engine knows nothing about terminal rendering or HTTP sockets. It emits lifecycle events to a central `EventBus`, allowing multiple UIs (Phase 1 CLI and Phase 2 Web Studio) to run concurrently.

---

## 3. Concessions & Deviations from the Original Idea

During design and implementation, several deliberate departures were made from initial assumptions:

### 1. Shift from Interactive Human Approvals to Unsupervised VM Isolation
* **Original Idea**: Prompt the user with a unified diff and command preview before every file write or terminal execution.
* **Deviation**: Switched to **unsupervised auto-pilot** inside an isolated sandbox. Interactive prompts create severe operator fatigue. Protection was moved to physical path confinement (`SandboxRunner.resolve_path`), non-traversable boundaries, and execution timeouts.

### 2. Elimination of Synthetic "Personas" in Favor of Cross-Vendor Pairing
* **Original Idea**: Use standard multi-agent frameworks with persona prompts on a single model.
* **Deviation**: Strictly banned same-model personas. Competing foundation models have different loss surfaces, training corpora, and failure modes. Putting Claude and Gemini in the same loop catches errors that a single model family consistently misses.

### 3. Outside-In (UI/UX-First) Reversal
* **Original Idea**: Allow models to build backend data structures first, then wire the interface.
* **Deviation**: Mandated that every `UserStory` include a mandatory `human_interaction_contract`. The Test Author must write tests verifying the human interface (command line UX, error messaging, output readability) before the Implementer writes code.

### 4. Phased Ergonomics: TUI Dashboard Before Web App
* **Original Idea**: Build a full-stack React chat application immediately.
* **Deviation**: Phase 1 was scoped to a high-density, low-latency **Terminal User Interface (TUI)** built with `Rich`. Phase 2 provides an asynchronous `WebEventBridge` that exposes WebSocket streams to a React frontend without modifying the engine.

### 5. Zero-Bloat Dependency Discipline
* **Original Idea**: Pull in heavy agent frameworks (LangChain, AutoGen, CrewAI).
* **Deviation**: Built a lightweight, modular engine directly on Python 3.11+ primitives. Testing uses Python's built-in `unittest` module (27 tests run in under 6 seconds with zero external test runner overhead).

---

## 4. System Architecture & Codebase Layout

```
pair programmer/
├── main.py                  # CLI entrypoint (supports --mock demo and live models)
├── pyproject.toml           # Project dependencies & package configuration
├── .env.example             # Template for API keys (Anthropic, Gemini, OpenAI)
├── .gitignore               # Excludes caches, sandbox artifacts, and sessions
│
├── src/
│   ├── agent_core/          # Shared domain contracts & communications
│   │   ├── types.py         # Message, ToolCall, UserStory, SprintBacklog, DoD
│   │   └── events.py        # Decoupled publish/subscribe EventBus
│   │
│   ├── sandbox/             # Contained execution environment
│   │   └── runner.py        # SandboxRunner (path confinement & watchdog timer)
│   │
│   ├── tools/               # Deterministic file and terminal primitives
│   │   ├── primitives.py    # read_file, write_file, patch_file, list_dir, run_command
│   │   └── registry.py      # ToolRegistry (schema generator & dispatcher)
│   │
│   ├── gateway/             # Cross-vendor model abstraction layer
│   │   ├── client.py        # Unified client for Claude, Gemini, OpenAI, local LLMs
│   │   └── mock.py          # ScriptedMockGateway for zero-cost deterministic tests
│   │
│   ├── agile/               # Extreme Programming & Scrum state machine
│   │   ├── backlog.py       # BacklogManager (story decomposition & DoD evaluation)
│   │   └── pair_engine.py   # PairProgrammingEngine (Ping-Pong TDD & role rotation)
│   │
│   ├── tui/                 # Phase 1: Terminal User Interface
│   │   └── app.py           # Rich dashboard (backlog, color-coded stage, VM log)
│   │
│   ├── server/              # Phase 2: Web UI Bridge
│   │   └── bridge.py        # WebEventBridge, sprint API, and WebSocket stream
│
├── frontend/                # Phase 2: React + Vite collaboration studio
│   ├── src/App.tsx           # Sprint brief, backlog, activity, and operator controls
│   └── vite.config.ts        # Local API and WebSocket proxy
│
├── tests/                   # Complete automated test suite (27 unit & e2e tests)
│   ├── test_types.py        # Contract validation and provider format tests
│   ├── test_events.py       # EventBus subscription & history tests
│   ├── test_sandbox.py      # Path traversal blocking & timeout watchdog tests
│   ├── test_tools.py        # File manipulation, patch diffs, and registry tests
│   ├── test_gateway.py      # Mock gateway response queuing and turn tests
│   ├── test_agile.py        # Single-story TDD cycle with mock LLMs
│   ├── test_tui.py          # TUI layout and event rendering tests
│   ├── test_e2e_mock_sprint.py # Multi-story sprint test with role rotation
│   └── test_server_bridge.py   # WebSocket queue and steering injection tests
│
└── sandbox_workspace/       # Isolated guest directory where agent writes code
```

---

## 5. Quickstart Guide for New Team Members

### 1. Prerequisites
* **Python 3.11+** installed and available on your system path.
* Standard terminal (PowerShell, Bash, or Zsh).

### 2. Environment Setup

Clone or open the repository directory:
```bash
cd "c:\Users\Aarik\Desktop\pair programmer"
```

Configure your environment variables:
```bash
cp .env.example .env
```

Edit `.env` with your API keys:
```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
OPENAI_API_KEY=your_openai_api_key_here

MODEL_A=anthropic/claude-3-5-sonnet-20241022
MODEL_B=gemini/gemini-2.5-flash
SANDBOX_TIMEOUT_SECONDS=60
```

Install the Python package and web API dependencies:

```powershell
python -m pip install -e .
```

### 3. Run the Automated Test Suite

Before making any code changes, verify that all 27 unit and integration tests pass:

```powershell
$env:PYTHONPATH="src" ; python -m unittest discover tests
```
*(On Linux/macOS: `PYTHONPATH=src python3 -m unittest discover tests`)*

Expected output:
```text
Ran 27 tests in ~5.9s
OK
```

### 4. Run the Zero-Cost Simulated Demo Sprint

You do not need live API keys to test the system. The codebase includes a deterministic `ScriptedMockGateway` that demonstrates a complete 2-story sprint (`Word Frequency Counter` and `Slug Generator`) with live TUI dashboard updates:

```bash
python main.py --mock
```

To enable automated Conventional Git branching and model commit attribution:
```bash
python main.py --mock --git
```

To execute sandbox commands inside an isolated Docker container:
```bash
python main.py --mock --docker
```

Watch the terminal:
* **Model A** writes failing tests.
* **Model B** implements the functions to turn the tests green.
* **Roles rotate** on Story 2 (Model B writes tests; Model A implements code).
* Completed files and tests are written to `sandbox_workspace/`.

### 5. Run with Live Foundation Models

To launch a live coding sprint with real LLMs:

```bash
python main.py --epic "Build a command-line markdown to HTML converter"
```

### 6. Run the Phase 2 Web Collaboration Studio

Install the frontend packages once:

```powershell
cd frontend
npm install
```

Start the API and model pair in one terminal:

```powershell
cd "c:\Users\Aarik\Desktop\pair programmer"
python main.py --web --epic "Build a command-line markdown to HTML converter"
```

Start the React studio in a second terminal:

```powershell
cd "c:\Users\Aarik\Desktop\pair programmer\frontend"
npm run dev
```

Open the local Vite address shown in the terminal. The studio lets you define a sprint goal, an initial user story, its human interaction contract, and acceptance checks. It streams sprint events live and supports pause, resume, and operator guidance. The API defaults to `http://127.0.0.1:8000`.

---

## 6. Engineering Standards & Contribution Rules

To keep the codebase maintainable and robust, all contributors must observe these rules:

### A. Technical Writing Standard (ASD-STE100)
* When writing technical specifications, code docstrings, and commit messages, use **Simplified Technical English (ASD-STE100)** guidelines at an **80% compliance level**.
* Use active voice.
* Keep instructions under 20 words where possible.
* Use unambiguous verbs: `execute`, `verify`, `dispatch`, `render`, `truncate`. Avoid vague jargon.

### B. Outside-In TDD Discipline
* Every new feature must start with a `UserStory` that contains a clear `human_interaction_contract`.
* Always write the test first. Verify the test fails cleanly before implementing the logic.
* Never commit code that breaks the **Definition of Done (DoD)**:
  1. Syntax check passes (`compileall`).
  2. 100% of unit tests pass in the sandbox.
  3. The non-author model issues an explicit `LGTM` approval.
  4. The user experience contract is validated.

### C. Adding a New Tool Primitive
1. Implement the handler function in `src/tools/primitives.py`.
2. Ensure the first argument is `runner: SandboxRunner` if it interacts with files or shell processes.
3. Catch all exceptions inside the primitive; return a `ToolResult(is_error=True)` instead of allowing exceptions to crash the harness.
4. Register the tool and its JSON Schema in `ToolRegistry._register_default_tools()` in `src/tools/registry.py`.
5. Add unit tests in `tests/test_tools.py`.

### D. Hooking into the Web UI (Phase 2 Bridge)
* Do not couple web code into the core engine.
* Connect to `WebEventBridge` in `src/server/bridge.py`.
* All engine events are streamed as standard JSON dictionaries with `type`, `timestamp`, `source_model`, and `payload`.
* Web clients can inject human guidance anytime via `bridge.handle_operator_steer("guidance text")`.

### E. Open-Source Ingestion & Translation Parity Standard
* **Off-the-Shelf Encouraged**: Reusing battle-tested open-source libraries and algorithms is strongly encouraged. Do not reinvent components from the ground up if an existing solution prevents setbacks.
* **Zero-Conflict Boundary**: Imported code must not introduce bloat, licensing incompatibilities, or dependency conflicts.
* **Language Translation Parity**: When adapting or porting components written in another language (e.g., TypeScript, Rust, or Go) into Python:
  1. The translated component must preserve **100% functional equivalence** with the original.
  2. If the original component and the translated component were swapped across external projects, their behavior, inputs, and outputs must be **completely indistinguishable**.

### F. Codebase Provenance & Implementation Breakdown

| Subsystem / File | Lines | Status | Provenance & Architectural Origin |
| :--- | :---: | :--- | :--- |
| `src/agile/pair_engine.py` | 242 | **100% Custom** | Ping-Pong TDD loop, role rotation, and cross-model audit state machine. Concept derived from Extreme Programming (Kent Beck). |
| `src/agile/backlog.py` | 61 | **100% Custom** | Sprint backlog manager and 4-point Definition of Done (DoD) quality gate. |
| `src/agent_core/types.py` | 121 | **100% Custom** | Domain contracts for Outside-In user stories, tool calls, and model roles using `pydantic`. |
| `src/agent_core/events.py` | 82 | **100% Custom** | Decoupled publish/subscribe `EventBus`. Standard Event-Driven Architecture pattern. |
| `src/tools/primitives.py` | 227 | **Pattern Adapted** | Custom implementation. The `patch_file` snippet search/replace engine is adapted from **Aider**'s diff engine. |
| `src/tools/registry.py` | 145 | **Pattern Adapted** | Custom dispatcher and inspector. Tool schema generation conforms to the open **OpenAI / Anthropic Function Calling** schema standard. |
| `src/sandbox/runner.py` | 127 | **Pattern Adapted** | Workspace path confinement (`is_relative_to`), timeout watchdogs, and context truncation. Adapted from **OpenCodeInterpreter** container patterns. |
| `src/sandbox/docker_runner.py` | 215 | **100% Custom** | Phase 3: Ephemeral Docker container driver with `--network none` isolation and host path pre-confinement. |
| `src/sandbox/git_manager.py` | 295 | **100% Custom** | Phase 4: EventBus-driven Git manager with outside-sandbox repository isolation, Conventional Commits, model attribution, and `--no-ff` merges. |
| `src/gateway/client.py` | 182 | **Hybrid** | Custom direct REST caller for OpenAI-compatible/vendor endpoints, with optional passthrough to **LiteLLM**. |
| `src/gateway/mock.py` | 50 | **100% Custom** | Scripted response FIFO queue for zero-cost offline test execution. |
| `src/tui/app.py` | 440 | **Library-Driven** | Custom multi-pane dashboard layout and event stream built on the open-source **`rich`** toolkit. |
| `src/tui/keyboard.py` | 210 | **100% Custom** | Non-blocking terminal keyboard controller (`msvcrt`/`select`) for live pause, inline steering, diff inspection, and clean exit. |
| `src/server/bridge.py` | 88 | **100% Custom** | WebSocket client queue broadcaster and operator-steering ingestion pipeline. |
| `main.py` | 174 | **100% Custom** | CLI argument parser, demo sprint driver, and live lifecycle runner. |
| `tests/*.py` (9 files) | 608 | **100% Custom** | 27 automated unit and integration tests written using standard library `unittest`. |

*Summary*: 100% of the repository's lines were written specifically for this codebase, while ~35% of the architectural patterns were adapted from proven open-source solutions. Runtime dependencies include `rich`, `pydantic`, `litellm`, `python-dotenv`, `fastapi`, and `uvicorn`. The web studio uses `react`, `react-dom`, `vite`, and `typescript`.

---

## 7. Roadmap & Next Milestones

| Milestone | Objective | Target Module | Status |
| :--- | :--- | :--- | :---: |
| **Phase 1** | Contained Multi-Model CLI with Rich TUI | `src/tui/app.py` | *(Delivered)* |
| **Phase 2** | React + Vite Split-Pane Web Collaboration Studio | `frontend/` + `src/server/bridge.py` | *(Delivered)* |
| **Phase 3** | Docker / MicroVM Container Driver | `src/sandbox/docker_runner.py` | *(Delivered)* |
| **Phase 4** | Automated Conventional Git Branching & Merging | `src/sandbox/git_manager.py` | *(Delivered)* |
| **Phase 5** | Product Owner Epic Decomposition, Self-Healing Refactor Loop & Terminal Keyboard Controller | `src/agile/backlog.py`, `src/agile/pair_engine.py`, `src/tui/keyboard.py` | *(Delivered)* |

---

## 8. License & Authors

* **Lead Architect**: Aarik Wells
* **License**: MIT
