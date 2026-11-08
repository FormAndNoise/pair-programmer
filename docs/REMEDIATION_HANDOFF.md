# Code Remediation Hand-off — Pair Programmer

**Date:** 2026-11-08
**Source audit:** full codebase audit completed 2026-11-08 (134 tests passing, ~64s).
**Prior remediation doc:** `docs/SECURITY_REMEDIATION.md` (Phases 0-6, 2026-11-06, commits `37c4392`, `b7516db`, `e70e9ac`).
**Audience:** implementing engineer with no prior context on this audit.
**Test command:** `python -m unittest discover tests` with `PYTHONPATH=src` (PowerShell: `$env:PYTHONPATH="src"; python -m unittest discover tests`) — expected: 134 tests, OK; skips vary by host capabilities (for example, Docker availability and POSIX-only terminal tests).
**Rule for every fix:** preserve existing behavior/tests unless the fix changes the contract; then update tests + `AGENTS.md`. Do not add new dependencies without a lockfile entry.

Status key: `[ ]` todo · `[x]` done. Severity: **C**ritical / **H**igh / **M**edium / **L**ow.

---

## What was already fixed (prior remediation, do not re-do)

| Area | Status |
|---|---|
| Docker as the live-mode default sandbox, `--user 65534`, `--read-only`, `--tmpfs /tmp`, resource caps (512m/CPU/pids), digest requirement flag, container cleanup, bounded output | Done (Phase 2); mock mode's local default is covered by R-01 |
| Token auth on `/api/*` + WS, `hmac.compare_digest`, token file `0600` `O_EXCL`, Origin validation, CORS limited to two localhost origins, REST rate limit | Done (Phase 1) |
| Loopback-only web defaults, partial Git secret exclusions (see R-12), environment scrubbing rules, ANSI/Rich markup sanitization | Done (Phase 0) |
| DoD anti-shadowing for selected stdlib modules, harness-owned smoke check, test SHA-256 pinning (`tests/**` only — see R-07), atomic file writes, unique-snippet `patch_file` | Done (Phase 3 + `e70e9ac`), with limits described below |
| EventBus thread-safety (lock + snapshot dispatch), bridge queue bounds, TUI terminal-keyboard hardening, CI hardening | Done (Phase 4-5) |
| Docs: `AGENTS.md`, `README.md`, `docs/SECURITY.md` updated | Done (Phase 6) |

This hand-off covers **only the new findings from the 2026-11-08 audit** that are not already closed above.

---

## P0 — Sandbox containment (do first)

### [x] R-01 `run_command` escapes sandbox in local mode — **C** (Completed 2026-11-08)
- **File:** `src/sandbox/runner.py:238-427`
- **Problem:** `SandboxRunner.run_command` confines file primitives (`resolve_path`) but not subprocesses. `--unsafe-local` runs commands with host access. The mock demo also selects local execution by default unless `--docker` is passed, even when `SANDBOX_MODE=docker` is set. A command can read or change host files and return their contents through captured output.
- **Fix:** Keep local execution for trusted demos only, and make that mode explicit in help and the startup banner. For any model-controlled code, require Docker or another OS sandbox. Decide whether `--mock` remains a trusted local demo or changes to require `--docker`; align `main.py`, `AGENTS.md`, and `README.md` with that contract.
- **Done when:** live model-controlled work cannot select `local_process` without an explicit unsafe opt-in; startup output clearly states when commands have host access; the mock mode's sandbox choice is documented and tested. A Docker regression test verifies that a child write stays inside the mounted workspace.
- **Tests:** add `tests/test_sandbox.py` case for local-mode refusal + banner.

### [x] R-02 Env scrubbing incomplete for local child — **H** (Completed 2026-11-08)
- **File:** `src/sandbox/runner.py:78-112,368-389`
- **Problem:** The child environment is copied from the parent and only selected entries are removed. Provider credentials and configuration, SSH/Git helpers, loader variables, and user-controlled paths can reach local subprocesses.
- **Fix:** Build a platform-aware allowlist for child commands. Preserve only required system variables (including `SYSTEMROOT`, `WINDIR`, `COMSPEC`, `PATHEXT` on Windows), a controlled `PATH`, locale settings, and workspace-local `HOME`/temporary directories. Explicitly exclude credentials, loader, pager, editor, SSH, Docker, and provider configuration. Reuse the policy in `git_manager.py` (see R-06).
- **Done when:** tests verify both the allowed platform variables and the excluded sensitive variables in the child environment.
- **Tests:** extend `tests/test_sandbox.py` env-scrub test.

### [x] R-03 TOCTOU symlink race in tool primitives — **H** (Completed 2026-11-08)
- **File:** `src/tools/primitives.py:23-38,58-85,135-159,213-279`
- **Problem:** Check-then-use between `_reject_symlink`/`resolve_path` and `open`/`mkdir`/`os.replace`. `patch_file` lacks the second `_reject_symlink` after read and lacks the tmp-file confinement check that `write_file` has at `:156`. A background process from `run_command` swaps a parent dir for a symlink to `/etc`/host between check and write.
- **Fix:** Walk each path component from an already-open workspace directory using directory-relative operations and no-follow flags where supported. Use directory-relative open/stat/create/unlink and atomic replacement; provide a safe platform-specific fallback where `dir_fd` or `O_NOFOLLOW` is unavailable. `fstat` alone cannot prove path containment. Add the missing post-read symlink check in `patch_file` as defense in depth.
- **Done when:** a race test that swaps an ancestor for a symlink cannot read or write outside the workspace on supported platforms; platform fallbacks have explicit tests and documented limits.
- **Tests:** add a TOCTOU regression test in `tests/test_tools.py`.

---

## P1 — DoD / test integrity (the harness must not be fooled by the agent it grades)

### [x] R-04 DoD harness trust boundary weak — **H** (Completed 2026-11-08)
- **File:** `src/agile/backlog.py:229-294`, `src/agile/ux_evaluator.py:22-26`
- **Problem:** `build_sandbox_python_env()` adds workspace `src` and `.` to `PYTHONPATH`, so DoD imports can resolve to workspace code. The shadow check covers selected filenames, while implementation code and tests run in the same interpreter as parts of the harness. Code under test can influence in-process test results. A workspace file named `*.pth` is not necessarily processed just because it is on arbitrary `PYTHONPATH`; the larger risks are import-path control, startup hooks in site directories, stdlib shadowing, and same-process monkeypatching.
- **Fix:** Separate trusted harness imports and acceptance checks from workspace imports. Use an isolated interpreter configuration for harness-owned gates, and explicitly add only the workspace paths needed to discover code under test. Do not treat `-I`, `-S`, a read-only mount, or pinned files as proof that adversarial code executed in the same process cannot spoof its own test results. Keep independent harness-owned checks for important behavior; describe test results as signals rather than a security proof against malicious implementation code.
- **Done when:** tests show trusted harness imports resolve from the harness, unsafe caller commands are rejected, and the harness-owned acceptance result does not depend on workspace stdout or replaceable test-runner objects. Document the remaining limit that arbitrary code executed in-process can influence its own test run.
- **Tests:** extend `tests/test_agile.py` DoD cases.

### [x] R-05 UX gate degenerates — **H** (Completed 2026-11-08)
- **File:** `src/agile/ux_evaluator.py:81-105,119-156`
- **Problem:** If the contract does not match func/CLI regexes (or CLI is deemed unsafe), the evaluator falls back to running the test suite and passes on test success (`passed = exec_res.is_success and ran_any`). Args/expected are interpolated into executed Python (`f"actual = {func_name}({args_str}); expected = {expected_raw}"`) — contract author gets arbitrary code exec in the sandbox and can fabricate expected values. First matching `def func(` in sorted `rglob` can be a stub file.
- **Fix:** Define and document an explicit contract grammar with a required module/function or CLI target. Fail closed when the contract does not match it. Parse arguments and expected values as literals (`ast.literal_eval`); never interpolate contract text into Python source. Resolve the named module deterministically and reject ambiguous or missing targets.
- **Done when:** an unrecognized contract with passing tests fails the UX gate; contract input containing executable Python is rejected and never executed.
- **Tests:** add cases in `tests/ux_simulation/test_outside_in_evaluator.py`.

### [x] R-06 Git env injection — **H** (Completed 2026-11-08)
- **File:** `src/sandbox/git_manager.py:396-409`
- **Problem:** `_env()` copies `os.environ` and removes only selected repository-location variables. Git and SSH helper variables such as `GIT_SSH_COMMAND`, askpass, external diff, pager/editor, `GIT_CONFIG_*`, and tracing options can alter Git behavior or invoke external programs.
- **Fix:** Build a minimal platform-aware environment for Git, remove inherited `GIT_*` and askpass overrides, and pass required configuration only through harness-owned arguments. Use `os.devnull` for the global config path on every platform; leave `GIT_SSH_COMMAND` unset unless the feature needs it.
- **Done when:** test sets `GIT_SSH_COMMAND=touch /tmp/pwned` in parent env, runs `GitManager.commit`, asserts `/tmp/pwned` was not created.
- **Tests:** extend `tests/test_git_manager.py`.

### [x] R-07 Test pinning gaps + caller-controlled DoD command — **M** (Completed 2026-11-08)
- **File:** `src/agile/backlog.py:180-195,264-291`
- **Problem:** Pinning covers selected test paths and may miss files that affect discovery. A missing hash snapshot must not silently disable verification. `test_command` accepts arbitrary caller input; this is a caller/API misuse risk unless exposed to model-controlled input. A command such as `python -c "print(1)"` can return success without running tests. The standard-library unittest runner does not provide JUnit XML output.
- **Fix:** Pin the actual discovered test files and discovery configuration used by the gate; fail closed when the snapshot is missing. Remove arbitrary `test_command` input or accept only a documented, exact allowlist of unittest invocations. Prefer a harness-owned result object or a strict runner contract over parsing success text. Keep this finding scoped to callers unless the command becomes model-accessible.
- **Done when:** changing or removing a discovered test/config file changes the pinned snapshot and fails verification; a no-op Python command is rejected before execution as a test command.
- **Tests:** extend `tests/test_agile.py`.

### [x] R-08 LGTM check bypassable + reviewer prompt injection — **M** (Completed 2026-11-08)
- **File:** `src/agile/pair_engine.py:473-494,513-527`
- **Problem:** The current standalone-token check can accept approval language followed by caveats, punctuation, or quoted/code-block text. The reviewer receives implementation-controlled content, which can contain prompt injection. Structured output can improve parsing but cannot make a model immune to untrusted prompt content.
- **Fix:** Parse a strict structured verdict and reject missing or malformed output. Clearly delimit untrusted implementation text and provide only the diff and bounded verification output needed for review. Treat the model verdict as a review signal; retain deterministic DoD gates and do not claim prompt-injection resistance.
- **Done when:** malformed or missing verdicts fail closed; tests cover common caveat/quotation cases. Document that a valid structured verdict still cannot prove resistance to all prompt injection.
- **Tests:** add cases in `tests/test_agile.py` / `tests/test_e2e_mock_sprint.py`.

---

## P2 — Secrets, supply chain, and web

### [x] R-09 Docker image / mount hardening — **M** (Completed 2026-11-08)
- **File:** `src/sandbox/docker_runner.py:67,290-319,349,373`
- **Problem:** Default image `python:3.11-slim` is mutable; digest pinning is opt-in. Host paths are interpolated into Docker volume arguments, and Docker runs `sh -c` while local execution uses `shell=False`.
- **Fix:** Require or configure a verified image digest for production use. Validate and encode workspace paths with Docker's supported mount syntax, including Windows paths. Keep the current `docker run` lifecycle unless deliberately redesigning it; if command arguments are introduced, validate and pass them to that lifecycle. Align and document command-string semantics across local and Docker runners.
- **Done when:** configured production images are digest-pinned; tests cover Windows and POSIX mount paths, workspace-root rejection, and equivalent documented command behavior.
- **Tests:** add validation tests in `tests/ux_simulation/test_docker_runner.py`.

### [x] R-10 WebSocket rate and token-handling gaps — **M** (Completed 2026-11-08)
- **File:** `src/server/bridge.py:81-83,239-250,424-456`, `frontend/src/App.tsx:62-71,199-202,212-220`
- **Problem:** Missing `Origin` passes the Origin check, which is appropriate for non-browser clients but does not bypass required bearer-token authentication. The Origin check protects browser contexts from cross-site WebSocket use; it is not client authentication. The bearer token in the WebSocket subprotocol may be logged, and WebSocket authentication attempts are not rate-limited. The frontend requests status on each WebSocket message.
- **Fix:** Add a WebSocket handshake/auth rate limit and redact the bearer subprotocol from logs. Debounce/coalesce `/api/status` calls. If returning HTTP 429 before WebSocket acceptance, implement and test an ASGI denial response supported by the selected server; a pre-accept close is not necessarily an HTTP 429. Keep token storage and XSS exposure documented.
- **Done when:** rapid failed handshakes are limited with an explicit denial response; the frontend makes at most one status request per two seconds.
- **Tests:** extend `tests/test_server_bridge.py` with WS-handshake rate-limit case.

### [x] R-11 `OPENAI_BASE_URL` credential redirect — **M** (Completed 2026-11-08)
- **File:** `src/gateway/client.py:199-201,488-493`
- **Problem:** `OPENAI_BASE_URL` overrides the OpenAI-compatible endpoint, and the API key is sent to that URL. The risk comes from a compromised or operator-controlled launcher environment; a child process cannot directly change its parent's environment.
- **Fix:** Validate the configured endpoint against supported provider hosts and explicitly opt in to custom endpoints, including local LLMs. Never send credentials to an unexpected host.
- **Done when:** setting `OPENAI_BASE_URL=https://evil.example/v1` → `send_chat` raises before any HTTP call.
- **Tests:** add cases in `tests/test_gateway.py`.

### [x] R-12 Secret files committed by git — **M** (Completed 2026-11-08)
- **File:** `src/sandbox/git_manager.py:28-41,215-225,267`
- **Problem:** Exclusion patterns may miss common secret filenames and token files. Broad patterns such as `*token*` can also hide ordinary source files. A sandbox-controlled `.gitignore` can remove exclusions; `info/exclude` is separate and should not overwrite or replace existing project rules.
- **Fix:** Add targeted secret filename patterns and a staged-content scanner with documented exceptions. Keep harness-owned ignore rules separate from the project's `.gitignore`; preserve existing rules and scan staged content before committing.
- **Done when:** staged secret content is rejected; harness-owned exclusions remain active even if the workspace `.gitignore` changes.
- **Tests:** extend `tests/test_git_manager.py`.

### [x] R-13 Unpinned dependencies, no lockfile — **M** (Completed 2026-11-08)
- **File:** `pyproject.toml:12-20`, `.github/workflows/ci.yml`
- **Problem:** Python runtime and development dependencies use unpinned ranges, and there is no Python dependency lockfile. The frontend does have `frontend/package-lock.json`. CI currently runs dependency audits with `continue-on-error`, so findings do not fail the build.
- **Fix:** Choose and commit a Python lockfile or equivalent reproducible dependency policy. Decide explicitly whether high-severity audit findings should fail CI, then update the workflow and report that policy. Preserve the existing npm lockfile.
- **Done when:** a clean checkout can reproduce the Python environment from the committed lock; CI behavior for high-severity Python and npm audit findings matches the documented policy.
- **Tests:** none (CI-only); verify by running the install from scratch.

---

## P3 — Bugs and robustness

### [x] R-14 Pipe-drain thread leak / process escape / no local resource limits — **M** (Completed 2026-11-08)
- **File:** `src/sandbox/runner.py:132-235`
- **Problem:** Drain threads can remain blocked when a descendant inherits a pipe. POSIX process-group termination does not catch a descendant that creates a new session. Local execution has no CPU, memory, or process limits.
- **Fix:** Prefer disabling local execution for untrusted code (R-01). If local mode remains, use OS process containment such as cgroups on Linux and Job Objects on Windows; process-group IDs do not kill descendants that call `setsid()`. Use a drain strategy that can stop reliably, and join readers after contained writers exit. Closing a pipe from another thread may not wake a blocking read on every platform.
- **Done when:** timeout tests verify escaped descendants are contained on supported platforms and reader threads terminate. Document platform gaps if full containment is unavailable.
- **Tests:** add a grandchild/timeout case in `tests/test_sandbox.py`.

### [x] R-15 Unbounded message history; `list_dir` symlink + tool-output cap interactions — **M** (Completed 2026-11-08)
- **File:** `src/agile/pair_engine.py:548-610`, `src/tools/primitives.py:317-379`, `src/agent_core/types.py:46-54`
- **Problem:** Tool and message history can grow across many tool calls; there is no reliable total-size bound based only on the per-output limit and turn count. `list_dir` skips symlink traversal with `os.walk(..., followlinks=False)`, but its root and entry behavior should be made explicit. Large listings can exceed `ToolResult.output`'s 100,000-character limit and become a generic error.
- **Fix:** Set an aggregate history budget and compact older tool results. Define consistent symlink behavior for the requested root and child entries. Enforce an entry-count cap and truncate output before constructing `ToolResult`.
- **Done when:** a workspace with 50,000 files returns a marked, truncated listing; a many-turn story stays within a documented aggregate history budget.
- **Tests:** add `list_dir` cap + symlinked-dir cases in `tests/test_tools.py`.

### [x] R-16 REST rate-limiter pre-auth DoS; WS no idle timeout — **L** (Completed 2026-11-08)
- **File:** `src/server/bridge.py:252-272,424-456`, `frontend/src/App.tsx:212-220`
- **Problem:** The per-IP REST limiter runs before authentication, so unauthenticated traffic can consume the shared local IP bucket. The application has no explicit idle timeout around the WebSocket event wait; actual ping/pong behavior depends on the selected ASGI server and WebSocket backend.
- **Fix:** Separate unauthenticated and authenticated quotas or otherwise prevent pre-auth traffic from exhausting the authenticated allowance. Configure and verify WebSocket ping/pong and idle timeouts for the selected server/backend. Debounce frontend status polling as in R-10.
- **Done when:** unauthenticated traffic cannot starve an authenticated client, and an integration test verifies the selected server closes an idle or half-open WebSocket within the documented interval.
- **Tests:** extend `tests/test_server_bridge.py`.

### [x] R-17 Token-file race; sprint run-id divergence — **L** (Completed 2026-11-08)
- **File:** `src/server/bridge.py:24-73,335-393`
- **Problem:** Token creation uses exclusive creation and retries reading an empty file, but the retry window is bounded; a stalled writer or crash can still leave an empty/partial file. `mkdir(mode=0o700)` only sets permissions when creating a new directory. `run_sprint` and `/api/sprint/start` can report different run IDs. Concurrent start requests can race between checking task state and replacing sprint state. The resume branch inside `start_sprint` is unreachable while the existing task is still active because the task-state check returns first.
- **Fix:** Set restrictive permissions on an existing token directory where supported and test a writer paused between file creation and content write. Use one run-ID source. Serialize sprint start/state changes with a lock and make the active-task policy explicit; remove or relocate the unreachable resume branch.
- **Done when:** concurrent starts accept at most one request or cleanly supersede by policy, and the API response, events, and error reports use the same run ID.
- **Tests:** add a token-race case in `tests/test_server_bridge.py`.

### [x] R-18 `patch_file`/`write_file` size, byte-count, NUL inconsistencies — **L** (Completed 2026-11-08)
- **File:** `src/tools/primitives.py:186-304`
- **Problem:** `patch_file` limits the input/replacement but does not check the final UTF-8 byte size against `MAX_WRITE_BYTES`. `write_file` reports character count rather than bytes and accepts NUL characters, which can produce content that text reads cannot round-trip.
- **Fix:** Check the resulting UTF-8 byte length against the write cap, report bytes, and reject NUL in `write_file`.
- **Done when:** a patch whose result exceeds the 1 MB write cap returns an error; NUL writes fail; success messages report bytes.
- **Tests:** add cases in `tests/test_tools.py`.

### [x] R-19 Retry/redaction gaps in gateway — **L** (Completed 2026-11-08)
- **File:** `src/gateway/client.py:66-89,104-127,203-235`
- **Problem:** `_redact` recognizes only selected credential prefixes and may expose other provider secrets in error bodies. `_status_code_of` extracts numbers from exception text and can misclassify a message containing a status-like number. `max_retries=0` can reach an invalid loop exit.
- **Fix:** Redact known credential formats and authorization headers, and avoid logging full provider response bodies. Classify status only from typed HTTP exceptions/responses. Define zero-retry behavior explicitly and remove the unreachable-loop-exit path; avoid broad entropy heuristics unless false-positive handling is documented.
- **Done when:** error body containing `ghp_...`/`AKIA...`/`Bearer ...` → logged as `[REDACTED]`; mocked 400 with "404" in message → exactly 1 attempt; `max_retries=0` works (1 attempt, no crash).
- **Tests:** extend `tests/test_gateway.py`.

### [x] R-20 Local subprocess quirks; dead attribute; `--sandbox` validation — **L** (Completed 2026-11-08)
- **File:** `src/sandbox/runner.py:313-325`, `src/tools/primitives.py:392-421`, `main.py:51-72,266,297-311`
- **Problem:** `shlex.split(command, posix=True)` may mishandle Windows paths and quoting. `getattr(runner, "timeout_seconds", 60)` reads a missing attribute; runners expose `default_timeout`. `--sandbox` accepts filesystem roots or the user's home, where confinement can become ineffective. `_loopback_bind_host` resolves and validates loopback addresses before binding; the current all-loopback check prevents a DNS answer from selecting a non-loopback address, so a DNS TOCTOU claim is not supported.
- **Fix:** Prefer passing validated argument vectors end-to-end. If the command-string interface remains, define and test platform-specific parsing, including quoted paths, backslashes, and `python -c`. Use `runner.default_timeout`. Reject filesystem-root and home workspaces.
- **Done when:** Windows command parsing works for the documented cases; the primitive respects configured timeout; `--sandbox /` and the user's home directory fail with a clear error.
- **Tests:** add a Windows-path case (or a skip-on-POSIX guard) in `tests/test_sandbox.py`; a timeout-config case in `tests/test_tools.py`.

---

## Suggested order & effort

| Batch | Items | Effort |
|---|---|---|
| P0 sandbox containment | R-01, R-02, R-03 | Re-estimate after deciding the local-mode contract and path-walk design |
| P1 DoD integrity | R-04, R-05, R-06, R-07, R-08 | Re-estimate after defining the trusted harness boundary |
| P2 secrets/web/supply chain | R-09, R-10, R-11, R-12, R-13 | Estimate after platform and CI policy choices |
| P3 bugs/robustness | R-14, R-15, R-16, R-17, R-18, R-19, R-20 | Estimate after platform containment choices |
| **Total** | **20 items** | **Rough planning only; the prior 4–5 day estimate omitted cross-platform design and validation** |

Within each item, land the fix + its named test in the same commit; run the full suite after each batch.

## Out of scope / accepted risk (document, do not fix now)

- Prompt injection via story/epic text is inherent to an LLM coding agent; mitigations are sandboxing + auth, not elimination. Ensure `--mock` demo wording never implies safety for `local_process`.
- `OPENAI_BASE_URL` and other provider env overrides remain a feature; after R-11 they require explicit opt-in.
- Exact dedupe between `docker_runner` and `runner` truncation logic — extract a shared helper when touching both (R-09/R-14).

## Verified OK in the 2026-11-08 audit (no action)

- `resolve_path` confinement (`runner.py:254-286`, `docker_runner.py:100-127`): blocks static `..`, absolute, UNC/drive, and symlink escapes; race resistance remains open under R-03. `GitManager._assert_git_dir_outside_workspace` keeps `.git` outside the sandbox.
- Local execution uses `shell=False` with `shlex.split`; this avoids shell metacharacter interpretation but does not provide OS isolation. Windows tree-kill handling is present, with escaped descendants still covered by R-14.
- Output flood protection caps captured output and terminates on a byte threshold; reader-thread and process-tree cleanup still need the R-14 verification.
- Bridge: constant-time token compare, exclusive token-file creation with mode `0600`, restricted CORS, Origin checks, a 10-connection WebSocket cap, REST rate limiter with 4096-bucket LRU eviction, and request size limits. These controls do not resolve the WebSocket rate/idle and pre-auth quota findings above.
- `patch_file`: unique-snippet enforcement, NUL/binary rejection, and atomic temp-file replacement. Final output byte size still needs the R-18 check.
- Git: argv lists (`shell=False`), hook/signing overrides, `--no-verify`, Git config isolation options, branch slugification, and base-branch validation. Inherited Git environment variables still need the R-06 review.
- EventBus: history bounded to 1000 events with lock-protected snapshot dispatch; bridge per-client queues bounded at 500 with drop-oldest.
- TUI: all model/tool text through `strip_ansi` before rendering; no `Text.from_markup` on untrusted data; bounded buffers; keyboard ESC/CSI/OSC consumption prevents terminal injection.
- Frontend: no `dangerouslySetInnerHTML`/`eval`, React-escaped rendering, WebSocket URL derived from `window.location`, client event cap 300.
