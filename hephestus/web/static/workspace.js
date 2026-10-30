/* Hephestus operator workspace.
   Single controller for the live run, per-session evidence, history sidebar,
   export menu and persistent settings. Stores no run data in the browser. */
(() => {
  "use strict";

  // ---- Auth token (capability URL fragment, then sessionStorage) ----
  const params = new URLSearchParams(location.hash.slice(1));
  let token = params.get("token") || sessionStorage.getItem("hephestusToken") || "";
  if (location.hash.includes("token=")) {
    sessionStorage.setItem("hephestusToken", token);
    history.replaceState(null, "", location.pathname);
  }

  const $ = (id) => document.getElementById(id);
  const activeStates = new Set(["running", "waiting_for_user", "stopping"]);
  const LiveEventCursor = window.HephestusLiveState && window.HephestusLiveState.LiveEventCursor;
  const liveCursor = LiveEventCursor ? new LiveEventCursor() : null;

  const state = {
    status: "idle",
    currentRunId: null,
    selectedSession: null,   // run_id when viewing history, null when live
    taskMode: "goal",
    maxSteps: 0,
    loader: {},
    proposal: null,          // {id, privileged:[]}
    stream: null,            // AbortController
    nextStreamAt: 0,
    lastSessionKey: "",
    gpu: { samples: [], lastT: 0, frame: 0, latest: null },
    draft: false,
    asking: false,
    pendingDeletes: new Set(),
    model: { status: "unloaded" },
    models: [],
    coverUrls: new Map(),
    coverTarget: null,
    prevStatus: null,
    alertedProposal: null,
  };

  // ---------- small helpers ----------
  const nice = (x) => String(x == null ? "" : x).replaceAll("_", " ");
  const put = (node, value, empty = false) => {
    if (!node) return;
    node.textContent = value;
    node.classList.toggle("empty", empty);
  };
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v == null ? fallback : v; } catch (_) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } },
  };
  const formatBytes = (bytes) => {
    const gb = Number(bytes || 0) / 1073741824;
    return gb >= 1 ? `${gb.toFixed(gb >= 10 ? 1 : 2)} GB` : `${(Number(bytes || 0) / 1048576).toFixed(0)} MB`;
  };

  // Operator-language labels for how a run ended (mirrors runner.TERMINATION_REASONS).
  const TERMINATION = {
    operator_completed: "Marked complete by operator",
    stopped: "Stopped by operator (Stop & disconnect)",
    human_abort: "Aborted by operator in the approval dialog",
    quick_task_rejected: "Command rejected by operator in the approval dialog",
    operator_rejected: "Command rejected on the remote PC (N)",
    operator_quit: "Remote-PC operator quit the worker (Q)",
    quick_task_complete: "Quick task succeeded",
    model_completion_claim: "Model reports completion — verify the evidence",
    max_steps: "Iteration limit reached",
    timeout: "Run timed out",
    planner_failure: "Model failed to plan",
    no_progress: "No progress — no usable proposal",
    duplicate_no_progress: "Stuck repeating commands",
    invalid_approval: "Invalid approval refused",
    server_interrupted: "Interrupted by server shutdown",
  };
  const terminationText = (reason) => TERMINATION[reason] || nice(reason || "finished");

  // ---------- copy to clipboard ----------
  const COPY_ICON = '<svg class="cp" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg>' +
    '<svg class="ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="m5 12 4.5 4.5L19 7"/></svg>';

  async function writeClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.cssText = "position:fixed;opacity:0;top:0;left:0";
      document.body.append(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      area.remove();
      return ok;
    }
  }

  function copyButton(getText, extraClass = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `copy-btn ${extraClass}`.trim();
    button.title = "Copy to clipboard";
    button.dataset.tip = "Copy";
    button.setAttribute("aria-label", "Copy command to clipboard");
    button.innerHTML = COPY_ICON;
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const text = typeof getText === "function" ? getText() : String(getText || "");
      if (!text) return;
      const ok = await writeClipboard(text);
      button.classList.toggle("copied", ok);
      button.dataset.tip = ok ? "Copied" : "Copy failed";
      clearTimeout(button._reset);
      button._reset = setTimeout(() => { button.classList.remove("copied"); button.dataset.tip = "Copy"; }, 1500);
    });
    return button;
  }

  // ---------- notification sounds (synthesized; no audio files) ----------
  const Sound = {
    ctx: null,
    enabled: store.get("hephestus.sound", "on") !== "off",
    volume: Math.min(1, Math.max(0, Number(store.get("hephestus.volume", "0.85")) || 0.85)),
    unlock() {
      if (!this.ctx) {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return null;
        this.ctx = new AudioCtx();
        this.master = this.ctx.createGain();
        const comp = this.ctx.createDynamicsCompressor();
        comp.threshold.value = -14; comp.knee.value = 10; comp.ratio.value = 4;
        this.master.connect(comp).connect(this.ctx.destination);
      }
      if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
      return this.ctx;
    },
    tone(freq, start, duration, { type = "sine", gain = 0.5, detune = 0 } = {}) {
      const ctx = this.ctx;
      const t0 = ctx.currentTime + start;
      const osc = ctx.createOscillator();
      const overtone = ctx.createOscillator();
      const env = ctx.createGain();
      const shimmer = ctx.createGain();
      osc.type = type; osc.frequency.value = freq; osc.detune.value = detune;
      overtone.type = "sine"; overtone.frequency.value = freq * 2.01;
      shimmer.gain.value = 0.18;
      env.gain.setValueAtTime(0.0001, t0);
      env.gain.exponentialRampToValueAtTime(gain, t0 + 0.012);
      env.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
      osc.connect(env);
      overtone.connect(shimmer).connect(env);
      env.connect(this.master);
      osc.start(t0); overtone.start(t0);
      osc.stop(t0 + duration + 0.05); overtone.stop(t0 + duration + 0.05);
    },
    play(kind, force = false) {
      if (!this.enabled && !force) return;
      if (!this.unlock()) return;
      this.master.gain.value = 1.6 * this.volume;
      const patterns = {
        // bright ascending major arpeggio with a held top note
        complete: () => { [523.25, 659.25, 783.99].forEach((f, i) => this.tone(f, i * 0.1, 0.55, { gain: 0.42 })); this.tone(1046.5, 0.3, 1.4, { gain: 0.5 }); this.tone(1567.98, 0.32, 1.1, { gain: 0.12 }); },
        // two-tone attention chime, repeated so it is hard to miss
        approval: () => { [0, 0.62].forEach((o) => { this.tone(987.77, o, 0.36, { type: "triangle", gain: 0.55 }); this.tone(1318.51, o + 0.16, 0.6, { type: "triangle", gain: 0.55 }); }); },
        // soft descending minor figure
        error: () => { [659.25, 554.37, 440].forEach((f, i) => this.tone(f, i * 0.17, 0.6, { gain: 0.45 })); },
        // quick sparkle when a model finishes loading
        loaded: () => { [783.99, 1174.66, 1567.98].forEach((f, i) => this.tone(f, i * 0.07, 0.42, { gain: 0.34 })); },
      };
      (patterns[kind] || patterns.complete)();
    },
    setEnabled(on) {
      this.enabled = !!on;
      store.set("hephestus.sound", on ? "on" : "off");
      const toggle = $("soundToggle");
      if (toggle) {
        toggle.setAttribute("aria-pressed", String(this.enabled));
        toggle.title = this.enabled ? "Mute notification sounds" : "Unmute notification sounds";
      }
      const box = $("soundEnabled");
      if (box) box.checked = this.enabled;
    },
  };

  // ---------- cartridge art ----------
  function initials(name) {
    // "qwen3-32b-…" -> "Q3", "llama-…" -> "LL", "phi" -> "PH"
    const lead = String(name || "?").replace(/\.gguf$/i, "").split(/[\s_.-]+/).find(Boolean) || "?";
    const letters = lead.replace(/[^A-Za-z]/g, "");
    const digits = (lead.match(/\d+/) || [""])[0].slice(0, 2);
    return ((letters.slice(0, 1) + (digits || letters.slice(1, 2))) || "?").toUpperCase();
  }

  function cartArt(model, coverUrl) {
    const cart = document.createElement("div");
    cart.className = "cart";
    cart.style.setProperty("--h", String(model ? model.accent || 210 : 220));
    const label = document.createElement("div");
    label.className = "cart-label";
    if (coverUrl) {
      const img = document.createElement("img");
      img.src = coverUrl;
      img.alt = "";
      label.append(img);
    } else {
      const big = document.createElement("span");
      big.className = "cart-initials";
      big.textContent = model ? initials(model.name || model.file) : "—";
      const arch = document.createElement("span");
      arch.className = "cart-arch";
      arch.textContent = model ? (model.size_label || model.arch || "gguf") : "";
      label.append(big, arch);
    }
    const grip = document.createElement("span");
    grip.className = "cart-grip";
    cart.append(label, grip);
    return cart;
  }

  function toast(text, type = "info") {
    const stack = $("toastStack");
    if (!stack) return;
    const item = document.createElement("div");
    item.className = `toast ${type}`;
    item.textContent = text;
    stack.append(item);
    setTimeout(() => item.remove(), 4200);
  }

  async function api(path, body) {
    const response = await fetch("/api/" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-Hephestus-Token": token },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({ error: "Invalid response from service" }));
    if (!response.ok && !data.error) data.error = "Request failed";
    return { ok: response.ok, status: response.status, data };
  }

  // ---------- shared event rendering ----------
  const REMOTE_STATUS = {
    approved: "approved and run on the remote PC",
    rejected: "REJECTED by the remote-PC operator (N) — not executed",
    quit: "remote-PC operator QUIT the worker (Q) — not executed",
    update: "remote-PC operator asked for an edit (U) — editing and re-sending",
    error: "remote worker error — not executed",
  };

  function outputText(data) {
    const lines = [`$ ${data.command || ""}`, `Exit code: ${data.exit_code ?? "unknown"}`];
    if (data.remote_status) lines.push(`Remote status: ${REMOTE_STATUS[data.remote_status] || data.remote_status}`);
    if (data.execution_uncertain) lines.push("Execution outcome: UNKNOWN — inspect the target before retrying.");
    else if (data.executed === false) lines.push("Executed: no");
    if (data.stdout) lines.push("\n" + data.stdout);
    if (data.stderr) lines.push("\n[stderr]\n" + data.stderr);
    return lines.join("\n");
  }

  function eventDetail(entry) {
    const d = entry.data || {};
    const makers = {
      environment_info: () => `Runtime ready · ${d.platform || "unknown OS"} · ${d.model_file || "local model"}`,
      start_run: () => `Task: ${d.goal || ""}\nMode: ${d.mode || "goal"}`,
      config_snapshot: () => "Session configuration captured in memory for this run.",
      planner_request: () => `Iteration ${d.attempt || "?"}: asking the local model for the next action.`,
      planner_output: () => d.text || "Planner returned no visible response.",
      proposed_commands: () => (d.commands || []).map((c) => `$ ${c}`).join("\n") || "No executable command was proposed.",
      policy_decision: () => `${d.allowed ? "Allowed" : "Blocked"}: ${d.command || ""}${d.reason ? "\n" + d.reason : ""}`,
      policy_block: () => (d.reasons || []).join("\n") || "The proposal was blocked by policy.",
      safety_review: () => `${d.allowed ? "Safe & in scope" : "BLOCKED (" + (d.source || "safety") + ")"}: ${d.command || ""}${d.reason ? "\n" + d.reason : ""}`,
      safety_block: () => "Safety/scope review blocked a proposal:\n" + ((d.reasons || []).join("\n") || ""),
      edit_request: () => `Operator asked to edit the command${d.instruction ? ':\n"' + d.instruction + '"' : " (different syntax)."}`,
      edited_command: () => `Edited command${d.instruction ? ' ("' + d.instruction + '")' : ""}:\n$ ${d.variant || ""}`,
      edit_failed: () => `Could not apply the edit${d.instruction ? ' ("' + d.instruction + '")' : ""} to:\n$ ${d.command || ""}`,
      approval_decision: () => d.aborted
        ? "Operator ABORTED the task in the approval dialog."
        : d.approval_source === "domain_pc"
          ? `Forwarded ${(d.approved || []).length} command(s) to the remote PC for Y / N / Q approval.`
          : `Approved: ${(d.approved || []).length} · Rejected: ${(d.rejected || []).length}${d.goal_update_provided ? " · New operator guidance received" : ""}`,
      generation_result: () => d.validation_error
        ? `Attempt ${(d.retry ?? 0) + 1} rejected by the controller: ${d.validation_error}`
        : `Attempt ${(d.retry ?? 0) + 1} accepted${d.finish_reason ? " · finish: " + d.finish_reason : ""}.`,
      generation_error: () => d.message || "The model could not generate a response.",
      command_dispatch: () => `Sent to ${d.destination === "remote_handler" ? "the remote PC for local approval" : "the local executor"}:\n$ ${d.command || ""}`,
      execution_result: () => outputText(d),
      observation: () => JSON.stringify(d, null, 2),
      premature_completion: () => "The model claimed completion without command evidence; the controller will request a concrete next step.",
      no_commands: () => "No executable command was extracted; the controller will repair and retry.",
      duplicate_command: () => `Skipped repeated command: ${d.command || ""}`,
      run_complete: () => `Run complete: ${terminationText(d.reason || d.status)}`,
      run_terminated: () => `Run ended: ${terminationText(d.reason)}${d.error_type ? "\nError type: " + d.error_type : ""}`,
    };
    const maker = makers[entry.event];
    return maker ? maker() : (Object.keys(d).length ? JSON.stringify(d, null, 2) : "");
  }

  // ---------- activity feed (working view) ----------
  function activityRow(entry) {
    const row = document.createElement("div");
    row.className = `activity ${entry.event || "info"}`;
    row.dataset.sequence = String(entry.sequence || "");
    const pin = document.createElement("i");
    pin.className = "pin";
    const content = document.createElement("div");
    const heading = document.createElement("div");
    const name = document.createElement("span");
    name.className = "activity-name";
    name.textContent = nice(entry.event || "activity");
    const time = document.createElement("span");
    time.className = "activity-time";
    time.textContent = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "now";
    const detail = document.createElement("div");
    detail.className = "activity-detail";
    detail.textContent = eventDetail(entry);
    const d = entry.data || {};
    const commandText = entry.event === "proposed_commands" ? (d.commands || []).join("\n")
      : entry.event === "command_dispatch" ? d.command || ""
      : entry.event === "edited_command" ? d.variant || "" : "";
    if (commandText) {
      detail.classList.add("codebox");
      detail.append(copyButton(() => commandText));
    }
    heading.append(name, time);
    content.append(heading, detail);
    row.append(pin, content);
    return row;
  }

  // ---------- evidence console (immutable command-output log) ----------
  function exitBadge(data) {
    const badge = document.createElement("span");
    if (data.remote_status === "rejected") {
      badge.className = "exit-badge rej";
      badge.textContent = "rejected · N";
      badge.title = "The remote-PC operator rejected this command; it was not executed.";
    } else if (data.remote_status === "quit") {
      badge.className = "exit-badge quit";
      badge.textContent = "worker quit · Q";
      badge.title = "The remote-PC operator quit the worker; the command was not executed.";
    } else if (data.execution_uncertain) {
      badge.textContent = "Outcome unknown";
      badge.title = "The command may have run. Inspect the target before retrying.";
    } else if (data.executed === false) {
      badge.className = "exit-badge skip";
      badge.textContent = "not run";
    } else if ((data.exit_code ?? 0) === 0) {
      badge.className = "exit-badge ok";
      badge.textContent = "exit 0";
    } else {
      badge.className = "exit-badge bad";
      badge.textContent = `exit ${data.exit_code ?? "?"}`;
    }
    return badge;
  }

  function evidenceBlock(index, entry) {
    const d = entry.data || {};
    const block = document.createElement("div");
    block.className = "evi";
    const head = document.createElement("div");
    head.className = "evi-head";
    const idx = document.createElement("span");
    idx.className = "evi-idx";
    idx.textContent = `#${index}`;
    const cmd = document.createElement("span");
    cmd.className = "evi-cmd";
    cmd.textContent = `$ ${d.command || ""}`;
    cmd.title = d.command || "";
    const time = document.createElement("span");
    time.className = "evi-time";
    time.textContent = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "";
    head.append(idx, cmd, exitBadge(d), time, copyButton(() => d.command || "", "inline"));

    const body = document.createElement("pre");
    body.className = "evi-body";
    if (d.remote_status) {
      const tag = document.createElement("div");
      tag.textContent = `Remote status: ${REMOTE_STATUS[d.remote_status] || d.remote_status}`;
      body.append(tag);
    }
    const stdout = String(d.stdout || "");
    const stderr = String(d.stderr || "");
    if (stdout) {
      const label = document.createElement("span");
      label.className = "evi-label";
      label.textContent = "stdout";
      body.append(label, document.createTextNode(stdout));
    }
    if (stderr) {
      const label = document.createElement("span");
      label.className = "evi-label";
      label.textContent = "stderr";
      const err = document.createElement("span");
      err.className = "evi-stderr";
      err.textContent = stderr;
      body.append(label, err);
    }
    if (!stdout && !stderr && !d.remote_status) {
      body.append(document.createTextNode("(no output captured)"));
    }
    block.append(head, body);
    return block;
  }

  function evidenceList() {
    return $("evidenceList");
  }

  function clearEvidence(placeholder) {
    const list = evidenceList();
    if (!list) return;
    list.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "evidence-empty";
    empty.textContent = placeholder ||
      "Every executed command and its full output is recorded here, in order, for this session — immutable evidence.";
    list.append(empty);
    $("evidenceCount").textContent = "0";
  }

  function appendEvidence(entry) {
    const list = evidenceList();
    if (!list) return;
    const placeholder = list.querySelector(".evidence-empty");
    if (placeholder) placeholder.remove();
    const count = list.querySelectorAll(".evi").length + 1;
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
    list.append(evidenceBlock(count, entry));
    $("evidenceCount").textContent = String(count);
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }

  // ---------- live entry routing ----------
  function updateEvidenceHeaders(entry) {
    const d = entry.data || {};
    if (entry.event === "start_run") put($("requestEcho"), d.goal || "", false);
    if (entry.event === "planner_request") put($("commandStatus"), "Model is planning the next action…");
    if (entry.event === "proposed_commands") {
      const command = (d.commands || []).join("\n");
      put($("commandValue"), command || "No executable command proposed.", !command);
      put($("commandStatus"), command ? "Proposal received · checking policy" : "No command proposed · repairing");
    }
    if (entry.event === "command_dispatch") {
      put($("commandValue"), d.command || "", false);
      put($("commandStatus"), d.destination === "remote_handler"
        ? "Sent to remote PC · awaiting local approval/result"
        : "Executing approved command");
    }
    if (entry.event === "execution_result") {
      put($("commandStatus"), d.remote_status === "update"
        ? "Operator requested an edit · rewriting the command…"
        : `Result received · exit ${d.exit_code ?? "unknown"}`);
    }
    if (entry.event === "edited_command") {
      put($("commandValue"), entry.data.variant || "", false);
      put($("commandStatus"), "Command edited · re-sending for approval");
    }
    if (["run_complete", "run_terminated"].includes(entry.event)) {
      put($("commandStatus"), `Run ended · ${terminationText(d.reason || d.status)}`);
    }
  }

  function appendLiveEntry(entry) {
    const runId = entry.run_id || state.currentRunId || "current";
    if (liveCursor && !liveCursor.accept(runId, entry)) return;
    if (state.selectedSession || state.draft) return;
    const feed = $("activityFeed");
    if (feed) {
      if (!feed.dataset.live) { feed.replaceChildren(); feed.dataset.live = "1"; }
      const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
      feed.append(activityRow(entry));
      $("activityCount").textContent = String(feed.querySelectorAll(".activity").length);
      if (nearBottom) feed.scrollTop = feed.scrollHeight;
    }
    // An "update" result is the operator asking for an edit, not run output.
    if (entry.event === "execution_result" && (entry.data || {}).remote_status !== "update") appendEvidence(entry);
    updateEvidenceHeaders(entry);
  }

  // ---------- progress / status ----------
  function renderProgress(progress = {}) {
    const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    const ring = $("progressRing");
    if (ring) {
      ring.style.background = `conic-gradient(var(--blue) ${percent * 3.6}deg,#2a3b59 0deg)`;
      ring.classList.toggle("working", percent > 0 && percent < 100);
    }
    put($("completionPercent"), percent + "%");
    put($("progressLabel"), progress.label || "Ready for a task");
    const stage = progress.stage_count ? `Stage ${progress.stage_index || 0}/${progress.stage_count}` : "Stage 0/0";
    const iteration = progress.max_steps ? ` · Iteration ${progress.current_step || 0}/${progress.max_steps}` : "";
    put($("stepCount"), stage + iteration);
    const heartbeat = $("progressHeartbeat");
    if (heartbeat) {
      heartbeat.textContent = progress.last_activity_at
        ? `Last activity ${new Date(progress.last_activity_at).toLocaleTimeString()} · live process state`
        : "Stage estimate — token generation progress is not exposed.";
    }
  }

  function applyState(s) {
    state.currentRunId = s.run_id || null;
    state.status = s.status || "idle";
    state.maxSteps = s.max_steps || (s.progress && s.progress.max_steps) || state.maxSteps;
    if (liveCursor) liveCursor.observeRun(s.run_id);
    const active = activeStates.has(s.status);
    notifyTransition(s);

    $("statusPill").className = `status ${s.status || "idle"}`;
    put($("statusText"), nice(s.status || "idle"));
    if (s.model) renderModel(s.model);
    $("startBtn").disabled = active || state.asking || state.model.status !== "loaded";
    $("startBtn").title = state.model.status === "loaded" ? "" : "Load a model from the Models tab first";
    $("stopBtn").classList.toggle("hidden", !active);
    $("stopBtn").disabled = s.status === "stopping";
    $("stopBtn").textContent = s.status === "stopping" ? "Disconnecting…" : "Stop & disconnect";
    $("newSessionBtn").disabled = active;
    $("goalInput").disabled = active;
    $("contextInput").disabled = active;
    const saveBtn = $("saveSettingsBtn");
    if (saveBtn) saveBtn.disabled = active;
    const resetBtn = $("resetSettingsBtn");
    if (resetBtn) resetBtn.disabled = active;
    document.querySelectorAll("#thinkingLevel button,#taskMode button").forEach((b) => { b.disabled = active; });
    const dial = $("thinkingDial");
    if (dial) { dial.disabled = active; if (active) closeThinkingPop(); }

    syncAskAvailability();
    $("startBtn").classList.toggle("primary", true);

    if (!state.draft && !state.selectedSession) renderProgress(s.progress || {});
    syncApproval(s);

    const key = `${s.run_id || "none"}:${s.status || "idle"}`;
    if (key !== state.lastSessionKey) {
      state.lastSessionKey = key;
      if (!state.selectedSession) refreshSessions();
    }
  }

  // A run finishing or pausing for approval should be heard, not just seen.
  function notifyTransition(s) {
    const previous = state.prevStatus;
    state.prevStatus = s.status;
    if (previous === null) { state.alertedProposal = s.proposal_id || null; return; }
    if (s.status === "waiting_for_user" && s.proposal_id && s.proposal_id !== state.alertedProposal) {
      state.alertedProposal = s.proposal_id;
      Sound.play("approval");
      if (document.hidden) document.title = "⚠ Approval needed · Hephestus";
      return;
    }
    if (previous === s.status || !activeStates.has(previous)) return;
    if (s.status === "completed") Sound.play("complete");
    else if (s.status === "error") Sound.play("error");
  }

  // ---------- approval modal ----------
  function syncApproval(s) {
    const modal = $("approvalDialog");
    if (!modal) return;
    if (s.status !== "waiting_for_user" || !(s.pending_commands || []).length) {
      state.proposal = null;
      modal.classList.remove("open");
      return;
    }
    const signature = JSON.stringify(s.pending_commands || []);
    if (state.proposal && state.proposal.id === s.proposal_id && state.proposal.sig === signature
        && modal.classList.contains("open")) return;
    state.proposal = { id: s.proposal_id, privileged: s.privileged_indices || [], sig: signature };
    const list = $("approvalCommandList");
    list.replaceChildren();
    $("approvalGuidance").value = "";
    (s.pending_commands || []).forEach((command, index) => {
      const label = document.createElement("label");
      label.className = "choice";
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = true;
      check.dataset.index = String(index);
      const code = document.createElement("code");
      code.textContent = command;
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "copy-btn edit-btn";
      edit.title = "Update: describe a change and the AI edits this command";
      edit.dataset.tip = "Update";
      edit.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
      edit.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); editPendingCommand(index); });
      label.append(check, code, copyButton(() => command), edit);
      list.append(label);
    });
    modal.classList.add("open");
    setTimeout(() => list.querySelector("input") && list.querySelector("input").focus(), 0);
  }

  async function editPendingCommand(index) {
    if (!state.proposal || !state.proposal.id) return;
    const instruction = window.prompt(
      "Describe the edit to apply to this command. The AI changes only what you ask and keeps the rest.\n\n" +
      "Examples: \"change the message to hello\"  ·  \"keep it the same but obfuscate the content\"\n\n" +
      "Leave blank to just rewrite it in a different syntax:", "");
    if (instruction === null) return;
    const list = $("approvalCommandList");
    if (list) list.classList.add("editing");
    toast("Editing the command…");
    try {
      const { ok, data } = await api("edit", { proposal_id: state.proposal.id, index, instruction: instruction.trim() });
      if (!ok) return toast(data.error || "Could not edit the command.", "error");
      const latest = await api("state");
      if (latest.ok) applyState(latest.data);
      toast("Command updated. Review the new revision before approval.", "success");
    } catch (_) {
      toast("Could not reach the server to edit the command.", "error");
    } finally {
      if (list) list.classList.remove("editing");
    }
    // The modal repaints from the next state poll with the edited command.
  }

  async function submitApproval(decision) {
    if (!state.proposal || !state.proposal.id) return;
    const privileged = (decision.approved || []).some((i) => state.proposal.privileged.includes(i));
    if (privileged && window.prompt("This command may elevate privileges. Type yes to approve:") !== "yes") return;
    const { ok, data } = await api("approve", { ...decision, proposal_id: state.proposal.id, confirmed_privilege: privileged });
    if (!ok) return toast(data.error || "Approval could not be applied.", "error");
    $("approvalDialog").classList.remove("open");
  }

  // ---------- live stream + polling ----------
  async function startStream() {
    if (state.stream) return;
    const controller = new AbortController();
    state.stream = controller;
    try {
      const since = liveCursor ? liveCursor.lastSequence : 0;
      const response = await fetch(`/api/stream?since=${since}`, {
        headers: { "X-Hephestus-Token": token }, signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error("Live activity stream unavailable");
      state.nextStreamAt = 0;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const line = frame.split("\n").find((x) => x.startsWith("data: "));
          if (!line) continue;
          const event = JSON.parse(line.slice(6));
          if (event.type === "status") applyState(event);
          if (event.type === "log") appendLiveEntry(event.entry);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        state.nextStreamAt = Date.now() + 10000;
        toast(`${error.message}; polling remains active.`, "error");
      }
    } finally {
      if (!controller.signal.aborted && activeStates.has(state.status)) {
        state.nextStreamAt = Math.max(state.nextStreamAt, Date.now() + 5000);
      }
      if (state.stream === controller) state.stream = null;
    }
  }

  async function pollState() {
    try {
      const since = liveCursor ? liveCursor.lastSequence : 0;
      const { ok, data } = await api(`events?since=${since}`);
      if (!ok) return;
      for (const runId of [...state.pendingDeletes]) {
        const removal = await fetch(`/api/sessions/${encodeURIComponent(runId)}`, {
          method: "DELETE", headers: { "X-Hephestus-Token": token },
        });
        if (removal.ok || removal.status === 404) state.pendingDeletes.delete(runId);
      }
      if (data.model) renderModel(data.model);
      applyState(data.state);
      for (const entry of data.logs || []) appendLiveEntry(entry);
      if (activeStates.has(data.state.status) && !state.stream && Date.now() >= state.nextStreamAt) startStream();
    } catch (_) { /* stream remains the primary channel */ }
  }

  // ---------- sessions sidebar ----------
  function sessionTimeLabel(session) {
    if (!session.created_at) return "";
    try { return new Date(session.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
    catch (_) { return ""; }
  }

  function sessionItem(session) {
    const item = document.createElement("div");
    item.className = "session-item";
    item.dataset.status = session.status || "idle";
    item.dataset.runId = session.run_id;
    item.dataset.runId = session.run_id;
    const isSelected = state.selectedSession
      ? state.selectedSession === session.run_id
      : (session.current && !state.selectedSession);
    item.classList.toggle("selected", !!isSelected && (state.selectedSession === session.run_id || (!state.selectedSession && session.current)));

    const dot = document.createElement("span");
    dot.className = "sdot";

    const main = document.createElement("div");
    main.className = "session-main";
    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = session.title || "Untitled session";
    const meta = document.createElement("span");
    meta.className = "session-meta";
    const time = sessionTimeLabel(session);
    const outcome = session.termination_reason && !session.active ? terminationText(session.termination_reason) : nice(session.status || "idle");
    meta.textContent = `${nice(session.mode || "goal")} · ${outcome} · ${session.event_count || 0} events${time ? " · " + time : ""}`;
    meta.title = meta.textContent;
    main.append(title, meta);
    if (session.current) {
      const badge = document.createElement("span");
      badge.className = "session-badge";
      badge.textContent = "current";
      title.append(badge);
    }

    const actions = document.createElement("div");
    actions.className = "session-rowactions";
    const renameBtn = document.createElement("button");
    renameBtn.title = "Rename session";
    renameBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
    renameBtn.addEventListener("click", (e) => { e.stopPropagation(); beginRename(item, session); });
    const exportBtn = document.createElement("button");
    exportBtn.title = "Export this session";
    exportBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>';
    exportBtn.addEventListener("click", (e) => { e.stopPropagation(); openExportMenu(session.run_id); });
    const delBtn = document.createElement("button");
    delBtn.className = "del";
    delBtn.title = "Delete session (wipes its logs)";
    delBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>';
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteSession(session); });
    actions.append(renameBtn, exportBtn, delBtn);

    item.append(dot, main);
    main.append(actions);
    item.addEventListener("click", () => selectSession(session));
    return item;
  }

  async function refreshSessions() {
    const list = $("sessionList");
    if (!list) return;
    let ok, data;
    try {
      ({ ok, data } = await api("sessions"));
    } catch (_) {
      return; // transient backend/network blip; the next tick retries
    }
    if (!ok) return;
    list.replaceChildren();
    const sessions = (data.sessions || []).filter(s => !state.pendingDeletes.has(s.run_id));
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "session-empty";
      empty.textContent = "No session yet. Start a task to create one.";
      list.append(empty);
      return;
    }
    for (const session of sessions) list.append(sessionItem(session));
  }

  function beginRename(item, session) {
    if (item.querySelector(".session-rename")) return;
    const input = document.createElement("input");
    input.className = "session-rename";
    input.value = session.title || "";
    input.maxLength = 120;
    input.placeholder = "Session title";
    item.querySelector(".session-main").append(input);
    input.focus();
    input.select();
    const commit = async (save) => {
      const value = input.value.trim();
      input.remove();
      if (!save) return;
      const { ok, data } = await api(`sessions/${encodeURIComponent(session.run_id)}/rename`, { title: value });
      if (!ok) return toast(data.error || "Could not rename session.", "error");
      toast("Session renamed.", "success");
      refreshSessions();
    };
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      if (e.key === "Escape") { e.preventDefault(); commit(false); }
    });
    input.addEventListener("blur", () => commit(true));
  }

  async function deleteSession(session) {
    if (!window.confirm(`Delete "${session.title || "this session"}"? Its logs and its saved file in the sessions folder are permanently removed. This cannot be undone.`)) return;
    try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(session.run_id)}`, {
      method: "DELETE", headers: { "X-Hephestus-Token": token },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return toast(data.error || "Could not delete session.", "error");
    state.pendingDeletes.delete(session.run_id);
    toast("Session and its logs deleted.", "success");
    } catch (_) {
      state.pendingDeletes.add(session.run_id);
      toast("Removed from this view. Deletion will retry when the server reconnects.");
      document.querySelectorAll(".session-item").forEach(item => {
        if (item.dataset.runId === session.run_id) item.remove();
      });
    }
    newSession();
    refreshSessions();
  }

  function showLiveView() {
    state.draft = false;
    state.selectedSession = null;
    const feed = $("activityFeed");
    if (feed) { delete feed.dataset.live; }
    // Re-sync from the live run; a fresh poll repaints panels from current logs.
    if (liveCursor) { liveCursor.runId = null; liveCursor.lastSequence = 0; liveCursor.seen.clear(); }
    resetLivePanels();
    pollState();
    refreshSessions();
  }

  function resetLivePanels() {
    put($("requestEcho"), "Your submitted task will appear here.", true);
    put($("commandValue"), "No command proposed yet.", true);
    put($("commandStatus"), "Waiting for a plan");
    clearEvidence();
    const feed = $("activityFeed");
    if (feed) {
      feed.replaceChildren();
      delete feed.dataset.live;
      const ready = activityRow({ event: "info", data: {} });
      ready.querySelector(".activity-name").textContent = "Ready";
      ready.querySelector(".activity-detail").textContent = "Start a task to see the live operator-facing planning trace.";
      feed.append(ready);
      $("activityCount").textContent = "0";
    }
  }

  async function selectSession(session) {
    if (session.current && !state.selectedSession && !state.draft) return;
    const { ok, data } = await api(`sessions/${encodeURIComponent(session.run_id)}`);
    if (!ok) return toast(data.error || "Could not open session.", "error");
    const logs = data.logs || [];
    if (session.current) {
      // Returning to the live session.
      state.selectedSession = null;
      showLiveView();
      return;
    }
    state.selectedSession = session.run_id;
    state.draft = false;
    resetQa();
    closeSidebarOnMobile();

    // Rebuild the activity feed (read-only).
    const feed = $("activityFeed");
    feed.replaceChildren();
    feed.dataset.live = "1";
    const banner = document.createElement("div");
    banner.className = "history-banner";
    const ended = data.state && data.state.termination_reason ? ` · ${terminationText(data.state.termination_reason)}` : "";
    banner.textContent = `Viewing a saved session${ended}. It is stored on disk in the sessions folder; viewing does not resume or change it.`;
    feed.append(banner);
    for (const entry of logs) feed.append(activityRow(entry));
    $("activityCount").textContent = String(feed.querySelectorAll(".activity").length);

    // Rebuild the evidence console (read-only) from this session's commands.
    clearEvidence("No command produced output in this session.");
    const list = evidenceList();
    const outputs = logs.filter((e) => e.event === "execution_result" && (e.data || {}).remote_status !== "update");
    if (outputs.length) {
      list.replaceChildren();
      outputs.forEach((entry, i) => list.append(evidenceBlock(i + 1, entry)));
      $("evidenceCount").textContent = String(outputs.length);
    }

    // Request / latest command echoes.
    const start = logs.find((e) => e.event === "start_run");
    put($("requestEcho"), (start && start.data && start.data.goal) || session.goal || "", false);
    const lastProposed = [...logs].reverse().find((e) => e.event === "proposed_commands");
    const command = lastProposed ? (lastProposed.data.commands || []).join("\n") : "";
    put($("commandValue"), command || "No command proposed in this session.", !command);
    put($("commandStatus"), `Viewing session · ${nice(session.status || "")}`);

    refreshSessions();
  }

  function newSession() {
    state.draft = true;
    state.selectedSession = null;
    resetQa();
    $("goalInput").value = "";
    $("contextInput").value = "";
    if (liveCursor) { liveCursor.runId = null; liveCursor.lastSequence = 0; liveCursor.seen.clear(); }
    resetLivePanels();
    renderProgress({});
    refreshSessions();
    closeSidebarOnMobile();
    $("goalInput").focus();
    toast("Ready for a new session.");
  }

  // ---------- export menu ----------
  function exportTargetRunId() {
    if (state.draft) return "";
    return state.selectedSession || state.currentRunId || "";
  }

  function openExportMenu(runId) {
    const menu = $("exportMenu");
    if (!menu) return;
    menu.dataset.runId = runId || exportTargetRunId();
    menu.classList.add("open");
  }

  function toggleExportMenu() {
    const menu = $("exportMenu");
    if (!menu) return;
    if (menu.classList.contains("open")) { menu.classList.remove("open"); return; }
    menu.dataset.runId = exportTargetRunId();
    menu.classList.add("open");
  }

  async function exportSession(format, runId) {
    const target = runId || $("exportMenu").dataset.runId || exportTargetRunId();
    let url = `/api/export?format=${format}`;
    if (target) url += `&run_id=${encodeURIComponent(target)}`;
    toast(format === "pdf" ? "Building the PDF handoff…" : "Preparing the JSON export…");
    try {
      const response = await fetch(url, { headers: { "X-Hephestus-Token": token } });
      if (!response.ok) {
        const failure = await response.json().catch(() => ({}));
        throw new Error(failure.error || `Export failed (${response.status}).`);
      }
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = /filename=([^;]+)/.exec(disposition);
      const filename = match ? match[1].trim() : `hephestus-handoff.${format}`;
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 2000);
      toast(format === "pdf" ? "PDF evidence report downloaded." : "JSON export downloaded.", "success");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  // ---------- start / stop / finish ----------
  async function start() {
    const goal = $("goalInput").value.trim();
    if (state.model.status !== "loaded") { toast("Load a model from the Models tab before starting a task.", "error"); return; }
    if (goal.length < 5) { toast("Enter a task of at least five characters.", "error"); $("goalInput").focus(); return; }
    state.selectedSession = null;
    state.draft = false;
    if (liveCursor) { liveCursor.runId = null; liveCursor.lastSequence = 0; liveCursor.seen.clear(); }
    resetLivePanels();
    put($("requestEcho"), goal, false);
    put($("commandStatus"), "Model is starting…");
    renderProgress({ percent: 4, label: "Preparing model — stage estimate", max_steps: state.maxSteps });
    const { ok, data } = await api("start", { goal, context: $("contextInput").value.trim(), mode: state.taskMode });
    if (!ok) return toast(data.error || "Could not start the task.", "error");
    toast("Task started. The working view will update live.", "success");
    refreshSessions();
    startStream();
  }

  async function stop() {
    try {
    const { ok, data } = await api("stop", {});
    if (!ok) return toast(data.error || "No active run.", "error");
    $("approvalDialog").classList.remove("open");
    toast(data.warning || "Stopping the task and disconnecting the remote worker.", data.warning ? "error" : "info");
    await pollState();
    } catch (_) { toast("Cannot reach the server to confirm Stop. The worker stops when its relay connection fails.", "error"); }
  }

  // ---------- settings ----------
  const SETTINGS_SECTIONS = [
    {
      title: "Inference behaviour", desc: "How the local model plans before proposing a command.",
      icon: '<path d="M12 2a7 7 0 0 0-4 12.7V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.3A7 7 0 0 0 12 2z"/><path d="M9 21h6"/>',
      fields: [
        { key: "max_tokens", id: "settingsTokens", label: "Response token budget", hint: "Max tokens generated per model call.", type: "number", min: 64, max: 8192, step: 64 },
        { key: "temperature", id: "settingsTemperature", label: "Creativity (temperature)", hint: "0 = deterministic, 2 = very random.", type: "range", min: 0, max: 2, step: 0.05 },
        { key: "top_p", id: "settingsTopP", label: "Diversity (top-p)", hint: "Nucleus sampling cutoff.", type: "range", min: 0.1, max: 1, step: 0.05 },
        { key: "max_steps", id: "settingsSteps", label: "Maximum iterations", hint: "How many command cycles a goal run may take.", type: "number", min: 1, max: 50, step: 1 },
      ],
    },
    {
      title: "Model loading", desc: "Changing these reloads the local model. Use conservative values if unstable.",
      icon: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M7 20h10M9 16v4M15 16v4"/>',
      loader: true,
      fields: [
        { key: "llm_context_size", id: "settingsContext", label: "Context window", hint: "Tokens of context the model holds.", type: "number", min: 512, max: 32768, step: 512 },
        { key: "llm_gpu_auto", id: "settingsGpuAuto", label: "Automatic GPU layers", hint: "Sizes GPU offload from free VRAM and the model. Turn off to set layers yourself.", type: "toggle" },
        { key: "llm_gpu_layers", id: "settingsGpu", label: "GPU layers", hint: "-1 offloads all layers to the GPU.", type: "number", min: -1, max: 200, step: 1 },
        { key: "llm_threads", id: "settingsThreads", label: "CPU threads", hint: "Threads for CPU inference.", type: "number", min: 1, max: 128, step: 1 },
        { key: "llm_batch_size", id: "settingsBatch", label: "Batch size", hint: "Prompt batch size per pass.", type: "number", min: 32, max: 2048, step: 32 },
      ],
    },
    {
      title: "Safety &amp; scope", desc: "Automatic review of risky commands and enforcement of your rules of engagement.",
      icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
      fields: [
        { key: "safety_review", id: "settingsSafetyReview", label: "AI safety &amp; scope review", hint: "When on, the model reviews each state-changing command against your goal and scope before it runs, and can block it. It is goal-aware: commands that carry out the authorised goal are allowed. Off by default — when off it never runs, even if a scope is set.", type: "toggle" },
        { key: "command_policy", id: "settingsCommandPolicy", label: "Dangerous-command policy", hint: "Blocks known-dangerous commands (disk format, diskpart/bcdedit, recursive deletes, fork bombs, reverse shells, custom deny/allow lists). On by default. Structural checks (length, truncated/placeholder commands, invalid PowerShell) always run and are not affected by this switch.", type: "toggle", danger: true, dangerConfirm: "[DANGEROUS] You are disabling the dangerous-command policy GLOBALLY.\n\nHephestus will no longer block destructive primitives such as disk format, diskpart, bcdedit, recursive deletes, fork bombs or reverse shells. Only structural checks and your own manual approval will stand between a proposal and execution.\n\nUse this only for minor, low-risk engagements or tests you fully trust. Disable the policy?" },
      ],
    },
    {
      title: "Execution &amp; limits", desc: "Guardrails for how commands run and how much evidence is retained.",
      icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
      fields: [
        { key: "command_timeout_seconds", id: "settingsCmdTimeout", label: "Command timeout (s)", hint: "Max seconds a single command may run.", type: "number", min: 5, max: 3600, step: 5 },
        { key: "run_timeout_seconds", id: "settingsRunTimeout", label: "Run timeout (s)", hint: "Whole-run limit. 0 = no limit.", type: "number", min: 0, max: 86400, step: 30 },
        { key: "max_repeated_failures", id: "settingsMaxFailures", label: "Max repeated failures", hint: "Stop after this many repeats of a failing command.", type: "number", min: 1, max: 20, step: 1 },
        { key: "max_context_tokens", id: "settingsMaxContext", label: "Working context (tokens)", hint: "Max prompt tokens sent to the model per call; trims oldest evidence to fit.", type: "number", min: 512, max: 32768, step: 256 },
        { key: "observation_max_chars", id: "settingsObsChars", label: "Model output window (chars)", hint: "How much of each command's output the model reads (head + tail).", type: "number", min: 200, max: 20000, step: 100 },
        { key: "output_max_chars", id: "settingsOutputChars", label: "Output capture (chars)", hint: "Max characters captured per command for the operator and export.", type: "number", min: 1000, max: 500000, step: 1000 },
      ],
    },
  ];
  const SETTINGS_FIELDS = SETTINGS_SECTIONS.flatMap((section) => section.fields);
  const NUMERIC_FIELDS = SETTINGS_FIELDS.filter((f) => f.type !== "toggle");
  const TOGGLE_FIELDS = SETTINGS_FIELDS.filter((f) => f.type === "toggle");
  const DANGER_FIELDS = TOGGLE_FIELDS.filter((f) => f.danger);
  const settingIds = Object.fromEntries(NUMERIC_FIELDS.map((f) => [f.key, f.id]));

  function buildSettings() {
    const host = document.querySelector("#settingsView .settings");
    if (!host || host.dataset.built) return;
    host.dataset.built = "1";
    host.innerHTML = `
      <div class="settings-hero">
        <div class="hero-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></div>
        <div><h1>Model &amp; run settings</h1><p>These controls tune the local model and run guardrails. Changes are saved to disk and restored automatically the next time the server starts.</p></div>
      </div>
      <div class="settings-notice"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg><span>Model-loading values reload the local model when applied and are unavailable while a task is active. "Reset to defaults" restores the balanced, best-tested configuration.</span></div>
      <div class="settings-grid"></div>
      <div class="settings-bar">
        <span id="settingsStatus" class="settings-status">Loading current settings…</span>
        <button id="resetSettingsBtn" class="btn ghost"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 2.6-6.4M3 4v4h4"/></svg>Reset to defaults</button>
        <button id="saveSettingsBtn" class="btn primary">Apply settings</button>
      </div>`;
    const grid = host.querySelector(".settings-grid");
    for (const section of SETTINGS_SECTIONS) {
      const card = document.createElement("section");
      card.className = "setting-card";
      const rows = section.fields.map((f) => {
        let control;
        if (f.type === "range") {
          control = `<input id="${f.id}" type="range" min="${f.min}" max="${f.max}" step="${f.step}"><span class="sr-value" id="${f.id}Value"></span>`;
        } else if (f.type === "toggle") {
          control = `<label class="switch"><input id="${f.id}" type="checkbox"><span class="switch-track"><span class="switch-knob"></span></span></label>`;
        } else {
          control = `<input id="${f.id}" type="number" min="${f.min}" max="${f.max}" step="${f.step}">`;
        }
        return `<div class="setting-row" data-field="${f.id}"><div class="sr-label"><label for="${f.id}">${f.label}</label><span class="sr-hint" id="${f.id}Hint">${f.hint}</span></div><div class="sr-control">${control}</div></div>`;
      }).join("");
      card.innerHTML = `<div class="setting-card-head"><span class="sc-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${section.icon}</svg></span><div><h2>${section.title}</h2><p>${section.desc}</p></div></div><div class="setting-card-body">${rows}</div>`;
      grid.append(card);
    }
    const notify = document.createElement("section");
    notify.className = "setting-card notify-card";
    notify.innerHTML = `<div class="setting-card-head"><span class="sc-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg></span><div><h2>Notification sounds</h2><p>Plays when a run finishes, fails, needs your approval, or a model finishes loading. Saved in this browser.</p></div></div>
      <div class="setting-card-body">
        <div class="setting-row"><div class="sr-label"><label for="soundEnabled">Sound notifications</label><span class="sr-hint">Turn off to mute every sound (also the speaker button in the top bar).</span></div><div class="sr-control"><label class="switch"><input id="soundEnabled" type="checkbox"><span class="switch-track"><span class="switch-knob"></span></span></label></div></div>
        <div class="setting-row"><div class="sr-label"><label for="soundVolume">Volume</label><span class="sr-hint">Loud by default so you hear it from across the room.</span></div><div class="sr-control"><input id="soundVolume" type="range" min="0.1" max="1" step="0.05"><span class="sr-value" id="soundVolumeValue"></span></div></div>
        <div class="setting-row full"><div class="sr-label"><label>Preview</label><span class="sr-hint">Each event has its own sound — tap to hear it.</span></div><div class="sr-control notify-tests">
          <button class="btn sm" type="button" data-sound="complete">Run complete</button>
          <button class="btn sm" type="button" data-sound="approval">Approval needed</button>
          <button class="btn sm" type="button" data-sound="error">Run failed</button>
          <button class="btn sm" type="button" data-sound="loaded">Model loaded</button>
        </div></div>
      </div>`;
    grid.append(notify);
    $("soundEnabled").checked = Sound.enabled;
    $("soundEnabled").addEventListener("change", (e) => { Sound.setEnabled(e.target.checked); if (e.target.checked) Sound.play("loaded"); });
    $("soundVolume").value = String(Sound.volume);
    put($("soundVolumeValue"), `${Math.round(Sound.volume * 100)}%`);
    $("soundVolume").addEventListener("input", (e) => {
      Sound.volume = Number(e.target.value);
      store.set("hephestus.volume", String(Sound.volume));
      put($("soundVolumeValue"), `${Math.round(Sound.volume * 100)}%`);
    });
    $("soundVolume").addEventListener("change", () => Sound.play("complete", true));
    notify.querySelectorAll("[data-sound]").forEach((b) => b.addEventListener("click", () => Sound.play(b.dataset.sound, true)));

    $("saveSettingsBtn").addEventListener("click", saveSettings);
    $("resetSettingsBtn").addEventListener("click", resetSettings);
    NUMERIC_FIELDS.forEach((f) => {
      if (f.type === "range") $(f.id).addEventListener("input", () => updateRangeLabel(f));
    });
    const auto = $("settingsGpuAuto");
    if (auto) auto.addEventListener("change", () => syncGpuAuto());
    // A dangerous control (e.g. disabling the dangerous-command policy globally)
    // must make the operator confirm a [DANGEROUS] prompt before it turns off.
    DANGER_FIELDS.forEach((f) => {
      const input = $(f.id);
      const row = document.querySelector(`.setting-row[data-field="${f.id}"]`);
      if (!input) return;
      const paint = () => { if (row) row.classList.toggle("danger-active", !input.checked); };
      input.addEventListener("change", () => {
        if (!input.checked) {
          const message = f.dangerConfirm || "[DANGEROUS] Disable this safeguard globally?";
          if (!window.confirm(message)) {
            input.checked = true;  // operator declined — keep the safeguard on
          } else {
            toast("[DANGEROUS] " + f.label + " disabled globally.", "error");
          }
        }
        paint();
      });
      paint();
    });
  }

  function updateRangeLabel(field) {
    const value = Number($(field.id).value || 0);
    put($(field.id + "Value"), value.toFixed(2));
  }

  function gbString(mib) {
    return `${(Number(mib || 0) / 1024).toFixed(1)} GB`;
  }

  function syncGpuAuto() {
    const auto = $("settingsGpuAuto");
    const field = $("settingsGpu");
    const hint = $("settingsGpuHint");
    if (!auto || !field) return;
    const on = auto.checked;
    const suggestion = state.gpuSuggestion || {};
    const layers = suggestion.layers;
    const layersText = layers === -1 ? "all layers" : `${layers} layers`;
    field.disabled = on;
    if (on) {
      if (field.dataset.manual === undefined) field.dataset.manual = field.value;
      if (suggestion.available && typeof layers === "number") field.value = layers;
    } else if (field.dataset.manual !== undefined) {
      field.value = field.dataset.manual;
      delete field.dataset.manual;
    }
    if (!hint) return;
    if (!suggestion.available) {
      hint.textContent = on
        ? "Auto: no NVIDIA GPU detected — the model runs on CPU."
        : "-1 offloads all layers to the GPU.";
    } else if (on) {
      hint.textContent = `Auto: using ${layersText} · ${gbString(suggestion.free_mib)} free of ${gbString(suggestion.total_mib)}.`;
    } else {
      hint.textContent = `-1 offloads all layers. Auto would suggest ${layersText} (${gbString(suggestion.free_mib)} free).`;
    }
  }

  function loadSettingsInto(settings) {
    if (!settings) return;
    for (const [key, id] of Object.entries(settingIds)) {
      const input = $(id);
      if (!input) continue;
      input.value = settings[key] != null ? settings[key] : "";
      input.defaultValue = input.value;
      if (id === "settingsGpu") delete input.dataset.manual;
    }
    for (const f of TOGGLE_FIELDS) {
      const input = $(f.id);
      if (input) input.checked = !!settings[f.key];
    }
    DANGER_FIELDS.forEach((f) => {
      const input = $(f.id);
      const row = document.querySelector(`.setting-row[data-field="${f.id}"]`);
      if (input && row) row.classList.toggle("danger-active", !input.checked);
    });
    NUMERIC_FIELDS.forEach((f) => { if (f.type === "range") updateRangeLabel(f); });
    syncGpuAuto();
    setThinking(settings.thinking_level || "balanced", false);
  }

  async function loadSettings() {
    buildSettings();
    const { ok, data } = await api("settings");
    if (!ok) { put($("settingsStatus"), data.error || "Could not load settings."); return; }
    state.gpuSuggestion = data.gpu_suggestion || null;
    loadSettingsInto(data);
    state.loader = { ...data };
    state.maxSteps = data.max_steps || state.maxSteps;
    const status = $("settingsStatus");
    status.className = "settings-status";
    status.textContent = "Settings loaded · saved values restore on restart.";
  }

  function collectSettings() {
    const payload = {};
    for (const [key, id] of Object.entries(settingIds)) {
      if (id === "settingsGpu" && $("settingsGpuAuto") && $("settingsGpuAuto").checked
          && $(id).dataset.manual !== undefined) {
        payload[key] = Number($(id).dataset.manual);
      } else {
        payload[key] = Number($(id).value);
      }
    }
    for (const f of TOGGLE_FIELDS) payload[f.key] = !!$(f.id).checked;
    return payload;
  }

  async function saveSettings() {
    const payload = collectSettings();
    const loaderChanged = ["llm_context_size", "llm_gpu_layers", "llm_gpu_auto", "llm_threads", "llm_batch_size"]
      .some((key) => String(payload[key]) !== String(state.loader[key]));
    const status = $("settingsStatus");
    status.className = "settings-status";
    status.textContent = loaderChanged ? "Reloading the local model with new settings…" : "Applying settings…";
    const { ok, data } = await api("settings", payload);
    if (!ok) { status.className = "settings-status error"; status.textContent = data.error; return toast(data.error, "error"); }
    if (data.gpu_suggestion) state.gpuSuggestion = data.gpu_suggestion;
    loadSettingsInto(data.settings);
    state.loader = { ...data.settings };
    state.maxSteps = data.settings.max_steps;
    status.className = "settings-status saved";
    status.textContent = data.reloaded ? "Saved · model reloaded · restores on restart." : "Saved · applied and persisted to disk.";
    toast(data.reloaded ? "Settings saved and model reloaded." : "Settings saved.", "success");
  }

  async function resetSettings() {
    if (!window.confirm("Reset all model and run settings to the balanced defaults?")) return;
    const status = $("settingsStatus");
    status.className = "settings-status";
    status.textContent = "Restoring defaults…";
    const { ok, data } = await api("settings/reset", {});
    if (!ok) { status.className = "settings-status error"; status.textContent = data.error; return toast(data.error, "error"); }
    if (data.gpu_suggestion) state.gpuSuggestion = data.gpu_suggestion;
    loadSettingsInto(data.settings);
    state.loader = { ...data.settings };
    state.maxSteps = data.settings.max_steps;
    status.className = "settings-status saved";
    status.textContent = data.reloaded ? "Defaults restored · model reloaded." : "Defaults restored and saved.";
    toast("Settings reset to defaults.", "success");
  }

  // ---------- thinking level ----------
  function syncDial(level) {
    const dial = $("thinkingDial");
    if (!dial) return;
    dial.classList.remove("quick", "balanced", "deep");
    dial.classList.add(level);
    const names = { quick: "Quick", balanced: "Balanced", deep: "Deep" };
    put($("thinkingDialLabel"), names[level] || nice(level));
  }

  function closeThinkingPop() {
    const pop = $("thinkingPop");
    if (pop) pop.classList.remove("open");
    const dial = $("thinkingDial");
    if (dial) dial.setAttribute("aria-expanded", "false");
  }

  function setThinking(level, send) {
    document.querySelectorAll("#thinkingLevel button").forEach((b) => b.classList.toggle("selected", b.dataset.level === level));
    syncDial(level);
    if (!send) return;
    api("thinking-level", { thinking_level: level }).then(({ ok, data }) => {
      if (!ok) return toast(data.error || "Could not apply thinking profile.", "error");
      loadSettingsInto(data.settings);
      state.loader = { ...state.loader, ...data.settings };
      toast(nice(level) + " thinking selected.", "success");
    });
  }

  function setMode(mode) {
    state.taskMode = mode;
    document.querySelectorAll("#taskMode button").forEach((b) => b.classList.toggle("selected", b.dataset.mode === mode));
  }

  // ---------- GPU telemetry (rail) ----------
  // A persistent server-side sampler records every reading (0.5 s cadence);
  // the browser fetches only new samples and draws a time-true, smoothly
  // scrolling chart whose colour follows load: green → cyan → amber → red.
  const GPU_WINDOW_MS = 60000;
  function gibibytes(mebibytes) { return `${(Number(mebibytes || 0) / 1024).toFixed(1)} GB`; }

  function gpuLevel(util) { return util >= 90 ? "peak" : util >= 70 ? "hot" : "normal"; }

  function setGpuUnavailable(message) {
    put($("gpuTelemetryState"), "");
    const big = $("gpuBig");
    if (big) big.innerHTML = '<small>offline</small>';
    put($("gpuTelemetryDetail"), message);
    const card = $("gpuCard");
    if (card) card.dataset.level = "normal";
  }

  function renderGpuStats() {
    const s = state.gpu.latest;
    if (!s) return;
    const recent = state.gpu.samples.filter((x) => x.t >= s.t - GPU_WINDOW_MS);
    const peak = recent.reduce((m, x) => Math.max(m, x.utilization), 0);
    const avg = recent.length ? recent.reduce((m, x) => m + x.utilization, 0) / recent.length : 0;
    const big = $("gpuBig");
    if (big) big.innerHTML = `${s.utilization}<small>%</small>`;
    $("gpuCard").dataset.level = gpuLevel(s.utilization);
    put($("gpuStatVram"), `${gibibytes(s.memory_used)} / ${gibibytes(s.memory_total)}`);
    put($("gpuStatTemp"), `${s.temperature}°C`);
    put($("gpuStatPower"), s.power != null ? `${Math.round(s.power)} W` : "—");
    put($("gpuStatPeak"), `${peak}% · avg ${Math.round(avg)}%`);
    put($("gpuTelemetryDetail"), state.gpu.name ? `${state.gpu.name} · ${Math.round(1000 / (state.gpu.interval || 500) * 10) / 10} samples/s` : "Live NVIDIA readings");
  }

  function smoothPath(ctx, points) {
    // Monotone cubic interpolation: smooth without overshooting real values.
    const n = points.length;
    if (n < 2) return;
    const dx = [], dy = [], m = [], t = [];
    for (let i = 0; i < n - 1; i++) { dx[i] = points[i + 1][0] - points[i][0] || 1e-6; dy[i] = points[i + 1][1] - points[i][1]; m[i] = dy[i] / dx[i]; }
    t[0] = m[0]; t[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
    ctx.moveTo(points[0][0], points[0][1]);
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      ctx.bezierCurveTo(points[i][0] + h, points[i][1] + t[i] * h, points[i + 1][0] - h, points[i + 1][1] - t[i + 1] * h, points[i + 1][0], points[i + 1][1]);
    }
  }

  function drawGpu(now) {
    const canvas = $("gpuCanvas");
    if (!canvas) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const top = 8, bottom = height - 4;
    const y = (pct) => bottom - (Math.max(0, Math.min(100, pct)) / 100) * (bottom - top);
    // grid
    ctx.lineWidth = 1;
    for (const level of [25, 50, 75, 100]) {
      ctx.strokeStyle = level === 100 ? "#ff71841f" : "#ffffff0d";
      ctx.setLineDash(level === 100 ? [3, 4] : []);
      ctx.beginPath(); ctx.moveTo(0, Math.round(y(level)) + 0.5); ctx.lineTo(width, Math.round(y(level)) + 0.5); ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.fillStyle = "#64789c";
    ctx.font = "9px ui-monospace,Consolas,monospace";
    ctx.fillText("100", 3, y(100) + 10); ctx.fillText("50", 3, y(50) - 2);

    // Shift the clock so the newest point sits at the right edge and glides in.
    const clockSkew = state.gpu.skew || 0;
    const end = now + clockSkew;
    const xOf = (t) => width - ((end - t) / GPU_WINDOW_MS) * width;
    const samples = state.gpu.samples.filter((s) => s.t >= end - GPU_WINDOW_MS - 2000);
    if (samples.length < 2) return;

    // VRAM % (dashed violet, behind)
    const memPoints = samples.map((s) => [xOf(s.t), y((s.memory_used / Math.max(1, s.memory_total)) * 100)]);
    ctx.beginPath(); smoothPath(ctx, memPoints);
    ctx.strokeStyle = "#a08bffb0"; ctx.lineWidth = 1.4; ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]);

    const points = samples.map((s) => [xOf(s.t), y(s.utilization)]);
    const stroke = ctx.createLinearGradient(0, y(100), 0, y(0));
    stroke.addColorStop(0, "#ff5d73"); stroke.addColorStop(0.12, "#ff7a6b"); stroke.addColorStop(0.28, "#ffbd61");
    stroke.addColorStop(0.5, "#5fe0ff"); stroke.addColorStop(1, "#51d99f");

    // depth: a darker, offset copy of the area beneath the main fill
    ctx.save();
    ctx.translate(0, 3);
    ctx.beginPath(); smoothPath(ctx, points);
    ctx.lineTo(points[points.length - 1][0], bottom); ctx.lineTo(points[0][0], bottom); ctx.closePath();
    ctx.fillStyle = "#00000055"; ctx.fill();
    ctx.restore();

    // area fill: load colour fading to transparent at the floor
    ctx.beginPath(); smoothPath(ctx, points);
    ctx.lineTo(points[points.length - 1][0], bottom); ctx.lineTo(points[0][0], bottom); ctx.closePath();
    ctx.save();
    ctx.globalAlpha = 0.38; ctx.fillStyle = stroke; ctx.fill();
    const fade = ctx.createLinearGradient(0, top, 0, bottom);
    fade.addColorStop(0, "#08132500"); fade.addColorStop(1, "#081325f0");
    ctx.globalAlpha = 1; ctx.fillStyle = fade; ctx.fill();
    ctx.restore();

    // glowing line
    ctx.beginPath(); smoothPath(ctx, points);
    ctx.lineWidth = 2.4; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.strokeStyle = stroke;
    ctx.shadowBlur = 10; ctx.shadowColor = state.gpu.latest && state.gpu.latest.utilization >= 70 ? "#ff8a6b" : "#5fe0ff";
    ctx.stroke(); ctx.shadowBlur = 0;

    // spike markers: highlight readings at or above 85 %
    for (const s of samples) {
      if (s.utilization < 85) continue;
      const px = xOf(s.t), py = y(s.utilization);
      ctx.beginPath(); ctx.arc(px, py, 2.2, 0, Math.PI * 2);
      ctx.fillStyle = s.utilization >= 95 ? "#ff5d73" : "#ffbd61"; ctx.fill();
    }

    // peak label
    const visible = samples.filter((s) => xOf(s.t) >= 0);
    const peak = visible.reduce((best, s) => (!best || s.utilization > best.utilization ? s : best), null);
    if (peak && peak.utilization > 5) {
      const px = Math.max(16, Math.min(width - 30, xOf(peak.t))), py = y(peak.utilization);
      ctx.fillStyle = "#ffffffcc"; ctx.font = "bold 9px ui-monospace,Consolas,monospace";
      ctx.fillText(`▲${peak.utilization}%`, px - 14, Math.max(9, py - 5));
    }

    // live head with pulsing halo
    const last = points[points.length - 1];
    const pulse = (Math.sin(now / 260) + 1) / 2;
    ctx.beginPath(); ctx.arc(last[0], last[1], 4 + pulse * 4, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(95,224,255,${0.12 + 0.12 * (1 - pulse)})`; ctx.fill();
    ctx.beginPath(); ctx.arc(last[0], last[1], 3, 0, Math.PI * 2);
    ctx.fillStyle = "#fff"; ctx.fill();
  }

  function gpuLoop(now) {
    // ~30 fps is plenty for a 60 s window and keeps the page light.
    if (!document.hidden && now - (state.gpu.drawn || 0) > 33) {
      state.gpu.drawn = now;
      drawGpu(Date.now());
    }
    state.gpu.frame = requestAnimationFrame(gpuLoop);
  }

  async function pollGpu() {
    let delay = 1000;
    try {
      const { ok, data } = await api(`telemetry/gpu?since=${state.gpu.lastT || 0}`);
      if (ok && data.available) {
        if (data.name) state.gpu.name = data.name;
        state.gpu.interval = data.interval_ms;
        for (const sample of data.history || []) {
          if (sample.t > state.gpu.lastT) { state.gpu.samples.push(sample); state.gpu.lastT = sample.t; }
        }
        const cutoff = state.gpu.lastT - GPU_WINDOW_MS - 5000;
        while (state.gpu.samples.length && state.gpu.samples[0].t < cutoff) state.gpu.samples.shift();
        const latest = (data.devices && data.devices[0]) || state.gpu.samples[state.gpu.samples.length - 1];
        if (latest) {
          state.gpu.latest = latest;
          // Lag the drawing clock by ~1.5 samples so the line never runs past its data.
          if (latest.t) state.gpu.skew = latest.t - Date.now() - (data.interval_ms || 500) * 0.5;
          renderGpuStats();
          put($("gpuTelemetryState"), "live");
        } else {
          put($("gpuTelemetryState"), "warming up…");
        }
      } else {
        delay = 10000;
        setGpuUnavailable("NVIDIA telemetry is not available to this process.");
      }
    } catch (_) {
      delay = 5000;
      setGpuUnavailable("Telemetry paused — the server is unreachable.");
    }
    setTimeout(pollGpu, document.hidden ? Math.max(delay, 4000) : delay);
  }

  function buildRailExtras() {
    const rail = $("activityRail");
    const feed = $("activityFeed");
    if (!rail || !feed) return;
    const card = document.createElement("section");
    card.className = "gpu-telemetry";
    card.id = "gpuCard";
    card.dataset.level = "normal";
    card.innerHTML = `
      <div class="gpu-telemetry-top">
        <span class="gpu-title"><svg class="ico ico-gpu" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2.5" opacity=".5"/><path class="bar" d="M8 15v-4"/><path class="bar b2" d="M12 15V9"/><path class="bar b3" d="M16 15v-3"/></svg>GPU monitor <span id="gpuTelemetryState" class="subtle">connecting…</span></span>
        <span id="gpuBig" class="gpu-big"><small>…</small></span>
      </div>
      <canvas id="gpuCanvas" class="gpu-canvas" role="img" aria-label="GPU utilization and memory over the last 60 seconds"></canvas>
      <div class="gpu-legend"><span><i class="lu"></i>Utilization</span><span><i class="lm"></i>VRAM</span><span style="margin-left:auto">last 60 s</span></div>
      <div class="gpu-stats">
        <div class="gpu-stat"><span>VRAM</span><strong id="gpuStatVram">—</strong></div>
        <div class="gpu-stat"><span>Temp</span><strong id="gpuStatTemp">—</strong></div>
        <div class="gpu-stat"><span>Power</span><strong id="gpuStatPower">—</strong></div>
        <div class="gpu-stat"><span>Peak 60 s</span><strong id="gpuStatPeak">—</strong></div>
      </div>
      <span id="gpuTelemetryDetail" class="gpu-detail">Local NVIDIA readings only</span>`;
    rail.insertBefore(card, feed);

    const cardQA = document.createElement("section");
    cardQA.className = "qa-card";
    cardQA.id = "qaCard";
    cardQA.innerHTML = `
      <div class="rail-head">
        <span class="qa-head-title"><svg class="ico ico-spark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1M5.6 18.4l2.1-2.1m8.6-8.6 2.1-2.1"/><circle cx="12" cy="12" r="2.5"/></svg>Ask the AI</span>
        <span class="qa-head-tools"><span id="qaScope" class="subtle">Current session</span>
          <button id="qaExpandBtn" class="qa-expand" type="button" title="Expand (Esc to close)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg></button></span>
      </div>
      <div id="qaTranscript" class="qa-transcript" role="log" aria-live="polite"></div>
      <div class="rail-question">
        <textarea id="logQuestion" placeholder="Ask about this session's commands and results… (Enter to send, Shift+Enter for a new line)"></textarea>
        <div class="rail-question-foot"><span id="askHint" class="hint">Answers use only the most relevant evidence from this session.</span><button id="askLogsBtn" class="btn sm primary" type="button">Ask</button></div>
      </div>`;
    rail.parentElement.append(cardQA);
    $("askLogsBtn").addEventListener("click", askLogs);
    $("logQuestion").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) { e.preventDefault(); if (!$("askLogsBtn").disabled) askLogs(); }
    });
    $("qaExpandBtn").addEventListener("click", () => toggleQaExpanded());
    resetQa();
  }

  function toggleQaExpanded(force) {
    const card = $("qaCard");
    if (!card) return;
    const open = force === undefined ? !card.classList.contains("expanded") : force;
    card.classList.toggle("expanded", open);
    document.body.classList.toggle("qa-expanded", open);
    $("qaExpandBtn").title = open ? "Collapse (Esc)" : "Expand (Esc to close)";
    if (open) $("logQuestion").focus();
  }

  const QA_SUGGESTIONS = [
    "Summarize what this session found.",
    "Why did the last command fail?",
    "Which commands succeeded?",
    "What should I verify next?",
  ];

  function resetQa() {
    const transcript = $("qaTranscript");
    if (!transcript) return;
    transcript.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "qa-empty";
    const text = document.createElement("div");
    text.textContent = "Ask a question about the commands, outputs and decisions in this session.";
    const chips = document.createElement("div");
    chips.className = "qa-suggestions";
    for (const suggestion of QA_SUGGESTIONS) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.textContent = suggestion;
      chip.addEventListener("click", () => { $("logQuestion").value = suggestion; if (!$("askLogsBtn").disabled) askLogs(); });
      chips.append(chip);
    }
    empty.append(text, chips);
    transcript.append(empty);
    const scope = $("qaScope");
    if (scope) scope.textContent = state.selectedSession ? "Saved session" : "Current session";
  }

  function syncAskAvailability() {
    const input = $("logQuestion");
    const ask = $("askLogsBtn");
    const hint = $("askHint");
    if (!input || !ask) return;
    // Chat is blocked only while the model is actively generating or the run
    // is shutting down. During ``waiting_for_user`` the GPU is idle (the
    // agent is parked on operator approval) so chatting is allowed.
    const busy = state.status === "running" || state.status === "stopping";
    const loaded = state.model.status === "loaded";
    input.disabled = busy;
    ask.disabled = busy || state.asking || !loaded;
    $("dockUnloadBtn").disabled = busy || state.asking || !loaded;
    document.querySelectorAll(".qa-suggestions button").forEach((b) => { b.disabled = ask.disabled; });
    if (hint && !state.asking) {
      hint.textContent = busy ? "Available when the current task finishes."
        : state.status === "waiting_for_user" ? "You can chat while the remote PC decides. The next iteration will queue behind your question."
        : !loaded ? "Load a model in the Models tab to ask questions."
        : "Answers use only the most relevant evidence from this session.";
    }
    const scope = $("qaScope");
    if (scope) scope.textContent = state.selectedSession ? "Saved session" : "Current session";
  }

  async function askLogs() {
    const input = $("logQuestion");
    const ask = $("askLogsBtn");
    const question = input.value.trim();
    if (question.length < 3) return toast("Ask a question of at least three characters.", "error");
    if (state.model.status !== "loaded") return toast("Load a model in the Models tab first.", "error");
    const transcript = $("qaTranscript");
    const empty = transcript.querySelector(".qa-empty");
    if (empty) empty.remove();
    const questionRow = document.createElement("div");
    questionRow.className = "qa-question";
    questionRow.textContent = question;
    transcript.append(questionRow);
    const answer = document.createElement("div");
    answer.className = "log-answer typing";
    answer.textContent = "Selecting the relevant evidence…";
    transcript.append(answer);
    transcript.scrollTop = transcript.scrollHeight;
    input.value = "";
    state.asking = true;
    syncAskAvailability();
    $("startBtn").disabled = true;
    ask.disabled = true; ask.textContent = "Thinking…";
    const started = Date.now();
    const ticker = setInterval(() => {
      const seconds = Math.round((Date.now() - started) / 1000);
      answer.textContent = seconds < 3 ? "Selecting the relevant evidence…" : `The local model is reading the evidence… ${seconds}s`;
    }, 1000);
    // A stuck request must not freeze the panel forever.
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6 * 60 * 1000);
    try {
      const response = await fetch("/api/ask/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Hephestus-Token": token },
        body: JSON.stringify({ question, run_id: state.selectedSession || "" }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const failure = await response.json().catch(() => ({}));
        throw new Error(failure.error || "Could not answer from the logs.");
      }
      // Render the answer as it streams so the first words appear in seconds.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "", text = "", contextChars = 0, done = null, painted = 0;
      const paint = (final) => {
        if (!final && Date.now() - painted < 90) return;
        painted = Date.now();
        clearInterval(ticker);
        answer.classList.remove("typing");
        renderMarkdown(answer, text + (final ? "" : " ▍"));
        const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 140;
        if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
      };
      while (!done) {
        const { value, done: ended } = await reader.read();
        if (ended) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const line = frame.split("\n").find((x) => x.startsWith("data: "));
          if (!line) continue;
          const event = JSON.parse(line.slice(6));
          if (event.type === "meta") contextChars = event.context_chars || 0;
          if (event.type === "delta") { text += event.text; paint(false); }
          if (event.type === "error") throw new Error(event.error);
          if (event.type === "done") done = event;
        }
      }
      if (!done) throw new Error("The answer stream ended unexpectedly.");
      if (done.empty || !text.trim()) text = "The model returned no visible answer. Try rephrasing the question.";
      if (done.truncated) text += "\n\n_(Answer cut short by the response budget — ask a narrower question for more detail.)_";
      paint(true);
      answer.querySelectorAll("pre").forEach((pre) => pre.append(copyButton(() => pre.innerText)));
      const meta = document.createElement("span");
      meta.className = "qa-meta";
      meta.textContent = `Answered in ${done.seconds ?? Math.round((Date.now() - started) / 100) / 10}s from ${Math.round(contextChars / 100) / 10}k characters of selected evidence`;
      answer.after(meta);
    } catch (error) {
      answer.classList.remove("typing");
      const message = error.name === "AbortError" ? "The model took too long to answer. Try a narrower question." : error.message;
      answer.textContent = message;
      input.value = question;
      toast(message, "error");
    } finally {
      clearInterval(ticker);
      clearTimeout(timeout);
      state.asking = false;
      ask.textContent = "Ask";
      $("startBtn").disabled = activeStates.has(state.status) || state.model.status !== "loaded";
      syncAskAvailability();
      transcript.scrollTop = transcript.scrollHeight;
    }
  }

  // ---------- model library + dock ----------
  const MODEL_STATE_TEXT = {
    unloaded: "No model loaded",
    loading: "Loading model…",
    loaded: "Model loaded",
    unloading: "Unloading model…",
    error: "Model failed to load",
  };

  function modelForState(model) {
    if (!model || !model.model_file) return null;
    return state.models.find((m) => m.file === model.model_file) || { name: model.model_file.replace(/\.gguf$/i, ""), file: model.model_file, accent: 210 };
  }

  async function coverUrl(model) {
    if (!model || !model.has_cover) return null;
    if (state.coverUrls.has(model.id)) return state.coverUrls.get(model.id);
    try {
      const response = await fetch(`/api/models/cover?id=${encodeURIComponent(model.id)}`, { headers: { "X-Hephestus-Token": token } });
      if (!response.ok) return null;
      const url = URL.createObjectURL(await response.blob());
      state.coverUrls.set(model.id, url);
      return url;
    } catch (_) { return null; }
  }

  function renderModel(model) {
    const previous = state.model.status;
    const previousFile = state.model.model_file;
    state.model = model || { status: "unloaded" };
    const status = state.model.status;
    const dock = $("modelDock");
    const changed = previous !== status || previousFile !== state.model.model_file;
    const dot = $("navModelDot");
    if (dot) dot.dataset.state = status;
    if (changed && previous === "loading" && status === "loaded") {
      Sound.play("loaded");
      toast(`${state.model.model_file} is loaded and ready.`, "success");
    }
    if (changed && previous === "loading" && status === "error") {
      Sound.play("error");
      toast(state.model.error || "The model could not be loaded.", "error");
    }
    if (changed && previous === "loaded" && status === "unloaded" && state.model.notice) toast(state.model.notice, "error");
    if (dock && changed) {
      dock.dataset.state = status;
      const info = modelForState(state.model);
      const art = $("dockArt");
      art.replaceChildren(cartArt(info, null));
      if (info && info.has_cover) coverUrl(info).then((url) => { if (url && state.model.model_file === info.file) art.replaceChildren(cartArt(info, url)); });
      put($("dockState"), MODEL_STATE_TEXT[status] || nice(status));
      if (status === "unloaded") {
        put($("dockName"), "Choose a model to begin");
        put($("dockMeta"), state.model.notice || "The server starts with nothing in memory. Load a model from the library to start tasks or ask questions.");
      } else {
        put($("dockName"), info ? info.name : state.model.model_file);
        const bits = [state.model.model_file];
        if (info && info.size_bytes) bits.push(formatBytes(info.size_bytes));
        if (info && info.quantization) bits.push(info.quantization);
        put($("dockMeta"), status === "error" ? (state.model.error || "Load failed") : bits.join(" · "));
      }
      $("dockUnloadBtn").classList.toggle("hidden", status !== "loaded");
      $("dockLibraryBtn").querySelector("span").textContent = status === "loaded" ? "Switch model" : "Open library";
      if (activeStates.has(state.status) === false) {
        $("startBtn").disabled = state.asking || status !== "loaded";
      }
      syncAskAvailability();
      if ($("modelsView").classList.contains("active") || changed) refreshModelLibrary(false);
    }
    if (dock && (status === "loaded" || status === "loading")) {
      const elapsed = state.model.changed_at ? Math.max(0, Math.round(Date.now() / 1000 - state.model.changed_at)) : 0;
      if (status === "loading") {
        const info = modelForState(state.model);
        put($("dockMeta"), `${state.model.model_file}${info && info.size_bytes ? " · " + formatBytes(info.size_bytes) : ""} · reading weights into memory… ${elapsed}s`);
      } else if (state.model.gpu_layers != null) {
        const info = modelForState(state.model);
        const layers = state.model.gpu_layers === -1 ? "all layers on GPU" : state.model.gpu_layers === 0 ? "CPU only" : `${state.model.gpu_layers} GPU layers`;
        put($("dockMeta"), [state.model.model_file, info && info.size_bytes ? formatBytes(info.size_bytes) : "", info && info.quantization, layers].filter(Boolean).join(" · "));
      }
    }
  }

  async function refreshModelLibrary(showToast) {
    let ok, data;
    try { ({ ok, data } = await api("models")); } catch (_) { return; }
    if (!ok) { if (showToast) toast(data.error || "Could not scan models.", "error"); return; }
    state.models = data.models || [];
    renderLibrary(data);
    renderModel(data.state);
    if (showToast) toast(`${state.models.length} model${state.models.length === 1 ? "" : "s"} found.`, "success");
  }

  function stripItem(label, value, cls = "", copyable = false) {
    const item = document.createElement("div");
    item.className = `strip-item ${cls}`.trim();
    const l = document.createElement("span"); l.textContent = label;
    const v = document.createElement("strong"); v.textContent = value; v.title = value;
    item.append(l, v);
    if (copyable && value) item.append(copyButton(() => value, "inline"));
    return item;
  }

  function renderLibrary(data) {
    const strip = $("libraryStrip");
    const grid = $("modelGrid");
    if (!strip || !grid) return;
    const total = state.models.reduce((sum, m) => sum + (m.size_bytes || 0), 0);
    const loaded = state.models.find((m) => m.status === "loaded");
    strip.replaceChildren(
      stripItem("Folder", data.models_dir || "models", "path", true),
      stripItem("Models", String(state.models.length)),
      stripItem("On disk", formatBytes(total)),
      stripItem("In memory", loaded ? loaded.name : (data.state && data.state.status === "loading" ? "loading…" : "nothing")),
    );
    grid.replaceChildren();
    if (!state.models.length) {
      const empty = document.createElement("div");
      empty.className = "session-empty";
      empty.textContent = "No .gguf files found. Copy a GGUF model into the project's models folder, then press Rescan.";
      grid.append(empty);
      return;
    }
    const busy = data.state && ["loading", "unloading"].includes(data.state.status);
    state.models.forEach((model, index) => grid.append(cartridgeCard(model, busy, index)));
  }

  function cartridgeCard(model, busy, index) {
    const card = document.createElement("article");
    card.className = "cart-card";
    card.dataset.status = model.status;
    card.style.setProperty("--h", String(model.accent));
    card.style.animationDelay = `${Math.min(index, 10) * 40}ms`;
    const stage = document.createElement("div");
    stage.className = "cart-stage";
    const ribbon = document.createElement("span");
    ribbon.className = "cart-ribbon";
    ribbon.innerHTML = "<i></i>";
    ribbon.append(document.createTextNode({ loaded: "Loaded", loading: "Loading", unloading: "Ejecting", error: "Failed" }[model.status] || ""));
    stage.append(ribbon, cartArt(model, null));
    if (model.has_cover) coverUrl(model).then((url) => { if (url) stage.querySelector(".cart").replaceWith(cartArt(model, url)); });

    const info = document.createElement("div");
    info.className = "cart-info";
    const name = document.createElement("div"); name.className = "cart-name"; name.textContent = model.name; name.title = model.name;
    const file = document.createElement("div"); file.className = "cart-file"; file.textContent = model.id; file.title = model.id;
    const tags = document.createElement("div"); tags.className = "cart-tags";
    const addTag = (text, cls = "") => { if (!text) return; const t = document.createElement("span"); t.className = cls; t.textContent = text; tags.append(t); };
    addTag(formatBytes(model.size_bytes), "s");
    addTag(model.size_label);
    addTag(model.quantization, "q");
    addTag(model.arch);
    addTag(model.layers ? `${model.layers} layers` : "");
    addTag(model.context_length ? `${Math.round(model.context_length / 1024)}k ctx` : "");
    if (!model.valid_gguf) addTag("unreadable header");
    info.append(name, file, tags);

    const actions = document.createElement("div");
    actions.className = "cart-actions";
    const main = document.createElement("button");
    main.type = "button";
    const isLoaded = model.status === "loaded";
    main.className = `btn sm ${isLoaded ? "" : "primary"}`;
    main.innerHTML = isLoaded
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12m0 0 4-4m-4 4-4-4M5 21h14"/></svg>Eject'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21V9m0 0-4 4m4-4 4 4M5 3h14"/></svg>Load';
    main.disabled = busy || activeStates.has(state.status) || !model.valid_gguf;
    main.addEventListener("click", () => (isLoaded ? unloadModel() : loadModel(model)));
    const cover = document.createElement("button");
    cover.type = "button";
    cover.className = "btn sm ghost icon-btn";
    cover.title = model.has_cover ? "Change cover image (Shift+click to remove)" : "Set a cover image";
    cover.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>';
    cover.addEventListener("click", (e) => (e.shiftKey && model.has_cover ? removeCover(model) : pickCover(model)));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn sm ghost icon-btn del";
    del.title = "Delete this model file from the project";
    del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>';
    del.disabled = busy || ["loaded", "loading"].includes(model.status);
    del.addEventListener("click", () => deleteModel(model));
    actions.append(main, cover, del);
    card.append(stage, info, actions);
    return card;
  }

  async function loadModel(model) {
    if (activeStates.has(state.status)) return toast("Stop the current task before switching models.", "error");
    Sound.unlock();
    const { ok, data } = await api("models/load", { id: model.id });
    if (!ok) return toast(data.error || "Could not start loading.", "error");
    toast(`Loading ${model.name}… large models can take a minute.`);
    if (data.state) renderModel(data.state);
    refreshModelLibrary(false);
  }

  async function unloadModel() {
    if (state.asking) return toast("Wait for the evidence answer to finish before unloading.", "error");
    if (activeStates.has(state.status)) return toast("Stop the current task before unloading.", "error");
    const dock = $("modelDock");
    if (dock) dock.dataset.state = "unloading";
    state.model = { ...state.model, status: "unloading" };
    document.querySelectorAll('.cart-card[data-status="loaded"]').forEach((c) => { c.dataset.status = "unloading"; });
    const { ok, data } = await api("models/unload", {});
    if (!ok) { toast(data.error || "Could not unload.", "error"); return refreshModelLibrary(false); }
    // Let the eject animation finish before showing the empty slot.
    await new Promise((resolve) => setTimeout(resolve, 900));
    state.model = { status: "unloading" };
    renderModel(data.state);
    toast("Model unloaded; memory released.", "success");
  }

  async function deleteModel(model) {
    const typed = window.prompt(`Permanently delete "${model.id}" (${formatBytes(model.size_bytes)}) from the project's models folder?\n\nThis cannot be undone. Type the file name to confirm:\n${model.file}`);
    if (typed === null) return;
    if (typed.trim() !== model.file) return toast("The file name did not match; nothing was deleted.", "error");
    const { ok, data } = await api("models/delete", { id: model.id, confirm: typed.trim() });
    if (!ok) return toast(data.error || "Could not delete the model.", "error");
    state.coverUrls.delete(model.id);
    toast(`${data.file} deleted.`, "success");
    refreshModelLibrary(false);
  }

  function pickCover(model) {
    state.coverTarget = model;
    const input = $("coverInput");
    input.value = "";
    input.click();
  }

  async function uploadCover(file) {
    const model = state.coverTarget;
    if (!model || !file) return;
    if (file.size > 3 * 1024 * 1024) return toast("Cover images must be 3 MB or smaller.", "error");
    try {
      const response = await fetch(`/api/models/cover?id=${encodeURIComponent(model.id)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Hephestus-Token": token }, body: file,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not save the cover.");
      const old = state.coverUrls.get(model.id);
      if (old) URL.revokeObjectURL(old);
      state.coverUrls.delete(model.id);
      toast("Cover image saved.", "success");
      refreshModelLibrary(false);
    } catch (error) { toast(error.message, "error"); }
  }

  async function removeCover(model) {
    await fetch(`/api/models/cover?id=${encodeURIComponent(model.id)}`, { method: "DELETE", headers: { "X-Hephestus-Token": token } });
    state.coverUrls.delete(model.id);
    toast("Cover removed.", "success");
    refreshModelLibrary(false);
  }

  function showView(viewId) {
    document.querySelectorAll(".nav button").forEach((x) => x.classList.toggle("active", x.dataset.view === viewId));
    document.querySelectorAll(".view").forEach((x) => x.classList.toggle("active", x.id === viewId));
    if (viewId === "settingsView") loadSettings();
    if (viewId === "modelsView") refreshModelLibrary(false);
    scrollTo({ top: 0, behavior: "smooth" });
  }

  // Safe Markdown subset: DOM text nodes only, no model-controlled HTML.
  function renderMarkdown(target, value) {
    target.replaceChildren();
    function inline(node, text) {
      const pattern = /(\x60[^\x60]+\x60|\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g;
      let offset = 0, match;
      while ((match = pattern.exec(text))) {
        node.append(document.createTextNode(text.slice(offset, match.index)));
        const token = match[0];
        let child;
        if (token.startsWith("\x60")) {
          child = document.createElement("code"); child.textContent = token.slice(1,-1);
        } else if (token.startsWith("**") || token.startsWith("__")) {
          child = document.createElement("strong"); child.textContent = token.slice(2,-2);
        } else if (token.startsWith("*")) {
          child = document.createElement("em"); child.textContent = token.slice(1,-1);
        } else {
          const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
          child = document.createElement("a"); child.textContent = link[1]; child.href = link[2];
          child.rel = "noopener noreferrer"; child.target = "_blank";
        }
        node.append(child); offset = pattern.lastIndex;
      }
      node.append(document.createTextNode(text.slice(offset)));
    }
    let code = null, list = null;
    for (const line of String(value || "").replace(/\r/g, "").split("\n")) {
      if (/^\x60{3}/.test(line)) {
        if (code) { code = null; } else {
          const pre = document.createElement("pre"); code = document.createElement("code");
          pre.append(code); target.append(pre);
        }
        list = null; continue;
      }
      if (code) { code.append(document.createTextNode(line + "\n")); continue; }
      if (!line.trim()) { list = null; continue; }
      const bullet = /^\s*(?:[-*+]|\d+[.)])\s+(.+)$/.exec(line);
      if (bullet) {
        const tag = /^\s*\d/.test(line) ? "OL" : "UL";
        if (!list || list.tagName !== tag) { list = document.createElement(tag); target.append(list); }
        const li = document.createElement("li"); inline(li, bullet[1]); list.append(li); continue;
      }
      list = null;
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      const node = document.createElement(heading ? "h" + Math.min(heading[1].length + 2, 6) : line.startsWith("> ") ? "blockquote" : "p");
      inline(node, heading ? heading[2] : line.replace(/^> /, ""));
      target.append(node);
    }
  }

  // ---------- sidebar (mobile) ----------
  function closeSidebarOnMobile() {
    if (window.innerWidth <= 900) {
      $("sessionSidebar").classList.remove("open");
      document.body.classList.remove("sidebar-open");
    }
  }

  // ---------- wiring ----------
  function wire() {
    document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
    $("dockLibraryBtn").addEventListener("click", () => showView("modelsView"));
    $("dockUnloadBtn").addEventListener("click", unloadModel);
    $("libraryRefreshBtn").addEventListener("click", () => refreshModelLibrary(true));
    $("coverInput").addEventListener("change", (e) => uploadCover(e.target.files && e.target.files[0]));
    $("soundToggle").addEventListener("click", () => {
      Sound.setEnabled(!Sound.enabled);
      if (Sound.enabled) Sound.play("loaded");
      toast(Sound.enabled ? "Notification sounds on." : "Notification sounds muted.");
    });
    Sound.setEnabled(Sound.enabled);
    // Browsers only allow audio after a user gesture; unlock on the first one.
    const unlockAudio = () => { Sound.unlock(); removeEventListener("pointerdown", unlockAudio); removeEventListener("keydown", unlockAudio); };
    addEventListener("pointerdown", unlockAudio);
    addEventListener("keydown", unlockAudio);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) document.title = "Hephestus"; });
    $("commandBox").append(copyButton(() => ($("commandValue").classList.contains("empty") ? "" : $("commandValue").textContent)));

    $("startBtn").addEventListener("click", start);
    $("stopBtn").addEventListener("click", stop);
    $("newSessionBtn").addEventListener("click", newSession);

    document.querySelectorAll("#taskMode button").forEach((b) => b.addEventListener("click", () => {
      if ($("startBtn").disabled) return;
      setMode(b.dataset.mode);
      toast(b.dataset.mode === "goal" ? "Goal workflow selected." : "Quick workflow selected.");
    }));
    document.querySelectorAll("#thinkingLevel button").forEach((b) => b.addEventListener("click", () => {
      if (b.disabled) return;
      setThinking(b.dataset.level, true);
      closeThinkingPop();
    }));
    const dial = $("thinkingDial");
    if (dial) {
      dial.addEventListener("click", (e) => {
        e.stopPropagation();
        if (dial.disabled) return;
        const open = !$("thinkingPop").classList.contains("open");
        $("thinkingPop").classList.toggle("open", open);
        dial.setAttribute("aria-expanded", String(open));
      });
      document.addEventListener("click", (e) => {
        if (!e.target.closest("#thinkingPicker")) closeThinkingPop();
      });
    }

    $("approveBtn").addEventListener("click", () => {
      const guidance = $("approvalGuidance").value.trim();
      if (guidance) return submitApproval({ update_goal: guidance });
      const approved = [...document.querySelectorAll("#approvalCommandList input:checked")].map((x) => Number(x.dataset.index));
      if (!approved.length) return toast("Select a command or provide new guidance.", "error");
      submitApproval({ approved });
    });
    $("rejectBtn").addEventListener("click", () => submitApproval({ reject_all: true }));
    $("abortBtn").addEventListener("click", () => submitApproval({ abort: true }));

    $("exportBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleExportMenu(); });
    document.querySelectorAll("#exportMenu .menu-item").forEach((b) => b.addEventListener("click", () => {
      $("exportMenu").classList.remove("open");
      exportSession(b.dataset.format);
    }));
    document.addEventListener("click", (e) => {
      const menu = $("exportMenu");
      if (menu && menu.classList.contains("open") && !e.target.closest(".menu-wrap")) menu.classList.remove("open");
    });

    $("sidebarToggle").addEventListener("click", () => {
      const open = $("sessionSidebar").classList.toggle("open");
      document.body.classList.toggle("sidebar-open", open);
    });

    addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.key === "Enter" && !$("startBtn").disabled) { e.preventDefault(); start(); }
      if (e.key === "Escape") { $("exportMenu").classList.remove("open"); closeThinkingPop(); toggleQaExpanded(false); }
    });
  }

  let connectionBusy = false;
  function renderConnection(data) {
    const remote = data.mode === "remote";
    $("localModeBtn").classList.toggle("active", !remote);
    $("remoteModeBtn").classList.toggle("active", remote);
    $("localModeBtn").setAttribute("aria-pressed", String(!remote));
    $("remoteModeBtn").setAttribute("aria-pressed", String(remote));
    const busy = connectionBusy || activeStates.has(state.status);
    $("localModeBtn").disabled = $("remoteModeBtn").disabled = busy;
    const message = !remote ? "Local · commands and approvals on this computer"
      : !data.ready ? "Remote connection ended · select Local, then Remote to reconnect"
      : data.worker_online ? "Remote worker connected · approval on the target computer"
      : "Remote ready · download the ZIP in Help and launch it on the target";
    put($("connectionStatus"), message);
    put($("remoteConnectionStatus"), message);
    put($("remoteConnectionUrl"), data.url ? "Temporary worker endpoint: " + data.url : "");
    $("downloadConnectionBtn").disabled = !remote || !data.ready || connectionBusy;
  }
  async function refreshConnection() {
    if (connectionBusy) return;
    try { const result = await api("connection"); if (result.ok) renderConnection(result.data); } catch (_) { /* polling retries */ }
  }
  async function selectConnection(mode) {
    if (connectionBusy) return;
    connectionBusy = true;
    $("localModeBtn").disabled = $("remoteModeBtn").disabled = true;
    put($("connectionStatus"), mode === "remote" ? "Creating a private remote connection…" : "Closing the remote connection…");
    try {
      const result = await api("connection", { mode });
      if (!result.ok) { toast(result.data.error, "error"); return; }
      renderConnection(result.data);
      if (mode === "remote") showView("helpView");
    } catch (_) { toast("Connection setup failed. Check the controller terminal and network.", "error"); }
    finally { connectionBusy = false; await refreshConnection(); }
  }
  async function downloadConnection() {
    try {
      const response = await fetch("/api/connection/bundle", { headers: { "X-Hephestus-Token": token } });
      if (!response.ok) { const data = await response.json(); return toast(data.error, "error"); }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a"); link.href = url; link.download = "remote-connection.zip";
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Private bundle downloaded. Transfer securely and follow the steps in Help.");
    } catch (_) { toast("Could not download the connection bundle.", "error"); }
  }

  async function init() {
    wire();
    buildRailExtras();
    $("localModeBtn").addEventListener("click", () => selectConnection("local"));
    $("remoteModeBtn").addEventListener("click", () => selectConnection("remote"));
    $("connectionGuideBtn").addEventListener("click", () => showView("helpView"));
    $("downloadConnectionBtn").addEventListener("click", downloadConnection);
    const auth = await api("session", {});
    if (!auth.ok) { toast(auth.data.error || "Open the capability URL printed by python web.py", "error"); $("startBtn").disabled = true; return; }
    const current = await api("state");
    if (current.ok) {
      applyState(current.data);
      if (activeStates.has(current.data.status)) startStream();
    }
    await refreshConnection();
    setInterval(refreshConnection, 2500);
    await loadSettings();
    await refreshModelLibrary(false);
    await refreshSessions();
    pollState();
    pollGpu();
    state.gpu.frame = requestAnimationFrame(gpuLoop);
    setInterval(pollState, 1200);
    setInterval(() => { if (!state.selectedSession) refreshSessions(); }, 7000);
  }

  init();
})();
