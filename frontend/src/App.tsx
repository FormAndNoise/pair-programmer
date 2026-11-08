import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type Story = {
  id: string;
  title: string;
  human_interaction_contract: string;
  acceptance_criteria: string[];
  status: string;
  dod: Record<string, boolean>;
};

type Session = {
  total_events: number;
  connected_clients: number;
  last_event: string | null;
  session_status: "ready" | "starting" | "running" | "paused" | "complete" | "failed";
  epic?: string;
  current_index?: number;
  is_complete?: boolean;
  models?: { a: string; b: string };
  stories?: Story[];
};

type AgentEvent = {
  id: string;
  type: string;
  timestamp: number;
  source_model?: string | null;
  payload: Record<string, unknown>;
};

type StoryDraft = {
  title: string;
  contract: string;
  criteria: string;
};

const initialDraft: StoryDraft = {
  title: "First feature",
  contract: "Describe what a person should be able to do and what result they should see.",
  criteria: "",
};

const statusLabels: Record<string, string> = {
  ready: "Ready",
  starting: "Starting",
  running: "In progress",
  paused: "Paused",
  complete: "Complete",
  failed: "Needs attention",
  todo: "To do",
  in_progress: "In progress",
  red_tests: "Writing tests",
  green_impl: "Implementation",
  review: "Review",
  refactor: "Refactoring",
  stopped: "Stopped",
  done: "Done",
  blocked: "Blocked",
};

const tokenStorageKey = "pairProgrammerToken";

function getSessionToken() {
  const existing = window.sessionStorage.getItem(tokenStorageKey);
  if (existing !== null) return existing;
  const entered = window.prompt("Enter the Pair Programmer access token:");
  const token = entered?.trim() ?? "";
  window.sessionStorage.setItem(tokenStorageKey, token);
  return token;
}

function prettyModel(model?: string | null) {
  if (!model) return "System";
  const [provider, name] = model.split("/", 2);
  return name ? `${provider} · ${name}` : model;
}

function eventTitle(event: AgentEvent) {
  const payload = event.payload;
  switch (event.type) {
    case "sprint:start": return "Sprint started";
    case "sprint:complete": return "Sprint completed";
    case "story:start": return String(payload.title ?? "Story started");
    case "story:status_change": return `Stage: ${statusLabels[String(payload.status ?? "").toLowerCase()] ?? payload.status}`;
    case "story:done": return `Completed: ${String(payload.title ?? "story")}`;
    case "model:message": return "Pair update";
    case "tool:start": return `Running ${String(payload.tool_name ?? "tool")}`;
    case "tool:result": return `${String(payload.tool_name ?? "Tool")} finished`;
    case "pair:critique_received": return "Review received";
    case "dod:evaluation": return payload.is_complete ? "Quality checks passed" : "Quality checks need attention";
    case "operator:steer": return "Guidance sent";
    case "operator:pause": return "Sprint paused";
    case "operator:resume": return "Sprint resumed";
    case "system:error": return "Something needs attention";
    default: return event.type.replaceAll(":", " · ");
  }
}

function eventDetail(event: AgentEvent) {
  const payload = event.payload;
  if (event.type === "story:status_change") return `The pair moved this story into ${String(payload.status ?? "a new stage").toLowerCase().replaceAll("_", " ")}.`;
  if (event.type === "model:message") return String(payload.content ?? "The model sent an update.");
  if (event.type === "tool:start") return String(payload.tool_name ?? "A tool") + " is working in the isolated workspace.";
  if (event.type === "tool:result") return String(payload.output ?? "Tool completed.");
  if (event.type === "pair:critique_received") return String(payload.critique ?? "Review completed.");
  if (event.type === "dod:evaluation") {
    const checks = ["syntax_valid", "tests_passed", "cross_review_approved", "user_experience_verified"];
    return checks.map((key) => `${payload[key] ? "✓" : "○"} ${key.replaceAll("_", " ")}`).join("  ·  ");
  }
  if (event.type === "system:error") return String(payload.error ?? "The sprint encountered an unexpected error.");
  if (event.type === "story:start") return `Test author ${String(payload.test_author ?? "—")} · Implementer ${String(payload.implementer ?? "—")}`;
  if (event.type === "story:done") return "All definition-of-done checks passed for this story.";
  if (event.type === "operator:steer") return String(payload.guidance ?? "Operator guidance added to the sprint.");
  if (event.type.startsWith("operator:")) return "The operator updated the sprint controls.";
  if (event.type === "sprint:start") return String(payload.epic ?? "The pair is beginning the sprint.");
  if (event.type === "sprint:complete") return String(payload.epic ?? "Every story in the sprint is complete.");
  return Object.entries(payload).map(([key, value]) => `${key}: ${String(value)}`).join(" · ");
}

function eventTone(type: string) {
  if (type.includes("error")) return "danger";
  if (type.includes("done") || type.includes("complete") || type.includes("evaluation")) return "success";
  if (type.includes("tool")) return "tool";
  if (type.includes("model") || type.includes("critique")) return "model";
  return "system";
}

function App() {
  const [authToken, setAuthToken] = useState(getSessionToken);
  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connection, setConnection] = useState<"connecting" | "live" | "offline">("connecting");
  const [epic, setEpic] = useState("Build Core CLI");
  const [draft, setDraft] = useState<StoryDraft>(initialDraft);
  const [steering, setSteering] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(authToken ? "" : "An access token is required. Enter it with the workspace button.");
  const [filter, setFilter] = useState("all");
  const formInitialized = useRef(false);

  const loadStatus = useCallback(async () => {
    try {
      const response = await fetch("/api/status", {
        headers: { Authorization: `Bearer ${authToken}` },
      });
      if (response.status === 401) {
        setNotice("The access token was rejected. Use the workspace button to update it.");
        setConnection("offline");
        return;
      }
      if (!response.ok) throw new Error("The studio API is unavailable.");
      const data = (await response.json()) as Session;
      setSession(data);
      if (!formInitialized.current && (data.session_status === "ready" || data.session_status === "failed")) {
        if (data.epic) setEpic(data.epic);
        const firstStory = data.stories?.[0];
        if (firstStory) {
          setDraft({
            title: firstStory.title,
            contract: firstStory.human_interaction_contract,
            criteria: firstStory.acceptance_criteria.join("\n"),
          });
        }
        formInitialized.current = true;
      }
    } catch {
      setConnection("offline");
    }
  }, [authToken]);

  const loadRecentEvents = useCallback(async () => {
    try {
      const response = await fetch("/api/events?limit=300", {
        headers: { Authorization: `Bearer ${authToken}` },
      });
      if (!response.ok) return;
      const data = (await response.json()) as { events: AgentEvent[] };
      setEvents((current) => {
        const knownIds = new Set(current.map((item) => item.id));
        const recovered = [...data.events].reverse().filter((item) => !knownIds.has(item.id));
        return [...recovered, ...current].slice(0, 300);
      });
    } catch {
      // The WebSocket remains the source for new events when history is unavailable.
    }
  }, [authToken]);

  useEffect(() => {
    void loadStatus();
    const poll = window.setInterval(() => void loadStatus(), 4000);
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    let socket: WebSocket | null = null;
    let retryTimer = 0;
    let closed = false;
    const connect = () => {
      if (closed) return;
      setConnection((current) => current === "live" ? current : "connecting");
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/events`, [
        "pair-programmer",
        `bearer.${authToken}`,
      ]);
      socket.onopen = () => {
        setConnection("live");
        void loadRecentEvents();
      };
      socket.onclose = () => {
        setConnection("offline");
        if (!closed) retryTimer = window.setTimeout(connect, 2500);
      };
      socket.onerror = () => setConnection("offline");
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as AgentEvent;
          setEvents((current) => [event, ...current].slice(0, 300));
          void loadStatus();
        } catch {
          // Ignore malformed frames and keep the live stream available.
        }
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retryTimer);
      window.clearInterval(poll);
      socket?.close();
    };
  }, [authToken, loadRecentEvents, loadStatus]);

  const runAction = async (path: string, body?: unknown) => {
    setBusy(true);
    setNotice("");
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${authToken}`,
          "Content-Type": "application/json",
        },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        throw new Error("The access token was rejected. Use the workspace button to update it.");
      }
      if (!response.ok) throw new Error(String(data.detail ?? "The request could not be completed."));
      await loadStatus();
      return true;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "The request could not be completed.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const updateAuthToken = () => {
    const entered = window.prompt("Enter the Pair Programmer access token:");
    const token = entered?.trim();
    if (!token) return;
    window.sessionStorage.setItem(tokenStorageKey, token);
    setAuthToken(token);
  };

  const startSprint = async (event: FormEvent) => {
    event.preventDefault();
    const ok = await runAction("/api/sprint/start", {
      epic: epic.trim(),
      stories: [{
        title: draft.title.trim(),
        human_interaction_contract: draft.contract.trim(),
        acceptance_criteria: draft.criteria.split("\n").map((line) => line.trim()).filter(Boolean),
      }],
    });
    if (ok) setNotice("Sprint queued. The live event stream will show each stage.");
  };

  const sendSteering = async (event: FormEvent) => {
    event.preventDefault();
    const guidance = steering.trim();
    if (!guidance) return;
    if (await runAction("/api/operator/steer", { guidance })) setSteering("");
  };

  const activeStatus = session?.session_status ?? "ready";
  const canPause = activeStatus === "running" || activeStatus === "starting";
  const isActive = canPause || activeStatus === "paused";
  const stories = session?.stories ?? [];
  const doneCount = stories.filter((story) => story.status === "done").length;
  const visibleEvents = useMemo(() => events.filter((event) => {
    if (filter === "all") return true;
    if (filter === "pair") return event.type.includes("model") || event.type.includes("critique") || event.type.includes("story");
    if (filter === "tools") return event.type.includes("tool") || event.type.includes("sandbox");
    return event.type.includes("error") || event.type.includes("evaluation") || event.type.includes("sprint");
  }), [events, filter]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#home" aria-label="Pair Programmer Studio home">
          <span className="brand-mark"><span /><span /></span>
          <span className="brand-name">pair<span>programmer</span></span>
          <span className="brand-divider" />
          <span className="studio-label">STUDIO</span>
        </a>
        <div className="topbar-right">
          <span className={`connection ${connection}`}><i />{connection === "live" ? "Live connection" : connection === "offline" ? "Reconnecting" : "Connecting"}</span>
          <span className="topbar-separator" />
          <button className="avatar-button" aria-label="Update access token" title="Update access token" onClick={updateAuthToken}>AW</button>
        </div>
      </header>

      <main className="page">
        <section className="page-heading">
          <div>
            <div className="eyebrow"><span className="eyebrow-dot" /> CROSS-MODEL COLLABORATION</div>
            <h1>Build together.</h1>
            <p className="page-subtitle">A focused workspace for your AI pair to plan, code, and review.</p>
          </div>
          <div className="heading-actions">
            {isActive && <>
              <button className="control-button" onClick={() => void runAction(canPause ? "/api/sprint/pause" : "/api/sprint/resume")} disabled={busy}>
                <span>{canPause ? "Ⅱ" : "▶"}</span>{canPause ? "Pause sprint" : "Resume sprint"}
              </button>
              <button className="control-button" onClick={() => void runAction("/api/sprint/stop")} disabled={busy} style={{ borderColor: "#ef4444", color: "#f87171" }}>
                <span>■</span>Stop sprint
              </button>
            </>}
            <div className={`session-pill ${activeStatus}`}><span className="session-pulse" />{statusLabels[activeStatus] ?? activeStatus}</div>
          </div>
        </section>

        {notice && <div className={`notice ${notice.includes("could not") || notice.includes("unavailable") ? "notice-error" : "notice-info"}`} role="status">
          <span>{notice.includes("could not") || notice.includes("unavailable") ? "!" : "✓"}</span>{notice}<button onClick={() => setNotice("")} aria-label="Dismiss">×</button>
        </div>}

        <div className="workspace-grid">
          <aside className="sidebar-card">
            <div className="card-heading">
              <div><span className="section-kicker">SPRINT</span><h2>Your backlog</h2></div>
              <span className="count-badge">{stories.length || "—"}</span>
            </div>
            <div className="epic-block">
              <span className="epic-icon">✳</span>
              <div><span className="micro-label">CURRENT EPIC</span><strong>{session?.epic || epic || "New sprint"}</strong></div>
            </div>
            <div className="story-list">
              {stories.length ? stories.map((story, index) => (
                <div className={`story-card ${story.status === "done" ? "story-done" : index === (session?.current_index ?? 0) && isActive ? "story-current" : ""}`} key={story.id}>
                  <div className="story-card-top"><span className="story-number">{String(index + 1).padStart(2, "0")}</span><span className={`story-status status-${story.status}`}>{statusLabels[story.status] ?? story.status}</span></div>
                  <strong>{story.title}</strong>
                  <p>{story.human_interaction_contract}</p>
                  {story.acceptance_criteria.length > 0 && <span className="criteria-count">{story.acceptance_criteria.length} acceptance checks</span>}
                </div>
              )) : <div className="empty-backlog"><span className="empty-icon">＋</span><strong>No stories yet</strong><p>Define a story below to start your first sprint.</p></div>}
            </div>
            <div className="sidebar-foot"><span className="foot-icon">⌘</span><div><strong>{doneCount} of {stories.length} stories done</strong><span>Ping-pong TDD · roles rotate each story</span></div></div>
          </aside>

          <section className="main-column">
            <div className="pair-card">
              <div className="pair-card-intro">
                <div><span className="section-kicker">YOUR PAIR</span><h2>Two models, one team.</h2></div>
                <span className="pair-symbol">A <span>↔</span> B</span>
              </div>
              <div className="model-row">
                <div className="model-chip"><span className="model-avatar model-a">A</span><div><span className="micro-label">MODEL A</span><strong>{prettyModel(session?.models?.a) || "Anthropic Claude"}</strong></div><span className="role-label">{((session?.current_index ?? 0) % 2 === 1) ? "Implementer" : "Test author"}</span></div>
                <span className="model-connector">↔</span>
                <div className="model-chip"><span className="model-avatar model-b">B</span><div><span className="micro-label">MODEL B</span><strong>{prettyModel(session?.models?.b) || "Google Gemini"}</strong></div><span className="role-label">{((session?.current_index ?? 0) % 2 === 1) ? "Test author" : "Implementer"}</span></div>
              </div>
            </div>

            {!isActive && <form className="start-card" onSubmit={startSprint}>
              <div className="card-heading start-heading"><div><span className="section-kicker">START A SPRINT</span><h2>What are we building?</h2></div><span className="start-spark">✦</span></div>
              <label className="field-label" htmlFor="epic">Sprint goal</label>
              <input id="epic" className="text-input" value={epic} onChange={(e) => setEpic(e.target.value)} maxLength={500} required placeholder="e.g. Build a markdown converter" />
              <div className="field-row">
                <div className="field-group"><label className="field-label" htmlFor="story-title">First story</label><input id="story-title" className="text-input" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} maxLength={200} required /></div>
                <div className="field-group"><label className="field-label" htmlFor="criteria">Acceptance checks <span>one per line</span></label><textarea id="criteria" className="text-input criteria-input" value={draft.criteria} onChange={(e) => setDraft({ ...draft, criteria: e.target.value })} rows={2} placeholder="Handles empty input\nShows a helpful error" /></div>
              </div>
              <label className="field-label" htmlFor="contract">Human interaction contract <span>What should the user see or be able to do?</span></label>
              <textarea id="contract" className="text-input contract-input" value={draft.contract} onChange={(e) => setDraft({ ...draft, contract: e.target.value })} maxLength={4000} rows={3} required placeholder="When I run the command, I see..." />
              <div className="start-actions"><span className="start-note"><span>↗</span> Your pair will write tests first, then implement and review.</span><button className="primary-button" type="submit" disabled={busy}>{busy ? "Starting…" : "Start sprint"}<span>→</span></button></div>
            </form>}

            {activeStatus === "complete" && <div className="completion-banner"><span className="complete-mark">✓</span><div><strong>Sprint complete</strong><p>Your pair finished all stories and passed the quality gate.</p></div></div>}

            <section className="activity-card">
              <div className="activity-heading">
                <div><span className="section-kicker">LIVE WORKSPACE</span><h2>Activity</h2></div>
                <div className="activity-tools"><span className="event-count">{events.length} events</span><button className="icon-button" onClick={() => setEvents([])} title="Clear local activity" aria-label="Clear local activity">⌫</button></div>
              </div>
              <div className="filter-tabs" role="tablist" aria-label="Activity filters">
                {[ ["all", "All activity"], ["pair", "Pair"], ["tools", "Tools"], ["checks", "Checks"] ].map(([key, label]) => <button role="tab" aria-selected={filter === key} className={filter === key ? "filter-tab selected" : "filter-tab"} key={key} onClick={() => setFilter(key)}>{label}</button>)}
              </div>
              <div className="activity-feed" aria-live="polite">
                {visibleEvents.length ? visibleEvents.map((event) => (
                  <article className="event-row" key={event.id}>
                    <div className={`event-marker ${eventTone(event.type)}`}>{eventTone(event.type) === "model" ? "✦" : eventTone(event.type) === "tool" ? "⌘" : eventTone(event.type) === "success" ? "✓" : eventTone(event.type) === "danger" ? "!" : "·"}</div>
                    <div className="event-content"><div className="event-title-row"><strong>{eventTitle(event)}</strong><time>{new Date(event.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div><p>{eventDetail(event)}</p><div className="event-meta"><span className={`event-tag ${eventTone(event.type)}`}>{event.type.split(":")[0]}</span>{event.source_model && <span>{prettyModel(event.source_model)}</span>}</div></div>
                  </article>
                )) : <div className="empty-activity"><div className="activity-empty-icon">↗</div><strong>{events.length ? "No matching activity" : "Your pair's work will appear here"}</strong><p>{events.length ? "Try another filter to see more events." : "Start a sprint to watch tests, implementation, and reviews unfold live."}</p></div>}
              </div>
              <div className="activity-footer"><span className="live-dot" /> Events stream in real time <span className="footer-dot">·</span> Latest first</div>
            </section>

            <form className={`steering-card ${!isActive ? "steering-disabled" : ""}`} onSubmit={sendSteering}>
              <div className="steering-heading"><span className="steering-icon">✎</span><div><strong>Guide your pair</strong><span>Add context or redirect the work at any time.</span></div></div>
              <div className="steering-input-row"><input className="text-input" value={steering} onChange={(e) => setSteering(e.target.value)} maxLength={4000} placeholder={isActive ? "e.g. Keep the output friendly and include a usage example…" : "Start a sprint to send live guidance"} disabled={!isActive || busy} /><button className="send-button" type="submit" disabled={!isActive || busy || !steering.trim()} aria-label="Send guidance">↑</button></div>
            </form>
          </section>
        </div>

        <footer className="page-footer"><span>PAIR PROGRAMMER STUDIO</span><span>Outside-in. Test-first. Human-centered.</span><span>{session?.total_events ?? 0} session events</span></footer>
      </main>
    </div>
  );
}

export default App;
