"""Domain contracts and data types for the dual-model pair-programming system."""

from __future__ import annotations

import json
import time
import uuid
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex}", min_length=1)
    name: str = Field(..., min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Observation returned after executing a tool call."""
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex}", min_length=1)
    name: str = Field(..., min_length=1)
    output: str = Field(default="", max_length=100_000)
    is_error: bool = False
    execution_time_ms: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class Message(BaseModel):
    """Universal message unit across models, memory, and UI streaming."""
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: Optional[str] = Field(default=None, max_length=100_000)
    tool_calls: Optional[List[ToolCall]] = None
    tool_result: Optional[ToolResult] = None
    model_name: Optional[str] = Field(default=None, min_length=1)
    pair_role: Optional[PairRole] = None
    timestamp: float = Field(default_factory=time.time, ge=0, allow_inf_nan=False)

    def to_provider_format(self) -> Dict[str, Any]:
        """Convert to standard provider-compatible message dictionary."""
        if self.role == Role.TOOL:
            if self.tool_result is None:
                raise ValueError("Message with role 'tool' requires tool_result to be set.")
            return {
                "role": "tool",
                "tool_call_id": self.tool_result.call_id,
                "content": self.tool_result.output,
            }
        payload: Dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": (
                            tc.arguments
                            if isinstance(tc.arguments, str)
                            else json.dumps(tc.arguments)
                        ),
                    },
                }
                for tc in self.tool_calls
            ]
        return payload


class ToolDefinition(BaseModel):
    """Specification of an executable tool."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    parameters_schema: Dict[str, Any]


class DefinitionOfDone(BaseModel):
    """Human-centered quality gate required before a story closes."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    syntax_valid: bool = False
    tests_passed: bool = False
    cross_review_approved: bool = False
    user_experience_verified: bool = False
    notes: Optional[str] = Field(default=None, max_length=100_000)

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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"US-{uuid.uuid4().hex.upper()}", min_length=1)
    title: str = Field(..., min_length=1)
    human_interaction_contract: str = Field(
        ...,
        min_length=1,
        description="The clear description of how a human interacts with this feature (CLI, UI, error feedback).",
    )
    acceptance_criteria: List[Annotated[str, Field(max_length=2000)]] = Field(default_factory=list)
    status: StoryStatus = StoryStatus.TODO
    driver_model: Optional[str] = Field(default=None, min_length=1)
    navigator_model: Optional[str] = Field(default=None, min_length=1)
    dod: DefinitionOfDone = Field(default_factory=DefinitionOfDone)
    pinned_test_hashes: Optional[Dict[str, str]] = None


class SprintBacklog(BaseModel):
    """Sprint container managing an ordered sequence of UserStories."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"SPRINT-{uuid.uuid4().hex.upper()}", min_length=1)
    epic_goal: str = Field(..., min_length=1)
    stories: List[UserStory] = Field(default_factory=list)
    current_index: int = Field(default=0, ge=0)

    @property
    def active_story(self) -> Optional[UserStory]:
        if 0 <= self.current_index < len(self.stories):
            return self.stories[self.current_index]
        return None

    @property
    def is_complete(self) -> bool:
        return bool(self.stories) and all(story.dod.is_complete for story in self.stories)

    def advance(self) -> Optional[UserStory]:
        if self.current_index < len(self.stories) - 1:
            self.current_index += 1
            return self.active_story
        return None


class AgentConfig(BaseModel):
    """Runtime configuration for models, sandbox, and safety limits."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model_a: str = Field(default="anthropic/claude-3-5-sonnet-20241022", min_length=1)
    model_b: str = Field(default="gemini/gemini-2.5-flash", min_length=1)
    max_turns_per_story: int = Field(default=20, gt=0, le=100)
    max_refactor_attempts: int = Field(default=3, gt=0, le=10)
    sandbox_dir: str = Field(default="./sandbox_workspace", min_length=1)
    timeout_seconds: int = Field(default=60, gt=0, le=600)
    max_output_chars: int = Field(default=8000, gt=0, le=100_000)
    require_ux_audit: bool = True
