"""Bridge connecting EventBus to Web UI clients over WebSockets and REST."""

import asyncio
import hmac
import inspect
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from agent_core.events import AgentEvent, EventBus, EventType
from agent_core.types import SprintBacklog, UserStory


ALLOWED_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})
MAX_WS_CONNECTIONS = 10
MAX_RECENT_EVENTS = 300
REST_RATE_LIMIT = 60
UNAUTH_REST_RATE_LIMIT = 20
REST_RATE_WINDOW_SECONDS = 60.0
WS_HANDSHAKE_RATE_LIMIT = 10
WS_HANDSHAKE_WINDOW_SECONDS = 60.0
WS_IDLE_TIMEOUT_SECONDS = 60.0


def _ensure_private_dir(directory: Path) -> None:
    """Create directory if missing and enforce 0o700 permissions even when pre-existing."""
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        # Windows may not expose POSIX directory mode bits.
        pass


def _write_token_atomically(token_path: Path, token: str) -> None:
    """Write token to a sibling temporary file with 0o600 permissions and replace atomically."""
    _ensure_private_dir(token_path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=str(token_path.parent), prefix=".token-")
    tmp_path = Path(tmp_name)
    try:
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.replace(tmp_path, token_path)
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    finally:
        tmp_path.unlink(missing_ok=True)


def _load_or_create_auth_token(
    token_path: Optional[Path] = None,
    retry_attempts: int = 50,
    retry_delay: float = 0.01,
) -> str:
    """Load the configured token or create a private token file on first run."""
    if token_path is None:
        configured = os.environ.get("PAIR_PROGRAMMER_TOKEN", "").strip()
        if configured:
            return configured
        token_path = Path.home() / ".pair_programmer" / "token"

    _ensure_private_dir(token_path.parent)

    def read_existing() -> Optional[str]:
        try:
            content = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError(f"Cannot read API token file at {token_path}: {exc}") from exc
        if not content:
            return ""
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        return content

    existing = read_existing()
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    if existing is None:
        try:
            descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                token_file.write(token + "\n")
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass
            return token

    # Another writer created the file concurrently or left an empty file; poll briefly.
    for _ in range(max(1, retry_attempts)):
        current = read_existing()
        if current:
            return current
        time.sleep(retry_delay)

    # Stalled or crashed writer left an empty file past the retry window; recover atomically.
    _write_token_atomically(token_path, token)
    return token


def _constant_time_token_match(candidate: str, expected: str) -> bool:
    """Compare UTF-8 token values without a timing-sensitive string comparison."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _origin_is_allowed(origin: Optional[str]) -> bool:
    """Allow non-browser clients and configured local studio origins."""
    return origin is None or origin in ALLOWED_ORIGINS


def _consume_rate_bucket(
    buckets: "OrderedDict[str, tuple[float, float]]",
    lock: threading.Lock,
    key: str,
    limit: int,
    window_seconds: float,
) -> bool:
    """Consume 1 token from key's rate bucket under lock; return True if allowed."""
    now = time.monotonic()
    with lock:
        tokens, updated_at = buckets.get(key, (float(limit), now))
        tokens = min(
            float(limit),
            tokens + (now - updated_at) * limit / window_seconds,
        )
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        buckets[key] = (tokens, now)
        buckets.move_to_end(key)
        while len(buckets) > 4096:
            buckets.popitem(last=False)
    return allowed


def evaluate_rest_access(
    *,
    method: str,
    origin: Optional[str],
    authorization: str,
    remote: str,
    auth_token: str,
    rate_buckets: "OrderedDict[str, tuple[float, float]]",
    rate_lock: threading.Lock,
    is_preflight: bool = False,
) -> Tuple[int, Optional[Dict[str, str]], Optional[str]]:
    """Evaluate Origin, Bearer auth, and separated auth/unauth rate-limit buckets.

    Returns ``(200, None, None)`` when allowed, or ``(status_code, headers, detail)`` when denied.
    """
    if is_preflight:
        if not _origin_is_allowed(origin):
            return 403, None, "Origin not allowed."
        return 200, None, None

    if method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_is_allowed(origin):
        return 403, None, "Origin not allowed."

    scheme, separator, candidate = (authorization or "").partition(" ")
    is_authenticated = bool(
        separator
        and scheme.lower() == "bearer"
        and _constant_time_token_match(candidate.strip(), auth_token)
    )

    if is_authenticated:
        allowed = _consume_rate_bucket(
            rate_buckets,
            rate_lock,
            f"auth:{remote}",
            REST_RATE_LIMIT,
            REST_RATE_WINDOW_SECONDS,
        )
        if not allowed:
            return 429, {"Retry-After": "1"}, "Too many requests."
        return 200, None, None

    allowed_unauth = _consume_rate_bucket(
        rate_buckets,
        rate_lock,
        f"unauth:{remote}",
        UNAUTH_REST_RATE_LIMIT,
        REST_RATE_WINDOW_SECONDS,
    )
    if not allowed_unauth:
        return 429, {"Retry-After": "1"}, "Too many requests."
    return 401, {"WWW-Authenticate": "Bearer"}, "Authentication required."


def redact_ws_subprotocols(subprotocols: List[str]) -> List[str]:
    """Replace any bearer.<token> subprotocol entry with bearer.[REDACTED] for safe logging."""
    return [
        "bearer.[REDACTED]" if protocol.startswith("bearer.") else protocol
        for protocol in subprotocols
    ]


def evaluate_ws_handshake(
    *,
    scope: Dict[str, Any],
    origin: Optional[str],
    remote: str,
    auth_token: str,
    rate_buckets: "OrderedDict[str, tuple[float, float]]",
    rate_lock: threading.Lock,
) -> Tuple[bool, int, str, Optional[str]]:
    """Redact bearer subprotocols in scope, enforce WS handshake rate limits, and verify origin + token.

    Returns ``(allowed, close_code, close_reason, accepted_subprotocol)``.
    """
    offered_protocols = list(scope.get("subprotocols", []))
    scope["subprotocols"] = redact_ws_subprotocols(offered_protocols)

    if not _consume_rate_bucket(
        rate_buckets,
        rate_lock,
        f"ws:{remote}",
        WS_HANDSHAKE_RATE_LIMIT,
        WS_HANDSHAKE_WINDOW_SECONDS,
    ):
        return False, 1008, "Too many handshake attempts.", None

    if not _origin_is_allowed(origin):
        return False, 1008, "Origin not allowed.", None

    candidate = next(
        (protocol[len("bearer.") :] for protocol in offered_protocols if protocol.startswith("bearer.")),
        "",
    )
    if not _constant_time_token_match(candidate, auth_token):
        return False, 1008, "Authentication required.", None

    accepted = "pair-programmer" if "pair-programmer" in offered_protocols else None
    return True, 1000, "", accepted


async def stream_ws_events_with_idle_timeout(
    websocket: Any,
    queue: asyncio.Queue,
    idle_timeout: float = WS_IDLE_TIMEOUT_SECONDS,
) -> None:
    """Forward events from queue to websocket and close with 1000 if idle beyond idle_timeout."""
    while True:
        try:
            data = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
        except asyncio.TimeoutError:
            await websocket.close(code=1000, reason="Idle timeout exceeded.")
            break
        try:
            await websocket.send_text(json.dumps(data))
        except Exception:
            break


class SprintRunCoordinator:
    """Thread-safe sprint lifecycle coordinator ensuring a single unified run_id and serialized starts."""

    def __init__(self, bridge: "WebEventBridge", engine: Optional[Any]) -> None:
        self.bridge = bridge
        self.engine = engine
        self.session_status: str = "ready"
        self.sprint_task: Optional[asyncio.Task] = None
        self.current_run_id: Optional[str] = None
        self._lock = threading.Lock()

    def _invoke_engine_run_sprint(self, run_id: str) -> Any:
        try:
            sig = inspect.signature(self.engine.run_sprint)
            if "run_id" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                return self.engine.run_sprint(run_id=run_id)
        except (TypeError, ValueError):
            pass
        return self.engine.run_sprint()

    async def _run_sprint_async(self, run_id: str) -> None:
        if self.session_status != "paused":
            self.session_status = "running"
        try:
            completed = await asyncio.to_thread(self._invoke_engine_run_sprint, run_id)
            if getattr(self.engine, "stop_requested", False) or self.session_status == "stopped":
                self.session_status = "stopped"
            else:
                self.session_status = "complete" if completed else "failed"
        except Exception:
            import traceback

            traceback.print_exc()
            self.session_status = "failed"
            self.bridge.bus.publish(
                AgentEvent(
                    event_type=EventType.ERROR,
                    payload={
                        "error": "Sprint failed. See server logs.",
                        "stage": "sprint",
                        "run_id": run_id,
                    },
                )
            )

    async def start(self, epic: str, stories: List[UserStory]) -> Dict[str, Any]:
        """Atomically validate active task state, assign a single run_id, and launch the sprint task."""
        with self._lock:
            task = self.sprint_task
            if (task is not None and not task.done()) or self.session_status in {"starting", "running", "paused"}:
                raise RuntimeError("A sprint is already running.")

            if hasattr(self.engine, "reset_stop"):
                self.engine.reset_stop()
            self.engine.backlog_mgr.sprint = SprintBacklog(epic_goal=epic, stories=stories)
            run_id = uuid.uuid4().hex
            self.current_run_id = run_id
            self.session_status = "starting"
            self.sprint_task = asyncio.create_task(self._run_sprint_async(run_id))
            return {"status": "starting", "story_count": len(stories), "run_id": run_id}


class WebEventBridge:
    """Dispatches engine events to web client connections and accepts operator steering."""

    def __init__(self, bus: EventBus, max_connections: int = MAX_WS_CONNECTIONS) -> None:
        self.bus = bus
        self.max_connections = max(1, max_connections)
        self._connected_queues: Set[asyncio.Queue] = set()
        self._queue_loops: Dict[asyncio.Queue, Optional[asyncio.AbstractEventLoop]] = {}
        self._clients_lock = threading.RLock()

        # Wire event listener
        self.bus.subscribe_all(self._on_agent_event)

    def _on_agent_event(self, event: AgentEvent) -> None:
        """Forward an event to all connected web client queues.

        Publisher may run on an arbitrary (engine) thread, so delivery
        prefers loop.call_soon_threadsafe. Queues registered from a thread
        without a running loop (tests, CLI, TUI) use an unbound asyncio.Queue
        and are enqueued directly via put_nowait, which is thread-safe in
        that unbound case. Bound queues (real WebSocket clients) are never
        touched from a foreign thread. Failures drop the client.
        """
        data = self._serialize_event(event)
        with self._clients_lock:
            clients = [(q, self._queue_loops.get(q)) for q in self._connected_queues]
        for q, loop in clients:
            if loop is not None:
                if not loop.is_running():
                    continue
                try:
                    loop.call_soon_threadsafe(self._enqueue_event, q, data)
                except RuntimeError:
                    self.unregister_client(q)
            else:
                # Unbound queue: safe to put directly.
                self._enqueue_event(q, data)

    @staticmethod
    def _serialize_event(event: AgentEvent) -> Dict[str, Any]:
        return {
            "id": event.id,
            "type": event.event_type.value,
            "timestamp": event.timestamp,
            "source_model": event.source_model,
            "payload": event.payload,
        }

    def get_recent_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return the latest serialized events for a browser reconnect."""
        limit = max(0, min(limit, MAX_RECENT_EVENTS))
        if limit == 0:
            return []
        history = self.bus.get_history()
        return [self._serialize_event(event) for event in history[-limit:]]

    @staticmethod
    def _enqueue_event(queue: asyncio.Queue, data: Dict[str, Any]) -> None:
        """Keep a slow browser from blocking the engine's event publisher."""
        try:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(data)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass

    def register_client(self) -> Optional[asyncio.Queue]:
        """Register a client unless the configured connection cap is full.

        The loop is captured from the calling (event-loop) thread so the
        publisher can later deliver thread-safely via call_soon_threadsafe.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._clients_lock:
            if len(self._connected_queues) >= self.max_connections:
                return None
            self._connected_queues.add(q)
            self._queue_loops[q] = loop
        return q

    register = register_client

    def connect(self) -> Optional[asyncio.Queue]:
        """Alias for register_client."""
        return self.register_client()

    def disconnect(self, q: asyncio.Queue) -> None:
        """Alias for unregister_client."""
        self.unregister_client(q)

    def unregister_client(self, q: asyncio.Queue) -> None:
        """Disconnect a web client."""
        with self._clients_lock:
            self._connected_queues.discard(q)
            self._queue_loops.pop(q, None)

    def handle_operator_steer(self, guidance: str) -> None:
        """Inject steering prompt from web UI operator into the event bus."""
        self.bus.publish(
            AgentEvent(
                event_type=EventType.OPERATOR_STEER,
                payload={"guidance": guidance},
            )
        )

    def get_status_summary(self) -> Dict[str, Any]:
        """Return snapshot summary of the active session."""
        history = self.bus.get_history()
        with self._clients_lock:
            connected_clients = len(self._connected_queues)
        return {
            "total_events": self.bus.get_total_event_count(),
            "connected_clients": connected_clients,
            "last_event": history[-1].event_type.value if history else None,
        }


def create_fastapi_app(
    bridge: WebEventBridge,
    engine: Optional[Any] = None,
    ws_idle_timeout: float = WS_IDLE_TIMEOUT_SECONDS,
) -> Any:
    """Optionally instantiate a FastAPI application if FastAPI is installed."""
    try:
        from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, Field

        auth_token = _load_or_create_auth_token()
        app = FastAPI(title="Pair Programming Agent API", version="0.1.0")
        coordinator = SprintRunCoordinator(bridge, engine)
        app.state.coordinator = coordinator
        app.state.session_status = "ready"
        app.state.sprint_task = None
        app.state.current_run_id = None
        app.state.ws_idle_timeout = ws_idle_timeout
        rate_buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        rate_lock = threading.Lock()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=sorted(ALLOWED_ORIGINS),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

        @app.middleware("http")
        async def protect_api(request, call_next):
            """Authenticate and rate-limit all REST API requests with separated auth/unauth quotas."""
            path = request.url.path
            if path != "/api" and not path.startswith("/api/"):
                return await call_next(request)

            origin = request.headers.get("origin")
            is_preflight = (
                request.method == "OPTIONS"
                and request.headers.get("access-control-request-method") is not None
            )
            remote = request.client.host if request.client is not None else "unknown"
            status_code, headers, detail = evaluate_rest_access(
                method=request.method,
                origin=origin,
                authorization=request.headers.get("authorization", ""),
                remote=remote,
                auth_token=auth_token,
                rate_buckets=rate_buckets,
                rate_lock=rate_lock,
                is_preflight=is_preflight,
            )
            if status_code != 200:
                return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)
            return await call_next(request)

        class SteerRequest(BaseModel):
            guidance: str = Field(min_length=1, max_length=4000)

        class StoryInput(BaseModel):
            title: str = Field(min_length=1, max_length=200)
            human_interaction_contract: str = Field(min_length=1, max_length=4000)
            acceptance_criteria: List[str] = Field(default_factory=list, max_length=20)

        class SprintStartRequest(BaseModel):
            epic: str = Field(min_length=1, max_length=500)
            stories: List[StoryInput] = Field(min_length=1, max_length=30)

        @app.get("/api/status")
        async def get_status():
            summary = bridge.get_status_summary()
            summary["session_status"] = coordinator.session_status
            summary["run_id"] = coordinator.current_run_id
            app.state.session_status = coordinator.session_status
            app.state.current_run_id = coordinator.current_run_id
            if engine is not None:
                sprint = engine.backlog_mgr.sprint
                summary.update(
                    {
                        "epic": sprint.epic_goal,
                        "current_index": sprint.current_index,
                        "is_complete": sprint.is_complete,
                        "models": {
                            "a": engine.config.model_a,
                            "b": engine.config.model_b,
                        },
                        "stories": [
                            {
                                "id": story.id,
                                "title": story.title,
                                "human_interaction_contract": story.human_interaction_contract,
                                "acceptance_criteria": story.acceptance_criteria,
                                "status": story.status.value,
                                "dod": story.dod.model_dump(),
                            }
                            for story in sprint.stories
                        ],
                    }
                )
            return summary

        @app.get("/api/events")
        async def get_events(limit: int = Query(default=200, ge=1, le=300)):
            return {"events": bridge.get_recent_events(limit)}

        @app.post("/api/sprint/start", status_code=202)
        async def start_sprint(req: SprintStartRequest):
            if engine is None:
                raise HTTPException(status_code=503, detail="Sprint controls are unavailable.")

            if not req.epic.strip() or any(
                not story.title.strip() or not story.human_interaction_contract.strip()
                for story in req.stories
            ):
                raise HTTPException(status_code=422, detail="Add a sprint goal, story title, and interaction contract.")

            stories = [
                UserStory(
                    title=story.title.strip(),
                    human_interaction_contract=story.human_interaction_contract.strip(),
                    acceptance_criteria=[criterion.strip() for criterion in story.acceptance_criteria if criterion.strip()],
                )
                for story in req.stories
            ]
            try:
                result = await coordinator.start(req.epic.strip(), stories)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            app.state.session_status = coordinator.session_status
            app.state.sprint_task = coordinator.sprint_task
            app.state.current_run_id = coordinator.current_run_id
            return result

        @app.post("/api/sprint/pause")
        async def pause_sprint():
            if engine is None or coordinator.session_status not in {"running", "starting"}:
                raise HTTPException(status_code=409, detail="No active sprint can be paused.")
            engine.pause()
            coordinator.session_status = "paused"
            app.state.session_status = "paused"
            return {"status": "paused"}

        @app.post("/api/sprint/resume")
        async def resume_sprint():
            if engine is None or coordinator.session_status != "paused":
                raise HTTPException(status_code=409, detail="The sprint is not paused.")
            engine.resume()
            coordinator.session_status = "running"
            app.state.session_status = "running"
            return {"status": "running"}

        @app.post("/api/sprint/stop")
        async def stop_sprint():
            if engine is None or coordinator.session_status not in {"running", "starting", "paused"}:
                raise HTTPException(status_code=409, detail="No active sprint can be stopped.")
            engine.request_stop()
            coordinator.session_status = "stopped"
            app.state.session_status = "stopped"
            return {"status": "stopped"}

        @app.post("/api/operator/steer")
        async def steer_operator(req: SteerRequest):
            bridge.handle_operator_steer(req.guidance)
            return {"status": "ok", "message": "Steering guidance ingested"}

        @app.websocket("/ws/events")
        async def websocket_endpoint(websocket: WebSocket):
            remote = websocket.client.host if websocket.client is not None else "unknown"
            origin = websocket.headers.get("origin")
            allowed, close_code, close_reason, accepted_protocol = evaluate_ws_handshake(
                scope=websocket.scope,
                origin=origin,
                remote=remote,
                auth_token=auth_token,
                rate_buckets=rate_buckets,
                rate_lock=rate_lock,
            )
            if not allowed:
                await websocket.close(code=close_code, reason=close_reason)
                return

            queue = bridge.register_client()
            if queue is None:
                await websocket.close(code=1013, reason="Connection limit reached.")
                return
            idle_timeout = float(getattr(app.state, "ws_idle_timeout", WS_IDLE_TIMEOUT_SECONDS))
            try:
                await websocket.accept(subprotocol=accepted_protocol)
                await stream_ws_events_with_idle_timeout(websocket, queue, idle_timeout=idle_timeout)
            except WebSocketDisconnect:
                pass
            finally:
                bridge.unregister_client(queue)

        return app

    except ImportError:
        return None
