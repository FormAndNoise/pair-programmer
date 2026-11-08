# Security Remediation Document — Pair Programmer

**Date:** 2026-11-06
**Source review:** full codebase security & correctness review (this session).
**Audience:** implementing engineer.
**Rule for every fix:** preserve existing behavior/tests unless the fix changes the contract; then update tests + AGENTS.md. Run `python -m unittest discover tests` (with `PYTHONPATH=src`) after each phase. Do not add new dependencies without a lockfile entry.

Status key: `[ ]` todo · `[x]` done. Severity: **C**ritical / **H**igh / **M**edium / **L**ow.

---

## Phase 0 — Quick wins (do first, ~1 hour)

### 0.1 Loopback-only web defaults + bind warning — H
- `main.py:223-224,301`: default `--web-host 127.0.0.1` (keep). If resolved host is not loopback (`127.0.0.1`/`localhost`/`::1`), print a loud warning and require an explicit `--insecure-remote` flag to proceed.
- `src/server/bridge.py`: narrow CORS: `allow_methods=["GET","POST"]`, `allow_headers=["Content-Type","Authorization"]` (drop `["*"]`). Keep `allow_credentials=False`.
- DONE WHEN: `--web --web-host 0.0.0.0` prints warning and refuses without `--insecure-remote`; CORS preflight for `DELETE` fails.

### 0.2 Git secret exclusion + branch validation — H
- `src/sandbox/git_manager.py:28-33,206-223`: add `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `*.pfx` to `DEFAULT_EXCLUDES`; commit a `.gitignore` inside workspace listing the same set.
- `git_manager.py:204,274,334-335`: validate `base_branch` against `^[A-Za-z0-9][A-Za-z0-9._/-]*$`, reject leading `-` and `..`; pass as `refs/heads/<branch>` where the API allows.
- DONE WHEN: test writes `.env` + `foo.pem` in workspace, runs story commit, asserts neither is in `git ls-files`.

### 0.3 Environment scrubbing order — H
- `src/sandbox/runner.py:44-72,148-152` and `docker_runner.py` equivalent: merge `extra_env` **first**, then scrub. Extend denylist: `DATABASE_URL`, `*_DSN`, `*_PRIVATE_KEY`, `*_KEY`, `*_CREDENTIALS`, `AWS_SESSION_TOKEN`, `GOOGLE_*`, `AZURE_*`, `SLACK_*`, `*_AUTH*`, `*_PASS*`. Prefer allowlist long-term: keep a small `ALLOWED_ENV` set (PATH-safe vars, LANG, LC_*, HOME, TMPDIR).
- Block loader re-injection regardless of denylist: strip `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `NODE_OPTIONS`, `GIT_CONFIG_*` after merge.
- DONE WHEN: test passes `extra_env={"ANTHROPIC_API_KEY":"x","PATH":"/tmp/evil"}` and asserts both absent from child env.

### 0.4 Rich/markup + terminal escape sanitization — M
- Add `src/agent_core/sanitize.py:strip_ansi(text)` removing `[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]` and `ESC[...`/OSC sequences. Apply in `runner._sanitize_and_truncate`, `tui/app.py` before storing dialogue/tool payloads, and `events.py` `print` calls.
- `main.py:246-248,299`, `tui/app.py:271,320-321,386-392`: never interpolate user/LLM strings into Rich markup; use `Text(...)` objects or `console.print(..., markup=False)`.
- DONE WHEN: test feeds `"\x1b]0;pwned\x07[red]x[/]"` through TUI event handler; rendered `Text.plain` contains no ESC, and console output contains literal `[red]`.

### 0.5 CI hardening — M
- `.github/workflows/ci.yml`: add `permissions: contents: read`; SHA-pin `actions/checkout`, `setup-python`, `setup-node` (resolve current SHAs); add `concurrency` cancel-in-progress; add `pip-audit` + `npm audit --audit-level=high` steps (continue-on-error with issue filing unless clean).
- Commit `frontend/package-lock.json` (`npm install --package-lock-only`) so `npm ci` works; add `requirements.lock` or pin in `pyproject.toml` / commit `uv.lock`. Until locks exist, pin ranges with `==` for `litellm`, `httpx`, `fastapi`.
- `.gitignore`: add `.env.production`, `.env.development`, `*.pem`, `*.key`.
- DONE WHEN: CI green; `npm ci` succeeds from clean checkout.

### 0.6 Small correctness bugs — L/M (batch)
- `src/tools/primitives.py:41-42`: `limit=None` sentinel (not falsy check); reject negative `offset`/`limit` with `ToolResult(is_error=True)`.
- `src/agent_core/types.py:147-148`: `SprintBacklog.is_complete` requires `len(stories) > 0 and all(s.dod.is_complete ...)`, not just `status == DONE`.
- `src/agent_core/types.py:161-165`: `Field(..., gt=0, le=<sane>)` for `max_turns_per_story`, `max_refactor_attempts`, `timeout_seconds`, `max_output_chars`; `current_index: int = Field(0, ge=0)`.
- `src/agent_core/types.py:39,120,135`: full `uuid4().hex` IDs (drop `[:8]`/`[:4]` truncation).
- `main.py:82-83`: parse `SANDBOX_TIMEOUT_SECONDS`/`SANDBOX_MAX_OUTPUT_CHARS` with bounds check; invalid → fail startup with clear message (no `ValueError` traceback). Same in `main.py:39,54` for `SANDBOX_MODE`: unknown value → exit, do not silently fall back to local.
- `main.py:84-96`: demo path must call `_build_runner()` (currently ignores `SANDBOX_MODE`).
- DONE WHEN: unit tests cover each; `python main.py --help` still works.

---

## Phase 1 — Authentication & WebSocket origin (Critical, ~half day)

### 1.1 Token auth on the FastAPI app — C
- `src/server/bridge.py:100-246`: require `Authorization: Bearer <token>` on every `/api/*` route and on WS upgrade. Token from env `PAIR_PROGRAMMER_TOKEN` (generate/persist to `~/.pair_programmer/token` with 0600 on first run if unset). Use `hmac.compare_digest`. Return `401` with no event/payload data.
- Frontend (`frontend/src/App.tsx`): prompt for token once, store in `sessionStorage`, and send the bearer header on every fetch. Browsers cannot set WebSocket headers, so send the token in a WebSocket subprotocol. Do not put the token in the URL.
- DONE WHEN: unauthenticated `curl /api/sprint/start` → 401; WS upgrade without token → 403; existing tests updated with token fixture.

### 1.2 Origin validation (CSWSH/CSRF) — C
- `bridge.py` WS handler (`:234-236`): reject upgrade unless `Origin` header is absent (non-browser client) or in the CORS allowlist. Same check for state-changing POSTs (`:175,205,213,221,229`) — reject mismatched `Origin` even with valid token (defense in depth).
- DONE WHEN: `evil.com` page opening `ws://127.0.0.1:8000/ws/events` gets 403; cross-origin POST to `/api/sprint/stop` gets 403.

### 1.3 DoS limits — H
- `bridge.py`: cap concurrent WS connections (e.g. 10); per-IP rate limit on REST (e.g. slowapi or simple token bucket, 60/min). Cap `EventBus._history` (`events.py:71,85`) as ring buffer (e.g. last 1000 events). Cap `get_recent_events` return size.
- DONE WHEN: opening 20 WS clients closes the 11th; history memory constant under 10k-event burst.

---

## Phase 2 — Sandbox hardening (Critical/High, ~1 day)

### 2.1 Docker as default; non-root + read-only — C/H
- `main.py` + `.env.example:12`: `SANDBOX_MODE` default → `docker`. `local_process` requires explicit `--unsafe-local` flag and prints a banner. `DockerSandboxRunner` (`docker_runner.py:224-250`): add `--user 65534:65534`, `--read-only`, `--tmpfs /tmp`, `--init`, `--memory-swap` = memory, `--storage-opt size=2g`, `--ulimit nofile=1024`, `--ulimit nproc=128`.
- `SANDBOX_IMAGE`: require digest (`image@sha256:...`) when `SANDBOX_REQUIRE_DIGEST=1` (default in CI); log the resolved digest at startup.
- DONE WHEN: container process runs as uid 65534; `touch /workspace/x && rm /workspace/x` works but `touch /etc/x` fails; oversized write fails with disk quota.

### 2.2 Container lifecycle — H
- `docker_runner.py:131,252`: on timeout, `docker kill` + `docker rm -f <name>` and verify exit; on startup, reap orphaned `pp-sandbox-*` containers. Wire `_ensure_image_available()` into `run_command` (currently dead code, `:191`).
- Bounded output: stream/reap with a cap (e.g. read at most `max_output_chars * 4` bytes from the pipe, then kill) instead of `capture_output` unbounded (`docker_runner.py:129`, `runner.py:160-167`).
- DONE WHEN: test runs `yes` with timeout; host RSS stays bounded; `docker ps --filter name=pp-sandbox` empty after timeout.

### 2.3 Local runner containment (defense in depth) — H
- `runner.py:138-165`: replace `shell=True` with `shlex.split(command)` argv execution (or restrict to a server-side allowlist of commands). Never pass raw LLM strings to a shell. Set `stdin=subprocess.DEVNULL`. Use `text=True, errors="replace"`. Validate `timeout_seconds`: `0`/negative → error; clamp to `[1, 600]`.
- `runner.py:198-211`: name says sanitize — actually strip ANSI (reuse Phase 0.4 helper) and cap `max_output_chars` in `__init__` to `[100, 100_000]`.
- DONE WHEN: `run_command("rm -rf /tmp/x; curl evil")` runs as argv (no shell metacharacter expansion); binary stdout returns replacement chars, not an exception.

### 2.4 TOCTOU / symlink races — M
- `primitives.py:76-87,115-151` + `runner.resolve_path`: open files with `os.open(..., os.O_NOFOLLOW)` relative to a workspace `dir_fd`, or re-validate `resolve_path` after open via `fstat`. `list_dir` must not follow symlinked dirs (`followlinks=False`) and must handle `stat()` errors per-entry instead of aborting.
- DONE WHEN: symlink swap between check and open cannot escape workspace (test with racing writer or O_NOFOLLOW flag assertion).

---

## Phase 3 — Data integrity & DoD (High, ~half day)

### 3.1 Pydantic invariants — H/M
- `types.py`: add `model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)` to all models; `min_length=1` on `name`, `call_id`, `title`, `epic_goal`, model names; `max_length` on `content` (e.g. 50k), `acceptance_criteria` item (e.g. 2k), `ToolResult.output`. `Message.timestamp`/`execution_time_ms`: `ge=0`, `allow_inf_nan=False`; set timestamp server-side.
- `Message.to_provider_format (:63-90)`: raise `ValueError` when `role==TOOL` and `tool_result is None` (instead of silent fall-through).
- DONE WHEN: fuzz-ish tests assert extra fields rejected, NaN timestamp rejected, oversized content rejected.

### 3.2 Tool result correlation & atomic writes — M
- `primitives.py`: remove hardcoded `call_id=""`; thread the real ID through `ToolRegistry.dispatch`. Write files atomically (tempfile + `os.replace`); cap `write_file`/`patch_file` content (e.g. 1 MB) and `read_file` size; `patch_file` must reject binary/CRLF-mangling or document text-only.
- `primitives.py:run_command (:242-256)`: wrap in try/except → `ToolResult(is_error=True)`; pass an explicit timeout always.
- DONE WHEN: every `ToolResult` carries the originating `call_id`; concurrent `write_file` never leaves a truncated file.

### 3.3 Review/DoD honesty — H
- `pair_engine.py:396-411`: tighten approval detection (word-boundary `LGTM`, keep negation filter but extend window); require approval to come from `test_model` (assert `source_model` matches role). `run_sprint (:344-358)`: set `story.status = DONE` on success; emit `STORY_STATUS_CHANGE` events for both outcomes.
- `backlog.verify_dod`: run DoD with Docker runner only; pin the test files it runs (hash `tests/` at RED completion) so GREEN cannot modify tests post-hoc; also run a harness-owned smoke test outside agent-writable `tests/`.
- DONE WHEN: test mutates `tests/` in GREEN and asserts DoD fails; test with reviewer = implementer asserts no approval.

---

## Phase 4 — Concurrency & error handling (High/Medium, ~half day)

### 4.1 EventBus thread safety — H
- `events.py:83-103`: add `threading.Lock` around `_history` mutation/iteration and handler registry. Replace `asyncio.create_task` inside `publish` with `loop.call_soon_threadsafe` capture at subscribe time; if no running loop, queue and schedule later instead of raising into a `print`.
- `bridge.py:20-31`: remove foreign-thread fallback enqueue; if `call_soon_threadsafe` fails, drop + log with counter. Guard `_connected_queues`/`_queue_loops` with a lock shared by publisher (any thread) and connect/disconnect (loop thread).
- DONE WHEN: stress test (4 publisher threads × 1k events + connect/disconnect churn) → no `RuntimeError`, no lost-handler crashes, monotonic history length.

### 4.2 Engine state — M
- `pair_engine.py:62-100`: guard `_paused`/`_stop_requested`/`_steering_guidance` with a `threading.Event`/`Lock`; replace busy-wait `time.sleep(0.05)` pause loop with `Event.wait()` so stop is interruptible. `bridge.py` must not touch `engine._stop_requested` privates — add public `engine.request_stop()` / `engine.is_running` and use those.
- `run_sprint` return: distinguish `STOPPED` vs `FAILED` (return enum or raise typed exception) so callers and TUI can render correctly.
- DONE WHEN: steering during pause doesn't lose notes; stop during a blocked gateway call terminates promptly.

### 4.3 Gateway — M
- `client.py:105-133,204-232,281-310,416-446`: retry only on 429/5xx/timeout; no retry on 400/401/403/404; add jitter; cap total wait. If API key env var missing, raise a clear error before sending (never send empty `Authorization`). Redact provider error bodies in exceptions (truncate, strip anything matching key patterns). Case-normalize provider key lookup (cache `provider in self.api_keys` check uses raw case).
- DONE WHEN: mocked 401 → exactly 1 attempt; missing key → immediate named error, no HTTP call.

### 4.4 Bridge error surface — M
- `bridge.py:166-173,241-242`: log sprint failures server-side (stderr), keep 202 semantics but include `run_id` in response so clients can correlate; wrap WS `send_text` in try/except → close connection cleanly. No stack traces to clients (generic message + run_id).
- DONE WHEN: failing sprint logs full traceback server-side; client receives only `run_id` + generic error.

---

## Phase 5 — TUI & keyboard (Medium, ~half day)

### 5.1 Terminal safety — M
- `tui/keyboard.py:84-117`: register `atexit` + `SIGINT`/`SIGTERM` handlers restoring termios/tty settings. Stop poll thread before restoring (proper `join()` without timeout race). Cap steering buffer (e.g. 1000 chars); ignore `ESC[...` escape sequences (arrow keys etc.) instead of treating bare `ESC` as resume. Fix `select()` + buffered `read(1)` mixing: use `os.read(fd, 1)` / `sys.stdin.buffer.raw`.
- `tui/app.py:53-59`: cap `stories` list growth; bound `vm_logs` already good (≤6).
- DONE WHEN: SIGINT from TUI leaves shell usable (echo restored); pasting 5000 chars caps at 1000; arrow keys don't trigger resume.

---

## Phase 6 — Documentation & regression sweep

- [ ] Update `AGENTS.md` (sandbox default, auth token, new env vars, test count 122).
- [ ] Update `README.md` (docker default, `--unsafe-local` warning, token setup).
- [ ] Add `docs/SECURITY.md`: threat model, trust boundaries, what is/isn't protected (local mode = no protection; DoD = agent-written tests unless Phase 3.3 lands).
- [ ] Full test run + demo sprint: `python main.py --mock` and `python main.py --mock --docker` both pass.
- [ ] Manual verification checklist: unauthenticated API → 401; evil.com WS → 403; `.env` not committed; container uid ≠ 0; `yes` command bounded; SIGINT restores terminal.

---

## Out of scope / accepted risk (document, do not fix now)

- Prompt injection via story/epic text is inherent to an LLM coding agent; mitigations are sandboxing + auth, not elimination. Ensure `--mock` demo wording never implies safety for `local_process`.
- `OPENAI_BASE_URL`/other provider env overrides remain a feature; document that setting them redirects credentials.
- Exact dedupe between `docker_runner` and `runner` truncation logic — extract shared helper when touching both (Phase 2.2/2.3).

## Effort estimate

| Phase | Finding count | Effort |
|---|---|---|
| 0 Quick wins | 6 | ~1h |
| 1 Auth + Origin | 3 | ~0.5 day |
| 2 Sandbox | 4 | ~1 day |
| 3 DoD/integrity | 3 | ~0.5 day |
| 4 Concurrency/gateway | 4 | ~0.5 day |
| 5 TUI/keyboard | 2 | ~0.5 day |
| 6 Docs/sweep | — | ~2h |
| **Total** | **22** | **~3–4 days** |
