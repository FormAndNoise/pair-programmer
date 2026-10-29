"""Bridge connecting EventBus to Web UI clients over WebSockets and REST."""

import asyncio
import json
from typing import Any, Dict, List, Optional, Set
from agent_core.events import AgentEvent, EventBus, EventType


class WebEventBridge:
    """Dispatches engine events to web client connections and accepts operator steering."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._connected_queues: Set[asyncio.Queue] = set()
        self._queue_loops: Dict[asyncio.Queue, Optional[asyncio.AbstractEventLoop]] = {}

        # Wire event listener
        self.bus.subscribe_all(self._on_agent_event)

    def _on_agent_event(self, event: AgentEvent) -> None:
        """Forward an event to all connected web client queues."""
        data = self._serialize_event(event)
        for q in list(self._connected_queues):
            loop = self._queue_loops.get(q)
            if loop and loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._enqueue_event, q, data)
                except RuntimeError:
                    self.unregister_client(q)
            else:
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

    def register_client(self) -> asyncio.Queue:
        """Register a new web client (e.g. WebSocket connection)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._connected_queues.add(q)
        try:
            self._queue_loops[q] = asyncio.get_running_loop()
        except RuntimeError:
            self._queue_loops[q] = None
        return q

    def unregister_client(self, q: asyncio.Queue) -> None:
        """Disconnect a web client."""
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
        return {
            "total_events": len(history),
            "connected_clients": len(self._connected_queues),
            "last_event": history[-1].event_type.value if history else None,
        }


def create_fastapi_app(bridge: WebEventBridge, engine: Optional[Any] = None) -> Any:
    """Optionally instantiate a FastAPI application if FastAPI is installed."""
    try:
        from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from pydantic import BaseModel, Field
        from agent_core.types import SprintBacklog, UserStory

        app = FastAPI(title="Pair Programming Agent API", version="0.1.0")
        app.state.session_status = "ready"
        app.state.sprint_task = None
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
            try:
                completed = await asyncio.to_thread(engine.run_sprint)
                app.state.session_status = "complete" if completed else "failed"
            except Exception as exc:
                app.state.session_status = "failed"
                bridge.bus.publish(
                    AgentEvent(
                        event_type=EventType.ERROR,
                        payload={"error": str(exc), "stage": "sprint"},
                    )
                )

        @app.post("/api/sprint/start", status_code=202)
        async def start_sprint(req: SprintStartRequest):
            if engine is None:
                raise HTTPException(status_code=503, detail="Sprint controls are unavailable.")

            task = app.state.sprint_task
            if task is not None and not task.done():
                raise HTTPException(status_code=409, detail="A sprint is already running.")
            if app.state.session_status == "complete":
                raise HTTPException(status_code=409, detail="This sprint is already complete.")
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
            engine.backlog_mgr.sprint = SprintBacklog(epic_goal=req.epic.strip(), stories=stories)
            app.state.session_status = "starting"
            app.state.sprint_task = asyncio.create_task(run_sprint())
            return {"status": "starting", "story_count": len(stories)}

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

        @app.post("/api/operator/steer")
        async def steer_operator(req: SteerRequest):
            bridge.handle_operator_steer(req.guidance)
            return {"status": "ok", "message": "Steering guidance ingested"}

        @app.websocket("/ws/events")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            queue = bridge.register_client()
            try:
                while True:
                    data = await queue.get()
                    await websocket.send_text(json.dumps(data))
            except WebSocketDisconnect:
                pass
            finally:
                bridge.unregister_client(queue)

        return app

    except ImportError:
        return None
