"""Helpers for removing terminal control sequences from untrusted text."""

from __future__ import annotations

import re


_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\|$)|"
    r"[P^_].*?(?:\x07|\x1b\\|$)|[ -/]*[@-~])"
)
_C1_CSI_SEQUENCE = re.compile(r"\x9b[0-?]*[ -/]*[@-~]")
_C1_STRING_SEQUENCE = re.compile(r"[\x90\x98\x9d-\x9f].*?(?:\x07|\x1b\\|\x9c|$)")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and terminal control characters."""
    clean = _ANSI_SEQUENCE.sub("", str(text))
    clean = _C1_CSI_SEQUENCE.sub("", clean)
    clean = _C1_STRING_SEQUENCE.sub("", clean)
    return _CONTROL_CHARACTERS.sub("", clean)
