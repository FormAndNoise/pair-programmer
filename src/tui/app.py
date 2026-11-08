"""Human-Centered Terminal Dashboard for Cross-Model Pair Programming."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from agent_core.events import AgentEvent, EventBus, EventType
from agent_core.sanitize import strip_ansi


class PairProgrammingTUI:
    """Rich terminal user interface visualizing dual-model pair programming and sprint progression."""

    MAX_STORIES = 500
    MAX_DIALOGUE_ENTRIES = 20

    def __init__(self, bus: EventBus, console: Optional[Console] = None) -> None:
        self.bus = bus
        self.console = console or Console()
        self.epic_goal = "Initializing Sprint..."
        self.active_story_title = "None"
        self.active_story_status = "IDLE"
        self.active_driver = "Driver"
        self.active_navigator = "Navigator"
        self.stories: List[Dict[str, Any]] = []
        self.dialogue_history: List[Dict[str, str]] = []
        self.vm_logs: List[str] = []
        self.is_paused: bool = False
        self.steering_buffer: str = ""
        self.dod_state: Dict[str, bool] = {
            "syntax_valid": False,
            "tests_passed": False,
            "cross_review_approved": False,
            "user_experience_verified": False,
        }

        # Wire event listener
        self.bus.subscribe_all(self.on_event)

    def on_event(self, event: AgentEvent) -> None:
        """Handle incoming events from the pair programming engine."""
        payload = event.payload

        if event.event_type == EventType.SPRINT_START:
            self.epic_goal = strip_ansi(payload.get("epic", self.epic_goal))

        elif event.event_type == EventType.BACKLOG_DECOMPOSED:
            self.epic_goal = strip_ansi(payload.get("epic", self.epic_goal))
            existing_ids = {s["id"] for s in self.stories}
            for item in payload.get("stories", []):
                story_id = strip_ansi(item.get("id", ""))
                if story_id and story_id not in existing_ids:
                    self._append_story({
                        "id": story_id,
                        "title": strip_ansi(item.get("title", "Story")),
                        "status": strip_ansi(item.get("status", "TODO")),
                    })
                    existing_ids.add(story_id)
            self._append_vm_log(f"[po] Decomposed epic into {payload.get('story_count', len(self.stories))} stories.")

        elif event.event_type == EventType.STORY_START:
            self.active_story_title = strip_ansi(payload.get("title", "Active Story"))
            self.active_story_status = "RED_TESTS"
            self.active_driver = strip_ansi(payload.get("test_author") or payload.get("driver") or (event.source_model.split('/')[-1] if event.source_model else "claude-3-5"))
            self.active_navigator = strip_ansi(payload.get("implementer") or payload.get("navigator") or "gemini-2.5")
            self.dod_state = {k: False for k in self.dod_state}

            # Update or append story in tracking list
            story_id = strip_ansi(payload.get("story_id", ""))
            found = False
            for s in self.stories:
                if s["id"] == story_id:
                    s["status"] = "IN_PROGRESS"
                    found = True
                    break
            if not found:
                self._append_story({
                    "id": story_id or "US-?",
                    "title": self.active_story_title,
                    "status": "IN_PROGRESS",
                })

        elif event.event_type == EventType.STORY_STATUS_CHANGE:
            new_status = strip_ansi(payload.get("status", self.active_story_status))
            if new_status == "GREEN_IMPL" and self.active_story_status != "GREEN_IMPL":
                # Roles swap in Green Implementation phase
                self.active_driver, self.active_navigator = self.active_navigator, self.active_driver
            self.active_story_status = new_status

        elif event.event_type == EventType.MODEL_MESSAGE:
            model = strip_ansi(event.source_model or "Unknown Model")
            content = strip_ansi(payload.get("content") or "[Tool Call Dispatched]")
            role = strip_ansi(payload.get("role") or "driver")
            self.dialogue_history.append({
                "model": model,
                "role": role,
                "content": content,
                "time": time.strftime("%H:%M:%S", time.localtime(event.timestamp)),
            })
            self._trim_dialogue_history()

        elif event.event_type == EventType.CRITIQUE_RECEIVED:
            model = strip_ansi(event.source_model or "Reviewer")
            self.dialogue_history.append({
                "model": model,
                "role": "reviewer",
                "content": strip_ansi(payload.get("critique", "")),
                "time": time.strftime("%H:%M:%S", time.localtime(event.timestamp)),
            })
            self._trim_dialogue_history()

        elif event.event_type == EventType.TOOL_RESULT:
            output = strip_ansi(payload.get("output", ""))
            tool = strip_ansi(payload.get("tool_name", "tool"))
            summary = output.strip().splitlines()
            first_line = summary[0] if summary else "Completed"
            self._append_vm_log(f"[{tool}] {first_line[:90]}")

        elif event.event_type == EventType.DOD_EVALUATION:
            self.dod_state = {
                "syntax_valid": payload.get("syntax_valid", False),
                "tests_passed": payload.get("tests_passed", False),
                "cross_review_approved": payload.get("cross_review_approved", False),
                "user_experience_verified": payload.get("user_experience_verified", False),
            }

        elif event.event_type in (EventType.GIT_BRANCH, EventType.GIT_COMMIT, EventType.GIT_MERGE):
            if event.event_type == EventType.GIT_COMMIT:
                line = f"[git] {payload.get('sha', '')} {payload.get('message', '')}"
            elif event.event_type == EventType.GIT_MERGE:
                verdict = "merged" if payload.get("merged") else "NOT merged"
                line = f"[git] {payload.get('branch', '')} {verdict}"
            else:
                line = f"[git] {payload.get('action', '')} {payload.get('branch', '')}"
            self._append_vm_log(line)

        elif event.event_type == EventType.REFACTOR_ITERATION:
            attempt = payload.get("attempt", 1)
            max_att = payload.get("max_attempts", 1)
            self._append_vm_log(f"[refactor] Attempt {attempt}/{max_att}: self-healing implementation")

        elif event.event_type == EventType.OPERATOR_PAUSE:
            self.is_paused = True
            self._append_vm_log("[operator] Sprint PAUSED -- type guidance & press Enter, or Space to resume")

        elif event.event_type == EventType.OPERATOR_RESUME:
            self.is_paused = False
            self.steering_buffer = ""
            self._append_vm_log("[operator] Sprint RESUMED")

        elif event.event_type == EventType.OPERATOR_STEER:
            guidance = strip_ansi(payload.get("guidance", ""))
            self._append_vm_log(f"[steer] Guidance queued: {guidance[:70]}")

        elif event.event_type == EventType.OPERATOR_STOP:
            self.is_paused = False
            self._append_vm_log("[operator] Clean stop requested -- exiting sprint")

        elif event.event_type == EventType.STORY_DONE:
            story_id = strip_ansi(payload.get("story_id", ""))
            for s in self.stories:
                if s["id"] == story_id:
                    s["status"] = "DONE"
                    break
            self.active_story_status = "DONE"

    def _append_story(self, story: Dict[str, Any]) -> None:
        """Append one story and keep only the most recent sprint entries."""
        self.stories.append(story)
        if len(self.stories) > self.MAX_STORIES:
            del self.stories[: len(self.stories) - self.MAX_STORIES]

    def _trim_dialogue_history(self) -> None:
        """Keep the rendered dialogue history bounded, including reviewer notes."""
        if len(self.dialogue_history) > self.MAX_DIALOGUE_ENTRIES:
            del self.dialogue_history[: len(self.dialogue_history) - self.MAX_DIALOGUE_ENTRIES]

    def _append_vm_log(self, line: str) -> None:
        """Append a line to the bounded VM log buffer."""
        self.vm_logs.append(strip_ansi(line)[:100])
        if len(self.vm_logs) > 6:
            self.vm_logs.pop(0)

    def _format_display_model(self, model: str, compact: bool = False) -> str:
        """Format model identifier cleanly to avoid mid-word slicing and ambiguity."""
        if not model:
            return "system"
        m = strip_ansi(model).strip()
        if "claude" in m.lower():
            return "claude" if compact else "anthropic/claude"
        if "gemini" in m.lower():
            return "gemini" if compact else "gemini/gemini"
        if compact:
            return m.split('/')[-1][:12]
        return m if len(m) <= 18 else m.split('/')[-1]

    def render_layout(self) -> Layout:
        """Construct the complete multi-pane Rich dashboard."""
        is_compact = self.console.height < 28 or self.console.width < 90
        all_passed = all(self.dod_state.values())

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3),
        )

        layout["body"].split_row(
            Layout(name="sidebar", ratio=1),
            Layout(name="stage", ratio=2),
        )

        feedback_ratio = 1
        stage_ratio = 3 if not is_compact else 2
        layout["body"]["stage"].split_column(
            Layout(name="dialogue", ratio=stage_ratio),
            Layout(name="vm_feedback", ratio=feedback_ratio),
        )

        # 1. Header with clear semantic badges
        if self.active_story_status == "DONE":
            status_style = "bold #000000 on #22C55E"
        elif "REVIEW" in self.active_story_status:
            status_style = "bold #000000 on #FACC15"
        elif "GREEN" in self.active_story_status:
            status_style = "bold #000000 on #22C55E"
        elif "RED" in self.active_story_status:
            status_style = "bold #FFFFFF on #EF4444"
        elif self.active_story_status == "IN_PROGRESS":
            status_style = "bold #000000 on #FACC15"
        else:
            status_style = "bold #FFFFFF on #334155"

        if is_compact:
            header_text = Text.assemble(
                (" STUDIO ", "bold #FFFFFF on #1E293B"),
                (" ", ""),
                ("Epic: ", "bold #94A3B8"),
                (f"{self.epic_goal[:16]}", "bold #38BDF8"),
                (" | ", "dim #64748B"),
                ("Story: ", "bold #94A3B8"),
                (f"{self.active_story_title[:16]} ", "bold #FFFFFF"),
                (f"[{self.active_story_status}]", status_style),
            )
        else:
            header_text = Text.assemble(
                (" AGILE PAIR-PROGRAMMING STUDIO ", "bold #FFFFFF on #1E293B"),
                (" Epic: ", "bold #94A3B8"),
                (f"{self.epic_goal[:35]}", "bold #38BDF8"),
                (" | Story: ", "bold #94A3B8"),
                (f"{self.active_story_title[:24]} ", "bold #FFFFFF"),
                (f"[{self.active_story_status}]", status_style),
            )
        layout["header"].update(Panel(header_text, style="white on #0F172A", border_style="#38BDF8"))

        # 2. Sidebar (Sprint Backlog & DoD)
        backlog_status_width = 6 if is_compact else 10
        backlog_table = Table(
            title="Sprint Backlog",
            expand=True,
            show_header=True,
            header_style="bold #38BDF8",
            border_style="#38BDF8",
            padding=(0, 0 if is_compact else 1),
        )
        backlog_table.add_column("Status", width=backlog_status_width, justify="center")
        backlog_table.add_column("Story Title", ratio=1, style="bold #FFFFFF")

        for s in self.stories:
            if is_compact:
                badge = "[bold #000000 on #22C55E]DONE[/]" if s["status"] == "DONE" else (
                    "[bold #000000 on #FACC15]ACTV[/]" if s["status"] == "IN_PROGRESS" else "[bold #38BDF8]TODO[/]"
                )
            else:
                badge = "[bold #000000 on #22C55E]  DONE  [/]" if s["status"] == "DONE" else (
                    "[bold #000000 on #FACC15] ACTIVE [/]" if s["status"] == "IN_PROGRESS" else "[bold #38BDF8]  TODO  [/]"
                )
            backlog_table.add_row(badge, Text(s["title"], style="bold #FFFFFF"))

        if not self.stories:
            empty_badge = "[bold #38BDF8]TODO[/]" if is_compact else "[bold #38BDF8]  TODO  [/]"
            empty_msg = Text("Backlog ready" if is_compact else "No active stories -- Backlog ready", style="bold #FFFFFF")
            backlog_table.add_row(empty_badge, empty_msg)

        # DoD Table with unambiguous status and icon state
        dod_table = Table(
            title="Definition of Done (DoD)",
            expand=True,
            show_header=False,
            padding=(0, 1) if is_compact else (1, 1),
        )
        dod_table.add_column("Key", ratio=1, style="bold #FFFFFF", no_wrap=True)
        val_col_width = 8 if is_compact else 10
        dod_table.add_column("Val", width=val_col_width, justify="center")

        dod_items = [
            ("Syntax" if is_compact else "Syntax & Linter", self.dod_state["syntax_valid"]),
            ("Sandbox Tests" if is_compact else "Sandbox Tests Pass", self.dod_state["tests_passed"]),
            ("Cross Review" if is_compact else "Cross-Model LGTM", self.dod_state["cross_review_approved"]),
            ("UX Pass" if is_compact else "Outside-In UX Pass", self.dod_state["user_experience_verified"]),
        ]
        for name, val in dod_items:
            if val:
                icon = "[bold #000000 on #22C55E]PASS[/]" if is_compact else "[bold #000000 on #22C55E] PASS [/]"
            else:
                icon = "[bold #94A3B8 on #1E293B]WAIT[/]" if is_compact else "[bold #94A3B8 on #1E293B] WAIT [/]"
            dod_table.add_row(name, icon)

        dod_border = "bold #22C55E" if all_passed else "bold #FACC15"
        dod_title = "[bold #22C55E]Quality Gate (DoD) -- PASSED[/]" if all_passed else "[bold #FACC15]Quality Gate (DoD)[/]"
        dod_subtitle = "[bold #22C55E]All Quality Gates Verified[/]" if all_passed else "[dim #FACC15]Definition of Done[/]"

        from rich.console import Group
        from rich.align import Align

        if all_passed:
            dod_content = Group(
                Align.center(Text("*** STORY COMPLETE -- ALL GATES VERIFIED ***", style="bold #000000 on #22C55E")),
                Text(""),
                dod_table,
            )
        else:
            dod_content = dod_table

        sidebar_layout = Layout()
        sidebar_layout.split_column(
            Layout(Panel(backlog_table, title="[bold #38BDF8]Backlog[/]", border_style="#38BDF8"), ratio=1),
            Layout(Panel(dod_content, title=dod_title, subtitle=dod_subtitle, border_style=dod_border), ratio=1),
        )
        layout["body"]["sidebar"].update(sidebar_layout)

        # 3. Stage (Dual-Model Dialogue) with 4 structured, non-clipping columns
        dialogue_table = Table(expand=True, show_header=False, show_edge=False, box=None, padding=(0, 1))
        dialogue_table.add_column("Time", width=8 if is_compact else 10, style="bold #94A3B8")
        dialogue_table.add_column("Role", width=6 if is_compact else 10, justify="center")
        model_col_width = 8 if is_compact else 18
        dialogue_table.add_column("Model", width=model_col_width, no_wrap=True)
        dialogue_table.add_column("Message", ratio=1, no_wrap=False)

        for d in self.dialogue_history[-8:]:
            is_driver = d["role"] == "driver"
            is_reviewer = d["role"] == "reviewer"
            if is_compact:
                role_badge = "[bold #000000 on #38BDF8]DRV[/]" if is_driver else (
                    "[bold #000000 on #C084FC]REV[/]" if is_reviewer else "[bold #94A3B8 on #1E293B]NAV[/]"
                )
            else:
                role_badge = "[bold #000000 on #38BDF8] DRIVER [/]" if is_driver else (
                    "[bold #000000 on #C084FC] REVIEW [/]" if is_reviewer else "[bold #94A3B8 on #1E293B] NAV [/]"
                )

            model_color = "#38BDF8" if "claude" in d["model"].lower() else "#FBBF24"
            display_model = self._format_display_model(d["model"], compact=is_compact)
            time_badge = Text(f"[{d['time']}]", style="bold #94A3B8")
            role_text = Text.from_markup(role_badge)
            model_text = Text(display_model, style=f"bold {model_color}")

            raw_msg = d["content"]
            # Apply native Syntax highlighting when code blocks or python keywords are detected
            if any(kw in raw_msg for kw in ["def ", "class ", "import ", "return ", "Counter", "unittest"]):
                from rich.syntax import Syntax
                message_body = Syntax(raw_msg.strip(), "python", theme="monokai", word_wrap=True)
            else:
                message_body = Text(raw_msg[:300] + ("..." if len(raw_msg) > 300 else ""), style="bold #FFFFFF")

            dialogue_table.add_row(time_badge, role_text, model_text, message_body)

        if not self.dialogue_history:
            empty_time = Text("[00:00:00]", style="bold #94A3B8")
            empty_role = Text.from_markup("[bold #000000 on #38BDF8]RDY[/]" if is_compact else "[bold #000000 on #38BDF8] READY [/]")
            empty_model = Text("Studio", style="bold #38BDF8")
            empty_body = Text("Sprint ready", style="bold #FFFFFF") if is_compact else Text("Sprint initialized -- Pair ready", style="bold #FFFFFF")
            dialogue_table.add_row(empty_time, empty_role, empty_model, empty_body)

        stage_border = "#38BDF8"
        driver_display = self._format_display_model(self.active_driver, compact=is_compact)
        navigator_display = self._format_display_model(self.active_navigator, compact=is_compact)

        if self.is_paused:
            stage_activity_badge = "[bold #000000 on #FACC15] PAUSED [/]"
        elif self.active_story_status == "DONE":
            stage_activity_badge = "[bold #000000 on #22C55E] DONE [/]" if is_compact else "[bold #000000 on #22C55E] COMPLETE [/]"
        elif "REFACTOR" in self.active_story_status:
            stage_activity_badge = "[bold #000000 on #FACC15] HEAL [/]" if is_compact else "[bold #000000 on #FACC15] REFACTOR [/]  [bold #FACC15]SELF-HEALING[/]"
        elif "GREEN" in self.active_story_status:
            stage_activity_badge = "[bold #000000 on #22C55E] IMPL [/]" if is_compact else "[bold #000000 on #22C55E] ACTIVE [/]  [bold #38BDF8]IMPLEMENTING[/]"
        elif "RED" in self.active_story_status:
            stage_activity_badge = "[bold #FFFFFF on #EF4444] TEST [/]" if is_compact else "[bold #FFFFFF on #EF4444] ACTIVE [/]  [bold #38BDF8]TESTING[/]"
        else:
            stage_activity_badge = "[bold #FFFFFF on #1E293B] IDLE [/]" if is_compact else "[bold #FFFFFF on #1E293B] STANDBY [/]"

        stage_title = Text()
        if is_compact:
            stage_title.append("Stage ", style="bold #38BDF8")
            stage_title.append_text(Text.from_markup(stage_activity_badge))
            stage_title.append(" | DRV:", style="bold #38BDF8")
            stage_title.append(driver_display, style="bold #38BDF8")
            stage_title.append(" NAV:", style="bold #FBBF24")
            stage_title.append(navigator_display, style="bold #FBBF24")
        else:
            stage_title.append("Dual-Model Collaboration Stage  ", style="bold #38BDF8")
            stage_title.append_text(Text.from_markup(stage_activity_badge))
            stage_title.append("  |  DRIVER ", style="bold #000000 on #38BDF8")
            stage_title.append(driver_display, style="bold #38BDF8")
            stage_title.append("  NAVIGATOR ", style="bold #94A3B8 on #1E293B")
            stage_title.append(navigator_display, style="bold #FBBF24")

        layout["body"]["stage"]["dialogue"].update(
            Panel(dialogue_table, title=stage_title, border_style=stage_border)
        )

        # 4. VM Feedback Pane
        if self.vm_logs:
            vm_lines = []
            for log_line in self.vm_logs:
                vm_lines.append(Text(f"> {log_line}", style="bright_white"))
            vm_renderable = Text("\n").join(vm_lines)
        else:
            vm_renderable = Text.assemble(
                ("[SYS]  ", "bold #FBBF24"),
                ("[00:00:00]  ", "bold #94A3B8"),
                ("Sandbox VM ready -- Test runner initialized", "bold #FFFFFF"),
            )

        layout["body"]["stage"]["vm_feedback"].update(
            Panel(vm_renderable, title="[bold magenta]Sandbox VM & Test Runner Log[/]", border_style="magenta")
        )

        # 5. Footer with context-aware controls
        if self.steering_buffer:
            footer_parts = [
                (" STEER: ", "bold #000000 on #FACC15"),
                (" ", ""),
                (f"{self.steering_buffer}_", "bold #FFFFFF"),
                ("    [Enter] Submit    [Esc] Cancel", "#94A3B8"),
            ]
        elif self.is_paused:
            footer_parts = [
                (" PAUSED: ", "bold #000000 on #FACC15"),
                (" ", ""),
                ("Sprint paused. Type to steer, [Space] Resume, [q] Exit", "bold #FACC15"),
            ]
        elif is_compact:
            footer_parts = [
                (" [Space] Steer  ", "#E2E8F0"),
                (" [d] Diff  ", "#E2E8F0"),
                (" [v] Term  ", "#E2E8F0"),
                (" [q] Exit", "bold #EF4444"),
            ]
        else:
            footer_parts = [
                (" CONTROLS: ", "bold #FFFFFF on #1E293B"),
                ("   ", ""),
                ("[Space]", "bold #FACC15"),
                (" Pause/Steer    ", "#E2E8F0"),
                ("[d]", "bold #FACC15"),
                (" Inspect Diff    ", "#E2E8F0"),
                ("[v]", "bold #FACC15"),
                (" Full Terminal    ", "#E2E8F0"),
                ("[q]", "bold #FFFFFF on #EF4444"),
                (" Exit Cleanly", "bold #EF4444"),
            ]

        footer_text = Text.assemble(*footer_parts)
        footer_border = "#22C55E" if all_passed else "#38BDF8"
        layout["footer"].update(Panel(footer_text, style="white on #0F172A", border_style=footer_border))

        return layout

    def display_once(self) -> None:
        """Render a single snapshot of the dashboard to the console."""
        self.console.print(self.render_layout())
