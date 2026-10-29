"""Model Gateway module for cross-vendor LLM coordination."""

from .client import ModelGateway
from .mock import ScriptedMockGateway

__all__ = ["ModelGateway", "ScriptedMockGateway"]
