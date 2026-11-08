"""Bridge connecting EventBus to Web UI clients over WebSockets and REST."""

import asyncio
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from agent_core.events import AgentEvent, EventBus, EventType


ALLOWED_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})
MAX_WS_CONNECTIONS = 10
MAX_RECENT_EVENTS = 300
REST_RATE_LIMIT = 60
REST_RATE_WINDOW_SECONDS = 60.0


def _load_or_create_auth_token() -> str:
    """Load the configured token or create a private token file on first run."""
    configured = os.environ.get("PAIR_PROGRAMMER_TOKEN", "").strip()
    if configured:
        return configured

    token_path = Path.home() / ".pair_programmer" / "token"

    def read_existing() -> str:
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read API token file at {token_path}: {exc}") from exc
        if not token:
            raise RuntimeError(f"API token file at {token_path} is empty.")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            # Windows may not expose POSIX mode bits.
            pass
        return token

    try:
        return read_existing()
    except RuntimeError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for attempt in range(100):
            try:
                return read_existing()
            except RuntimeError as exc:
                if exc.__cause__ is not None and not isinstance(exc.__cause__, FileNotFoundError):
                    raise
                if attempt == 99:
                    raise
                time.sleep(0.01)
    with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
        token_file.write(token + "\n")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        # Windows may not expose POSIX mode bits; the file is still created exclusively.
        pass
    return token


def _constant_time_token_match(candidate: str, expected: str) -> bool:
    """Compare UTF-8 token values without a timing-sensitive string comparison."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _origin_is_allowed(origin: Optional[str]) -> bool:
    """Allow non-browser clients and configured local studio origins."""
    return origin is None or origin in ALLOWED_ORIGINS


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

    register = register_client

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


def create_fastapi_app(bridge: WebEventBridge, engine: Optional[Any] = None) -> Any:
    """Optionally instantiate a FastAPI application if FastAPI is installed."""
    try:
        from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, Field
        from agent_core.types import SprintBacklog, UserStory

        auth_token = _load_or_create_auth_token()
        app = FastAPI(title="Pair Programming Agent API", version="0.1.0")
        app.state.session_status = "ready"
        app.state.sprint_task = None
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
            """Authenticate and rate-limit all REST API requests."""
            path = request.url.path
            if path != "/api" and not path.startswith("/api/"):
                return await call_next(request)

            origin = request.headers.get("origin")
            is_preflight = (
                request.method == "OPTIONS"
                and request.headers.get("access-control-request-method") is not None
            )
            if is_preflight:
                if not _origin_is_allowed(origin):
                    return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
                return await call_next(request)

            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_is_allowed(origin):
                return JSONResponse({"detail": "Origin not allowed."}, status_code=403)

            remote = request.client.host if request.client is not None else "unknown"
            now = time.monotonic()
            with rate_lock:
                tokens, updated_at = rate_buckets.get(remote, (float(REST_RATE_LIMIT), now))
                tokens = min(
                    float(REST_RATE_LIMIT),
                    tokens + (now - updated_at) * REST_RATE_LIMIT / REST_RATE_WINDOW_SECONDS,
                )
                allowed = tokens >= 1.0
                if allowed:
                    tokens -= 1.0
                rate_buckets[remote] = (tokens, now)
                rate_buckets.move_to_end(remote)
                if len(rate_buckets) > 4096:
                    rate_buckets.popitem(last=False)
            if not allowed:
                return JSONResponse(
                    {"detail": "Too many requests."},
                    status_code=429,
                    headers={"Retry-After": "1"},
                )

            authorization = request.headers.get("authorization", "")
            scheme, separator, candidate = authorization.partition(" ")
            if (
                not separator
                or scheme.lower() != "bearer"
                or not _constant_time_token_match(candidate.strip(), auth_token)
            ):
                return JSONResponse(
                    {"detail": "Authentication required."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )

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
            summary["session_status"] = app.state.session_status
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

        async def run_sprint() -> None:
            if app.state.session_status != "paused":
                app.state.session_status = "running"
            run_id = uuid.uuid4().hex
            try:
                completed = await asyncio.to_thread(engine.run_sprint)
                if getattr(engine, "stop_requested", False) or app.state.session_status == "stopped":
                    app.state.session_status = "stopped"
                else:
                    app.state.session_status = "complete" if completed else "failed"
            except Exception as exc:
                import traceback

                traceback.print_exc()
                app.state.session_status = "failed"
                bridge.bus.publish(
                    AgentEvent(
                        event_type=EventType.ERROR,
                        payload={
                            "error": "Sprint failed. See server logs.",
                            "stage": "sprint",
                            "run_id": run_id,
                        },
                    )
                )

        @app.post("/api/sprint/start", status_code=202)
        async def start_sprint(req: SprintStartRequest):
            if engine is None:
                raise HTTPException(status_code=503, detail="Sprint controls are unavailable.")

            task = app.state.sprint_task
            if task is not None and not task.done():
                raise HTTPException(status_code=409, detail="A sprint is already running.")
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
            if hasattr(engine, "reset_stop"):
                engine.reset_stop()
            elif hasattr(engine, "request_stop"):
                pass
            if getattr(engine, "is_paused", False):
                engine.resume()
            engine.backlog_mgr.sprint = SprintBacklog(epic_goal=req.epic.strip(), stories=stories)
            app.state.session_status = "starting"
            app.state.sprint_task = asyncio.create_task(run_sprint())
            run_id = uuid.uuid4().hex
            return {"status": "starting", "story_count": len(stories), "run_id": run_id}

        @app.post("/api/sprint/pause")
        async def pause_sprint():
            if engine is None or app.state.session_status not in {"running", "starting"}:
                raise HTTPException(status_code=409, detail="No active sprint can be paused.")
            engine.pause()
            app.state.session_status = "paused"
            return {"status": "paused"}

        @app.post("/api/sprint/resume")
        async def resume_sprint():
            if engine is None or app.state.session_status != "paused":
                raise HTTPException(status_code=409, detail="The sprint is not paused.")
            engine.resume()
            app.state.session_status = "running"
            return {"status": "running"}

        @app.post("/api/sprint/stop")
        async def stop_sprint():
            if engine is None or app.state.session_status not in {"running", "starting", "paused"}:
                raise HTTPException(status_code=409, detail="No active sprint can be stopped.")
            engine.request_stop()
            app.state.session_status = "stopped"
            return {"status": "stopped"}

        @app.post("/api/operator/steer")
        async def steer_operator(req: SteerRequest):
            bridge.handle_operator_steer(req.guidance)
            return {"status": "ok", "message": "Steering guidance ingested"}

        @app.websocket("/ws/events")
        async def websocket_endpoint(websocket: WebSocket):
            origin = websocket.headers.get("origin")
            if not _origin_is_allowed(origin):
                await websocket.close(code=1008, reason="Origin not allowed.")
                return

            offered_protocols = websocket.scope.get("subprotocols", [])
            candidate = next(
                (protocol[len("bearer.") :] for protocol in offered_protocols if protocol.startswith("bearer.")),
                "",
            )
            if not _constant_time_token_match(candidate, auth_token):
                await websocket.close(code=1008, reason="Authentication required.")
                return

            queue = bridge.register_client()
            if queue is None:
                await websocket.close(code=1013, reason="Connection limit reached.")
                return
            accepted_protocol = "pair-programmer" if "pair-programmer" in offered_protocols else None
            try:
                await websocket.accept(subprotocol=accepted_protocol)
                while True:
                    data = await queue.get()
                    try:
                        await websocket.send_text(json.dumps(data))
                    except (WebSocketDisconnect, RuntimeError):
                        break
            except WebSocketDisconnect:
                pass
            finally:
                bridge.unregister_client(queue)

        return app

    except ImportError:
        return None
