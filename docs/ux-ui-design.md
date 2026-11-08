# Pair Programmer Studio — Comprehensive UX/UI Design

**Status:** Design spec. Derived from a full audit of the codebase (CLI, TUI, web studio, engine).
**Note on Figma:** This document is structured Figma-ready (frames, tokens, components). Import the token table into Figma Variables and treat each ASCII wireframe as a frame spec. No live Figma API was used.

---

## 1. Function & Option Inventory (Codebase Audit)

### 1.1 CLI (`main.py`)

| ID | Flag / Argument | Type | Default | Purpose |
|---|---|---|---|---|
| CLI-01 | `--mock` | bool flag | off | Zero-cost scripted demo sprint |
| CLI-02 | `--simulate-ux` | bool flag | off | Run automated UX simulation scorecard |
| CLI-03 | `--docker` | bool flag | off | Docker container sandbox |
| CLI-04 | `--git` | bool flag | off | Story branches, per-model commits, merges |
| CLI-05 | `--epic` | string | "Build Core CLI" | Sprint objective |
| CLI-06 | `--max-stories` | int | 3 | PO decomposition cap |
| CLI-07 | `--no-decompose` | bool flag | off | Single-story mode |
| CLI-08 | `--web` | bool flag | off | Launch web studio API |
| CLI-09 | `--web-host` | string | 127.0.0.1 | API host |
| CLI-10 | `--web-port` | int | 8000 | API port |
| CLI-11 | `--model-a` | string | anthropic/claude-3-5-sonnet | First pair seat |
| CLI-12 | `--model-b` | string | gemini/gemini-2-5-flash | Second pair seat |
| CLI-13 | `--sandbox` | path | ./sandbox_workspace | Isolated workspace |

### 1.2 Environment Variables

| ID | Variable | Default | Purpose |
|---|---|---|---|
| ENV-01..04 | `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` | — | Provider credentials |
| ENV-05/06 | `MODEL_A`, `MODEL_B` | see CLI-11/12 | Model defaults |
| ENV-07 | `SANDBOX_TIMEOUT_SECONDS` | 60 | Watchdog |
| ENV-08 | `SANDBOX_MODE` | local_process | local vs docker |
| ENV-09 | `SANDBOX_IMAGE` | python:3.11-slim | Docker image |
| ENV-10 | `GIT_AUTOMATION` | 0 | Git on/off |
| ENV-11 | `GIT_BASE_BRANCH` | main | Merge base |
| ENV-12 | `OPENAI_BASE_URL` | api.openai.com | Endpoint override |

### 1.3 TUI Keyboard Controls (`src/tui/keyboard.py`)

| ID | Key | Context | Action |
|---|---|---|---|
| KB-01 | `Space` | running | Pause sprint |
| KB-02 | `Space` / `Enter` | paused, empty buffer | Resume |
| KB-03 | printable chars | paused | Type steering guidance |
| KB-04 | `Enter` | paused, text in buffer | Submit steering + resume |
| KB-05 | `Esc` | paused | Discard buffer, resume |
| KB-06 | `Backspace` | paused | Edit buffer |
| KB-07 | `d` | running | Inspect workspace diff |
| KB-08 | `v` | running | Inspect sandbox terminal |
| KB-09 | `q` | running | Request clean stop |
| KB-10 | `c` | DoD passed | Commit & next story (prompted in TUI footer) |

### 1.4 TUI Panels (`src/tui/app.py`)

| ID | Panel | Content |
|---|---|---|
| TUI-01 | Header | Epic, active story, phase badge (RED/GREEN/REVIEW/REFACTOR/DONE/PAUSED) |
| TUI-02 | Sidebar — Sprint Backlog | Story list with TODO/ACTIVE/DONE badges |
| TUI-03 | Sidebar — DoD Quality Gate | 4 gates: Syntax, Sandbox Tests, Cross-Model LGTM, Outside-In UX |
| TUI-04 | Stage — Dialogue | Time, role (DRIVER/NAV/REVIEW), model, message with syntax highlighting |
| TUI-05 | Stage — VM Feedback | Tool results, git lines, operator/refactor log (6-line buffer) |
| TUI-06 | Footer | Key hints, steering buffer echo |
| TUI-07 | Compact mode | Auto below 90x28 terminal; abbreviated badges |

### 1.5 Web API (`src/server/bridge.py`)

| ID | Route | Method | Purpose |
|---|---|---|---|
| API-01 | `/api/status` | GET | Session state, models, stories, DoD |
| API-02 | `/api/events?limit=` | GET | Event history (1–300) |
| API-03 | `/api/sprint/start` | POST | Start sprint {epic, stories[]} |
| API-04 | `/api/sprint/pause` | POST | Pause |
| API-05 | `/api/sprint/resume` | POST | Resume |
| API-06 | `/api/operator/steer` | POST | Inject guidance (1–4000 chars) |
| API-07 | `/ws/events` | WS | Live event stream |

### 1.6 Web Studio Controls (`frontend/src/App.tsx`)

| ID | Control | Type | Bound to |
|---|---|---|---|
| WEB-01 | Epic field | text input | `/api/sprint/start` |
| WEB-02 | Story draft: title / contract / criteria | text inputs + textarea | story list |
| WEB-03 | Start sprint | button | API-03 |
| WEB-04 | Pause / Resume | button pair | API-04/05 |
| WEB-05 | Steering input | text field | API-06 |
| WEB-06 | Event filter | segmented (all/system/model/tool/error) | event feed |
| WEB-07 | Connection badge | status badge | WS state (live/connecting/offline) |
| WEB-08 | Event feed | live list | WS + API-02 |
| WEB-09 | Story status cards | status badges | API-01 |

### 1.7 Agent Tool Surface (exposed to models, not humans)

`read_file`, `write_file`, `patch_file`, `list_dir`, `run_command` — surfaced in UI only as TOOL_START/TOOL_RESULT feed entries (TUI-05, WEB-08).

### 1.8 Event Taxonomy (`src/agent_core/events.py`)

20 event types across 6 groups: sprint lifecycle (6), collaboration (5), tools/sandbox (3), DoD (1), git (3), operator (4), plus `system:error`. All 20 must be visually distinguishable in the event feed via tone mapping.

---

## 2. Design System

### 2.1 Direction

- **Product type:** Developer tool / AI agent observability dashboard
- **Style:** Modern Dark (dark mode primary; no light mode for v1)
- **Dials:** Variance 4 (balanced), Motion 3 (subtle), Density 8 (dense dashboard)
- **Personality:** Precision instrument. The user supervises two AIs; the UI must read as trustworthy telemetry, not chat.

### 2.2 Color Tokens (Figma Variables: `color/*`)

| Token | Hex | Usage |
|---|---|---|
| `color/background` | `#0F172A` | App shell |
| `color/surface` | `#1E293B` | Panels, cards |
| `color/surface-muted` | `#272F42` | Inset areas, table stripes |
| `color/border` | `#475569` | Dividers, panel borders |
| `color/foreground` | `#F8FAFC` | Primary text |
| `color/foreground-muted` | `#94A3B8` | Timestamps, labels |
| `color/accent` | `#22C55E` | Success, GREEN phase, primary CTA |
| `color/info` | `#38BDF8` | Driver seat, links, focus ring |
| `color/warning` | `#FACC15` | REVIEW/REFACTOR, active states |
| `color/destructive` | `#EF4444` | RED phase, errors, stop |
| `color/review` | `#C084FC` | Reviewer role only |
| `color/model-b` | `#FBBF24` | Second model identity |

Contrast: all text-on-background pairs above pass WCAG AA at 4.5:1 except `#94A3B8` on `#0F172A` at small sizes (4.4:1) — reserve muted text for 14px+ or non-critical labels.

**Phase semantics are load-bearing:** RED/GREEN/REVIEW map 1:1 to TDD phases already encoded in the TUI. Never reuse `#EF4444` or `#22C55E` decoratively.

### 2.3 Typography

- **Family:** Inter (300/400/500/600/700) via Google Fonts; code: `JetBrains Mono` or system mono
- **Scale (dense dashboard):** 11 label / 12 caption / 13 body / 15 panel title / 20 header
- **Line-height:** 1.4 UI, 1.55 code
- Model names always rendered in mono to distinguish machine actors from UI chrome.

### 2.4 Spacing & Layout (density 8)

`--space-1:4px  --space-2:8px  --space-3:12px  --space-4:16px  --space-6:24px  --space-8:32px`
Panel padding 12px, gutter 8px, card radius 8px, badge radius 4px.

### 2.5 Motion (subtle)

- State transitions 150–250ms, `power1.out` / `expo.out`
- New event-feed rows: 12px rise + fade, 200ms
- Phase badge change: crossfade only, no scale (badges are read constantly)
- Respect `prefers-reduced-motion`: disable all but opacity
- **Never** animate layout properties; event feed uses transform only

### 2.6 Accessibility (priority 1)

- All sprint controls keyboard reachable; tab order matches visual order
- `aria-live="polite"` on event feed; `aria-live="assertive"` on `system:error` entries
- Visible focus ring: `2px color/info`, never `outline: none`
- Icon-only buttons carry `aria-label` (pause/resume/stop)
- DoD gate state never conveyed by color alone: pair icon + text (PASS/WAIT/REVIEW)

---

## 3. Screen Designs

### 3.1 Web Studio — Sprint Monitor (primary screen)

```
┌────────────────────────────────────────────────────────────────────────┐
│ ● Live  PAIR PROGRAMMER STUDIO   Epic: [Build Core CLI____________]    │
│   conn badge                       [Pause] [Resume] [Stop]  model A·B  │
├──────────────┬───────────────────────────────────────┬─────────────────┤
│ BACKLOG      │ COLLABORATION STAGE                   │ EVENT FEED      │
│ ┌──────────┐ │ ┌─ DRIVER: claude ──────────────────┐ │ [filter pills]  │
│ │DONE story│ │ │ 10:22 model wrote test_word.py   │ │ ● sprint:start  │
│ │ACTV story│ │ └────────────────────────────────────┘ │ ● tool:result   │
│ │TODO story│ │ ┌─ NAVIGATOR: gemini ────────────────┐ │ ● model:message │
│ └──────────┘ │ │ 10:23 implementing count_words…  │ │ ● dod:eval      │
│              │ └────────────────────────────────────┘ │                 │
│ DoD GATE     │ PHASE: [GREEN]  attempt 1/3            │                 │
│ ✓ Syntax     │ ┌─ SANDBOX / TOOL LOG ───────────────┐ │                 │
│ ✓ Tests      │ │ [write_file] test_word.py          │ │                 │
│ ○ LGTM       │ │ [git] a1b2c3 feat: word counter    │ │                 │
│ ○ UX audit   │ └────────────────────────────────────┘ │                 │
├──────────────┴───────────────────────────────────────┴─────────────────┤
│ Steer: [guidance…                                    ] [Send]  ⌨ hints │
└────────────────────────────────────────────────────────────────────────┘
```

- **3-column grid:** 260px backlog/DoD · fluid stage · 320px event feed. Collapses to stacked tabs <1024px.
- Stage maps TUI-04/05 directly; each model seat is a persistent card whose border takes that model's identity color.
- DoD panel replicates TUI-03 gate logic including the "all passed" celebratory state + Commit CTA.

### 3.2 Web Studio — Sprint Setup (pre-start)

Single-column form (max 640px, centered): Epic field (WEB-01), story repeater (WEB-02, add/remove up to 30 per API-03 limit), model pair readout (CLI-11/12 as read-only badges — changing models is a launch-time decision, keep it out of the runtime UI), Start button (WEB-03). Validation errors inline per field, matching the 422 rules in bridge.py (empty title/contract).

### 3.3 TUI Redesign Notes

The TUI (TUI-01..07) is the reference implementation; align it to the same tokens (it already uses the same hex values). Remaining gaps:
- Footer key hints must include `d` (diff), `v` (terminal), `c` (commit) — currently only pause/stop/steer are discoverable.
- Add a dedicated PAUSED banner in the stage panel so state survives glance-away (currently only a small badge).

### 3.4 Component Specs

| Component | Spec |
|---|---|
| `PhaseBadge` | Pill, 11px bold uppercase, bg = phase token, black text; states: RED_TESTS/GREEN_IMPL/REVIEW/REFACTOR/DONE/PAUSED/IDLE |
| `ModelSeatCard` | Surface card, 2px left border in model color, mono model name, latest message, tool-call chip when active |
| `DodGateRow` | Icon (✓/○/◐) + label + status text; never color-only |
| `EventRow` | Tone dot (5 tones: danger/success/tool/model/system) + timestamp mono + title + expandable detail |
| `SteerBar` | Sticky footer input, 4000-char counter (API limit), Enter to send |
| `ConnectionBadge` | Dot + label; offline = destructive, pulses while reconnecting |

---

## 4. Interaction Flows

1. **Start:** Setup → POST /api/sprint/start → status `starting` → WS `sprint:start` → monitor columns populate. Button disabled with spinner while 202 pending (409 = already running, toast).
2. **Steer mid-flight:** Pause (optional) → type guidance → Send → `operator:steer` event echoes in feed as confirmation. TUI parity: pause auto-opens typing mode.
3. **DoD failure:** `dod:evaluation` with failing gates → gate rows flip to destructive, refactor badge shows attempt n/3, feed pins the failure until next evaluation.
4. **Offline:** WS close → badge to offline → exponential reconnect (current 2.5s fixed; recommend 1s→8s backoff) → on reconnect, backfill via `/api/events` (already implemented — keep).
5. **Completion:** All gates pass → banner + Commit & Next CTA → `story:done`; at `sprint:complete`, feed freezes and summary panel (stories, commits, duration) replaces the stage.

---

## 6. Easy Mode (Cognitive Accessibility Track)

**Design persona:** a 13-year-old with Down syndrome on an Amish farm. Every decision is tested against one question: could they use this alone, calmly, the first time?
**Mockup:** `docs/mockups/easy-mode.html`

### 6.1 Principles

1. **One thing per screen.** No dashboards, no sidebars, no feeds. The eye lands on exactly one idea at a time.
2. **One or two actions, ever.** Pause and Stop. Setup has one Go button. Nothing else is pressable.
3. **Machines are characters, not systems.** The two models become named helpers ("Blue" and "Violet") with faces, colors, and turns. People understand taking turns; they do not need "role rotation."
4. **Plain words, spoken aloud in your head.** Grade-3 reading level, Andika font (designed for beginning readers), 20px+ body, 26px+ headings.
5. **Progress is a path, not a pipeline.** Jobs are circles on a path: done (checkmark), now (big, glowing), later (dim). No percentages, no burndowns.
6. **Never alarming.** Errors are impossible to phrase as blame or danger. Worst case message: "The helpers got stuck. A grown-up can look at the details." Soft barn red, never alarm red.
7. **Reassurance is a feature.** A persistent honest footer: "Everything is safe. Nothing is broken." Calm is the product.
8. **48px+ targets, 24px gaps, rounded everything.** Warm paper background, sky/grass/sun/barn palette from a farm world, not a terminal.

### 6.2 Jargon Translation Map

| System term | Easy Mode says |
|---|---|
| Epic / sprint | The big job |
| User story | A job |
| RED / GREEN phase | Writing a check / building it |
| DoD gate: syntax | The check spelled right |
| DoD gate: tests | It really works |
| DoD gate: LGTM | The other helper agrees |
| DoD gate: UX audit | It feels good to use |
| Refactor loop | Trying again (it's okay!) |
| Steering | Tell the helpers something |
| Sandbox | Their worktable |
| `system:error` | The helpers got stuck |
| Operator pause | Pause |

### 6.3 Screens

1. **Hello:** "What should we build?" one big text box + one big green Go button. Optional: three picture-chips of example jobs.
2. **Working (the mockup):** job card, two helper cards, one story sentence, job path, Pause/Stop.
3. **Done:** big celebration ("All 3 jobs are done!"), one button: "See what they made." Details (files, commits) live one tap deeper for grown-ups, never on the main path.
4. **Stuck:** soft message, no stack trace, one button "Try again", one link "Show a grown-up" (expands the technical log).

### 6.4 Relationship to Pro Mode

The dense Sprint Monitor (Section 3) becomes **Pro Mode**, hidden behind a deliberate, non-discoverable-by-accident toggle. Same engine, same events — only the vocabulary and layout change. All Easy Mode copy is a pure function of the existing event stream; no backend changes required.

---

## 7. Standard Mode (Shipping Design — the Middle of the Spectrum)

**Decision:** this is the mode we ship as the default. It sits between Pro Mode (§3, dense telemetry) and Easy Mode (§6, full cognitive scaffold). Mockup: `docs/mockups/standard-mode.html`.

### 7.1 Positioning

| Axis | Pro Mode | **Standard Mode** | Easy Mode |
|---|---|---|---|
| Audience | engineers | general users, students, hobbyists | high-support users |
| Reading level | jargon | plain sentences, technical terms one tap away | grade-3 |
| Layout | 3-column dense grid | 2-column calm grid | single column |
| Actions visible | 9+ | 4 (Tell, Pause, Stop, Details) | 2 |
| Models shown as | provider/model strings | named helpers with faces | named helpers with faces |
| Event stream | full feed, 20 types | narration + one `<details>` drawer | one sentence |

### 7.2 What Standard Mode Keeps from Each Extreme

**From Easy Mode (kept verbatim):**
- Warm paper palette (`#FAF7F0`), farm-world accent colors (sky/grass/sun/barn)
- Named helper characters ("Blue"/"Violet") with colored faces and turns
- Plain-language translation map (§6.2) as the primary copy layer
- Job path circles (done ✓ / now / later)
- DoD gates rendered as friendly check sentences ("It really works")
- Reassurance footer, soft stop-red, 46px+ pill buttons, no alarm states

**From Pro Mode (kept, tamed):**
- Two-column information structure: jobs+checks left, live stage right
- Sequential turn cards with timestamps and file chips (no mono model strings in chrome)
- Full technical truth preserved — but collapsed inside a single `<details>` drawer labeled "Technical details, for grown-ups"
- Steering input retained, relabeled "Tell the helpers something"
- Connection status reduced to one dot + one word ("Working")

**Deliberately dropped from Pro Mode:** event-type filter pills, tone-coded feed, keyboard-hint bar, model pair strings, phase acronyms (RED/GREEN/DoD) in user-facing copy.

### 7.3 Screens

1. **Setup:** "What should we build?" + optional job list (one line per job) + one green Go button. Model selection hidden behind "Advanced."
2. **Working (the mockup):** header status, jobs list, checks card, now-strip, turn cards, details drawer, footer controls.
3. **Done:** same layout with all checks green, celebration line, "See what they made" button revealing sandbox files.
4. **Stuck:** same layout, barn-soft notice card, "Try again" primary action, technical log inside the drawer.

### 7.4 Tokens

Standard Mode owns its own token layer (`--paper`, `--ink`, `--sky`, `--grass`, `--sun`, `--barn`, `--violet` — see mockup `:root`). Type: Inter 16px body / JetBrains Mono only inside the details drawer. Radius 14px, 1px borders, no shadows except button focus rings.

### 7.5 Mode Switching

Three modes, one engine. Mode is a user preference persisted client-side; the same event stream renders through three copy/layout layers. Default = Standard. Easy and Pro are opt-in from a single settings row; no mode ever hides the Stop button.

---

## 8. Pre-Delivery Checklist

- [ ] Lucide/Heroicons SVGs only, no emoji glyphs in chrome (✓/○ inside DoD rows are text semantics, acceptable; keep out of buttons)
- [ ] All controls ≥ 32px hit area, 8px separation
- [ ] Loading feedback on every POST (spinner + disabled)
- [ ] Focus rings visible; tab order = visual order
- [ ] `prefers-reduced-motion` honored
- [ ] Contrast audit on warning/destructive badges (black text on `#FACC15`, white on `#EF4444`)
- [ ] Responsive passes at 375 / 768 / 1024 / 1440
- [ ] Event feed virtualized at 300-row cap (matches existing slice)
