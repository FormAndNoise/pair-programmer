import os
import re
from server import PromptServer


class SequentialMarkdownPromptLoader:
    # Class-level state dictionary to track progress across queue runs.
    # Keys are "{unique_id}_{file_path}" strings.
    _state = {}

    # File parse cache keyed by file_path → (mtime, prompts).
    # Avoids re-reading and re-parsing the file on every execution.
    _file_cache = {}

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file_path": ("STRING", {
                    "default": "prompts.md",
                    "multiline": False
                }),
                "reset_sequence": ("BOOLEAN", {
                    "default": False,
                    "label_on": "Reset to Start",
                    "label_off": "Continue Sequence"
                }),
                "start_index": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 99999,
                    "step": 1
                }),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            }
        }

    RETURN_TYPES = ("STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("prompt", "current_index", "total_prompts", "filename_prefix")
    FUNCTION = "get_next_prompt"
    CATEGORY = "utils/prompts"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # Returning NaN forces ComfyUI to re-evaluate this node every single run
        # instead of serving a cached result.
        return float("NaN")

    def parse_markdown(self, file_path):
        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"[SequentialPrompt] Markdown file not found at: {file_path}"
            )

        # FIX #6: Cache parse results by mtime — skip re-read if file is unchanged.
        mtime = os.path.getmtime(file_path)
        cached = self.__class__._file_cache.get(file_path)
        if cached and cached[0] == mtime:
            return cached[1]

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # FIX #4: Strip YAML front-matter (e.g., from Obsidian/Hugo/Jekyll files)
        # so the opening "---" block is not treated as a prompt separator.
        content = re.sub(r'^---\n.*?\n---\n', '', content, count=1, flags=re.DOTALL)

        # Split blocks by horizontal rules (---) on their own line.
        raw_blocks = re.split(r'^\s*---+\s*$', content, flags=re.MULTILINE)

        prompts = []
        for block in raw_blocks:
            # Strip out markdown comment/header lines starting with #.
            lines = [
                line.strip() for line in block.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            cleaned_prompt = " ".join(lines).strip()
            if cleaned_prompt:
                prompts.append(cleaned_prompt)

        self.__class__._file_cache[file_path] = (mtime, prompts)
        return prompts

    def get_next_prompt(self, file_path, reset_sequence, start_index, unique_id):
        prompts = self.parse_markdown(file_path)
        total = len(prompts)

        if total == 0:
            raise ValueError(
                f"[SequentialPrompt] No valid prompts found in {file_path}"
            )

        node_key = f"{unique_id}_{file_path}"
        reset_flag_key = f"{node_key}_reset_pending"

        # FIX #3: Clamp start_index to a valid range before using it.
        clamped_start = start_index % total

        # FIX #1: One-shot reset guard.
        # The BOOLEAN widget stays True across runs unless the user resets it,
        # so we track whether this reset has already been consumed to avoid
        # restarting the sequence on every subsequent run.
        if reset_sequence and not self._state.get(reset_flag_key):
            # First run where reset is True — apply the reset once.
            self._state[node_key] = clamped_start
            self._state[reset_flag_key] = True
        elif not reset_sequence:
            # Widget is back to False — clear the consumed flag so a future
            # True edge will trigger a reset again.
            self._state.pop(reset_flag_key, None)
            if node_key not in self._state:
                self._state[node_key] = clamped_start
        # else: reset_sequence is True but already consumed — do nothing.

        current_idx = self._state[node_key]

        # Clamp index defensively (e.g., file shrank since last run).
        if current_idx >= total:
            current_idx = 0

        prompt_text = prompts[current_idx]

        # Advance state for the next run.
        next_idx = current_idx + 1
        has_more = next_idx < total

        if has_more:
            self._state[node_key] = next_idx
        else:
            # Full sequence complete — reset back to the (clamped) start.
            self._state[node_key] = clamped_start

        # Notify the JS frontend whether to queue another run.
        PromptServer.instance.send_sync("sequential_prompt_status", {
            "node_id": unique_id,
            "current_index": current_idx,
            "total": total,
            "has_more": has_more
        })

        prefix = f"prompt_{current_idx + 1:03d}_of_{total:03d}"
        print(
            f"[SequentialPrompt] Running {current_idx + 1}/{total}: "
            f"{prompt_text[:50]}..."
        )

        return (prompt_text, current_idx + 1, total, prefix)
