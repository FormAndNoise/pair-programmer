"""Domain contracts and data types for the dual-model pair-programming system."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class PairRole(str, Enum):
    DRIVER = "driver"
    NAVIGATOR = "navigator"
    REVIEWER = "reviewer"


class StoryStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    RED_TESTS = "red_tests"
    GREEN_IMPL = "green_impl"
    REFACTOR = "refactor"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


class ToolCall(BaseModel):
    """Represents a tool invocation requested by a model."""
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Observation returned after executing a tool call."""
    call_id: str
    name: str
    output: str
    is_error: bool = False
    execution_time_ms: float = 0.0


class Message(BaseModel):
    """Universal message unit across models, memory, and UI streaming."""
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_result: Optional[ToolResult] = None
    model_name: Optional[str] = None
    pair_role: Optional[PairRole] = None
    timestamp: float = Field(default_factory=time.time)

    def to_provider_format(self) -> Dict[str, Any]:
        """Convert to standard provider-compatible message dictionary."""
        payload: Dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        return payload


class ToolDefinition(BaseModel):
    """Specification of an executable tool."""
    name: str
    description: str
    parameters_schema: Dict[str, Any]


class DefinitionOfDone(BaseModel):
    """Human-centered quality gate required before a story closes."""
    syntax_valid: bool = False
    tests_passed: bool = False
    cross_review_approved: bool = False
    user_experience_verified: bool = False
    notes: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        return (
            self.syntax_valid
            and self.tests_passed
            and self.cross_review_approved
            and self.user_experience_verified
        )


class UserStory(BaseModel):
    """An atomic Agile unit of work defined with Outside-In UX contracts."""
    id: str = Field(default_factory=lambda: f"US-{uuid.uuid4().hex[:4].upper()}")
    title: str
    human_interaction_contract: str = Field(
        ...,
        description="The clear description of how a human interacts with this feature (CLI, UI, error feedback).",
    )
    acceptance_criteria: List[str] = Field(default_factory=list)
    status: StoryStatus = StoryStatus.TODO
    driver_model: Optional[str] = None
    navigator_model: Optional[str] = None
    dod: DefinitionOfDone = Field(default_factory=DefinitionOfDone)


class SprintBacklog(BaseModel):
    """Sprint container managing an ordered sequence of UserStories."""
    id: str = Field(default_factory=lambda: f"SPRINT-{uuid.uuid4().hex[:4].upper()}")
    epic_goal: str
    stories: List[UserStory] = Field(default_factory=list)
    current_index: int = 0

    @property
    def active_story(self) -> Optional[UserStory]:
        if 0 <= self.current_index < len(self.stories):
            return self.stories[self.current_index]
        return None

    @property
    def is_complete(self) -> bool:
        return all(s.status == StoryStatus.DONE for s in self.stories)

    def advance(self) -> Optional[UserStory]:
        if self.current_index < len(self.stories) - 1:
            self.current_index += 1
            return self.active_story
        return None


class AgentConfig(BaseModel):
    """Runtime configuration for models, sandbox, and safety limits."""
    model_a: str = "anthropic/claude-3-5-sonnet-20241022"
    model_b: str = "gemini/gemini-2.5-flash"
    max_turns_per_story: int = 20
    max_refactor_attempts: int = 3
    sandbox_dir: str = "./sandbox_workspace"
    timeout_seconds: int = 60
    max_output_chars: int = 8000
    require_ux_audit: bool = True
