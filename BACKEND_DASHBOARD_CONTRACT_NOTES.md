# Backend Requirements for the Dashboard Contract

**Status:** Requirements note only. No backend code has changed.

**Basis:** The pasted Agent Soul and JSON payload are the product contract for this work. Existing UX/UI project material was not used. The backend review covered the domain types, event bus, pair engine, API bridge, model gateway, sandbox runner, and file tools.

## What the backend already supports

- The API can start, pause, resume, and steer a sprint.
- `/api/status` returns a basic session and backlog summary. `/api/events` returns recent events. `/ws/events` streams new events.
- The engine emits story phase changes, model messages, review critiques, refactor iterations, and final DoD results.
- File tools use `SandboxRunner.resolve_path()` to keep agent writes inside the sandbox.

## Changes required for the pasted contract

1. **Define a versioned dashboard snapshot.** Add typed backend response models for the dashboard state, agent, workspace file, review history, and session metrics. Include a schema/API version and session identifier. Define the meaning of `loop_count`, `current_goal`, and every displayed status. Add a snapshot endpoint and send a current snapshot when a WebSocket client connects; an event stream alone cannot reliably rebuild current state after a disconnect.

2. **Make event delivery resumable.** Add an ordered event sequence or cursor and support replay after that cursor. The current bounded WebSocket queue drops its oldest event when full, without telling the client. Signal a resync when events are lost. Keep timestamps in one documented format.

3. **Add real streaming before exposing stream indicators.** The engine currently calls the gateway synchronously and emits `MODEL_MESSAGE` after the response returns. It does not emit token chunks, despite chunk event types existing. Add a streaming gateway interface and publish chunk events if the UI needs a live token or code cursor. Otherwise omit `active_stream_token` and report a non-streaming agent status.

4. **Collect model usage and session metrics.** Add provider usage metadata to gateway responses and aggregate input/output tokens, elapsed time, and cost per session. Some providers may not return usage; represent unavailable values as `null`. Calculate cost only when a known pricing entry exists. Use a monotonic clock for elapsed time.

5. **Publish authoritative workspace file state.** Add structured file-change events with sandbox-relative path, content or fetch reference, content revision/hash, and a structured diff. The current `write_file` event only reports tool output, and the current patch diff is embedded in text. Serve editor content from the sandbox as the source of truth. Treat the editor theme as a frontend preference, not backend state.

6. **Implement the human edit and resume lifecycle.** Current pause is cooperative: the engine checks it between model calls and verification steps. Represent `pause_requested` separately from `paused`, and report when the engine reaches a safe checkpoint. Record the story, phase, model role, and refactor attempt at that checkpoint. Add a paused-only edit endpoint that applies submitted content to the selected sandbox file, records the diff and human notes, and checks the expected file revision to reject stale edits.

   - `FORCE_AUDIT`: apply the human edit, skip the coder turn, then run review and every required DoD gate.
   - `RESET_TURN_CODER`: apply the edit and resume the implementer from the saved checkpoint.
   - `FORCE_PASS_COMPLETE`: this conflicts with the supplied project instruction that all DoD gates are mandatory. Do not let this strategy mark a story complete or bypass tests, review, or the UX audit. Remove this value from the accepted enum or redefine it as a non-completing human override.

7. **Persist structured auditor history.** `CRITIQUE_RECEIVED` currently carries only critique text; it has no story/iteration identity, approval result, or annotations. Store each review with reviewer model, story, iteration, timestamp, result, and any structured severity/line annotations. Tie line ranges to a file revision so annotations do not point at changed code. Do not claim annotations exist when the reviewer returned only plain text.

8. **Define trigger conditions in backend behavior.** The listed `TIME_LIMIT`, `DOCUMENT_LENGTH`, and `GOAL_CHECKLIST` triggers do not exist in the current engine. Define each trigger's measured value, threshold, percentage formula, and action when reached. Emit the current and limit values from the backend. If a trigger is unsupported or has no configured limit, return `null` rather than inventing values.

9. **Map current engine roles to the contract explicitly.** The engine uses Test Author, Implementer, and Reviewer concepts plus `DRIVER`, `NAVIGATOR`, and `REVIEWER` pair roles. Define how these map to `CODER`, `AUDITOR`, and `NONE_HUMAN_OVERRIDE`, and include stable model identifiers and display names. Map internal sprint states to `AUTONOMOUS_RUNNING`, `HUMAN_INTERVENTION`, `LOOP_HALTED_ERROR`, and `GOAL_COMPLETED` in one backend serializer.

10. **Validate intervention requests.** Use typed request models for the action and resolution enums. Do not trust a client-supplied file path or agent ID to select the target. Resolve the active file and agent from server state, enforce sandbox path confinement, bound content and note sizes, require the current session and file revision, and reject edits unless the engine is paused at an editable checkpoint.

11. **Add session recovery if restart recovery is required.** Current engine state, event history, and pause checkpoint live in memory. Persist the session snapshot and checkpoint if the dashboard contract promises recovery after a backend restart. WebSocket reconnect within the same process can use a snapshot plus event cursor.

12. **Protect control endpoints before remote access.** The current API has no authentication. Keep it bound to a trusted local interface or add session authorization and origin checks before allowing remote clients to pause a run or write code.

## Suggested implementation order

1. Finalize the state, role, trigger, and override semantics; resolve the `FORCE_PASS_COMPLETE` conflict.
2. Add typed snapshot and action models, then expose the snapshot endpoint.
3. Add session state/checkpoints and safe pause/edit/resume transitions.
4. Emit structured workspace and review events; add ordered replay and resync behavior.
5. Add streaming and usage collection only for fields the UI will display.
6. Add persistence and remote-access controls if those deployment modes are required.
