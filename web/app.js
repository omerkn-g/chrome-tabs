/* TabFlow Modern Light Responsive Web UI Controller */

const state = {
  tabGroups: [],
  pendingPlans: [],
  telemetry: { recent_logs: [], recent_spans: [], pii_redactions_count: 0 },
  memoryProfile: {},
  selectedGroupIds: new Set(),
  activeTelemetryTab: "logs",
  searchQuery: "",
};

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatSimpleMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/_([^_]+)_/g, "<em>$1</em>");
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\n/g, "<br/>");
  return html;
}

async function fetchBoardState(query = "") {
  const url = query ? `/api/state?q=${encodeURIComponent(query)}` : "/api/state";
  const res = await fetch(url);
  const data = await res.json();
  applyBoardSnapshot(data);
}

function applyBoardSnapshot(snapshot) {
  state.tabGroups = snapshot.tab_groups || [];
  state.pendingPlans = snapshot.pending_merge_plans || [];
  state.telemetry = snapshot.telemetry || state.telemetry;
  state.memoryProfile = snapshot.memory_profile || {};

  document.getElementById("statGroups").textContent = snapshot.total_groups ?? state.tabGroups.length;
  document.getElementById("statTabs").textContent = snapshot.total_tabs ?? 0;
  document.getElementById("statRedactions").textContent =
    state.telemetry.pii_redactions_count ?? 0;

  renderPendingPlans();
  renderTabGroups();
  renderTelemetry();
  updateSelectionButton();
}

function renderPendingPlans() {
  const container = document.getElementById("pendingPlansContainer");
  const list = document.getElementById("pendingPlansList");
  if (!state.pendingPlans.length) {
    container.style.display = "none";
    list.innerHTML = "";
    return;
  }

  container.style.display = "block";
  list.innerHTML = state.pendingPlans
    .map((plan) => {
      const sourceTags = (plan.source_group_names || plan.source_group_ids || [])
        .map((n) => `<span class="source-tag">${escapeHtml(n)}</span>`)
        .join(" + ");

      const strayItems = (plan.stray_routing || [])
        .map(
          (s) =>
            `<li>• <strong>${escapeHtml(s.title)}</strong> → routed to <strong>${escapeHtml(
              s.target_session_name
            )}</strong></li>`
        )
        .join("");

      const strayBox = strayItems
        ? `<div class="stray-routing-box">
             <strong>🧭 Stray Tabs Routed Automatically:</strong>
             <ul class="stray-list">${strayItems}</ul>
           </div>`
        : "";

      return `
        <div class="plan-card" data-plan-id="${escapeHtml(plan.plan_id)}">
          <div class="plan-sources">
            <span>Merging:</span> ${sourceTags}
          </div>

          <div class="plan-field">
            <label>Suggested Work Session Name (approve or customize):</label>
            <input
              type="text"
              id="planName-${escapeHtml(plan.plan_id)}"
              value="${escapeHtml(plan.suggested_session_name)}"
            />
          </div>

          <div class="plan-field">
            <label>Synthesized "Where I Left Off" Summary Note (Pro):</label>
            <textarea id="planNote-${escapeHtml(plan.plan_id)}" rows="2">${escapeHtml(
        plan.synthesized_note
      )}</textarea>
          </div>

          <div class="plan-summary-pills">
            <span class="mini-pill">${plan.merged_tabs.length} Core Unique Tabs</span>
            <span class="mini-pill mini-pill-dedup">${plan.duplicates_removed.length} Duplicate(s) Removed</span>
            <span class="mini-pill mini-pill-stray">${plan.stray_routing.length} Stray Tab(s) Routed</span>
          </div>

          ${strayBox}

          <div class="modal-actions">
            <button
              class="btn btn-success btn-sm"
              onclick="approveMergePlan('${escapeHtml(plan.plan_id)}')"
            >
              ✅ Approve &amp; Merge into Work Session
            </button>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderTabGroups() {
  const grid = document.getElementById("tabGroupsGrid");
  if (!state.tabGroups.length) {
    grid.innerHTML = `<div class="group-card"><p>No matching Work Sessions found.</p></div>`;
    return;
  }

  grid.innerHTML = state.tabGroups
    .map((grp) => {
      const isSelected = state.selectedGroupIds.has(grp.group_id);
      const colorClass = `color-${grp.color || "blue"}`;
      const noteText = grp.where_i_left_off_note
        ? escapeHtml(grp.where_i_left_off_note)
        : "No summary note yet — click Edit to add where you left off.";
      const noteEmptyClass = grp.where_i_left_off_note ? "" : "empty";

      const tabsHtml = (grp.tabs || [])
        .map((t) => {
          const scrubbedBadge =
            t.sanitized_params && t.sanitized_params.length
              ? `<span class="scrubbed-tag" title="Scrubbed: ${escapeHtml(
                  t.sanitized_params.join(", ")
                )}">🛡️ Scrubbed</span>`
              : "";
          return `
            <li class="tab-item">
              <div class="tab-main">
                <a class="tab-title" href="${escapeHtml(t.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(
            t.title
          )}">
                  ${escapeHtml(t.title)}
                </a>
                <div class="tab-domain">
                  <span>${escapeHtml(t.domain || t.url)}</span>
                  ${scrubbedBadge}
                </div>
              </div>
            </li>
          `;
        })
        .join("");

      return `
        <article class="group-card ${isSelected ? "selected" : ""}">
          <div class="group-header">
            <div class="group-title-row">
              <input
                type="checkbox"
                class="group-select-cb"
                ${isSelected ? "checked" : ""}
                onchange="toggleSelectGroup('${escapeHtml(grp.group_id)}', this.checked)"
                title="Select to merge with another Tab Group"
              />
              <span class="group-color-pill ${colorClass}">
                ${escapeHtml(grp.session_name)}
              </span>
              <span class="tab-count-badge">${(grp.tabs || []).length} tabs</span>
            </div>

            <div class="group-actions">
              <button
                class="btn btn-ghost btn-sm"
                onclick="openEditModal('${escapeHtml(grp.group_id)}')"
                title="Rename session or edit note"
              >
                ✏️ Edit
              </button>
              <button
                class="btn btn-ghost btn-sm"
                onclick="openDeleteModal('${escapeHtml(grp.group_id)}', '${escapeHtml(
        grp.session_name
      )}')"
                title="Delete tab group (HITL confirmed)"
              >
                🗑️
              </button>
            </div>
          </div>

          <div class="session-note-box ${noteEmptyClass}">
            <span class="note-label">📌 Where I Left Off</span>
            ${noteText}
          </div>

          <ul class="tab-list">
            ${tabsHtml}
          </ul>
        </article>
      `;
    })
    .join("");
}

function renderTelemetry() {
  const box = document.getElementById("telemetryContent");
  if (state.activeTelemetryTab === "logs") {
    const logs = state.telemetry.recent_logs || [];
    if (!logs.length) {
      box.innerHTML = `<div class="log-row">No structured JSON logs recorded yet.</div>`;
      return;
    }
    box.innerHTML = logs
      .map(
        (l) => `
        <div class="log-row">
          <div><span class="log-type">[${escapeHtml(l.event_type)}]</span> <strong>${escapeHtml(
          l.agent_name
        )}</strong></div>
          <div>${escapeHtml(l.message)}</div>
        </div>
      `
      )
      .join("");
  } else if (state.activeTelemetryTab === "spans") {
    const spans = state.telemetry.recent_spans || [];
    if (!spans.length) {
      box.innerHTML = `<div class="log-row">No OpenTelemetry spans recorded yet.</div>`;
      return;
    }
    box.innerHTML = spans
      .map(
        (s) => `
        <div class="log-row">
          <div><span class="log-type">${escapeHtml(s.span_name)}</span> (${escapeHtml(
          s.duration_ms
        )}ms)</div>
          <div>trace_id: ${escapeHtml(s.trace_id.slice(0, 12))}… | agent: ${escapeHtml(
          s.agent_name
        )}</div>
        </div>
      `
      )
      .join("");
  } else {
    const mem = state.memoryProfile || {};
    const domains = (mem.top_domains || [])
      .map((d) => `${escapeHtml(d.domain)} (${d.count})`)
      .join(", ");
    box.innerHTML = `
      <div class="log-row">
        <div><span class="log-type">ASYNC MEMORY BANK</span></div>
        <div><strong>Active Sessions:</strong> ${escapeHtml(
          (mem.active_work_sessions || []).join(", ")
        )}</div>
        <div><strong>Top Domains:</strong> ${domains || "N/A"}</div>
        <div><strong>Async Consolidations Completed:</strong> ${
          state.telemetry.async_memory_tasks_completed ?? 0
        }</div>
      </div>
    `;
  }
}

function appendChatMessage(role, text, metaLabel = "") {
  const container = document.getElementById("chatMessages");
  const div = document.createElement("div");
  div.className = `msg msg-${role}`;
  div.innerHTML = `
    <div class="msg-meta">${escapeHtml(
      metaLabel || (role === "user" ? "You" : "TabFlow Agent")
    )}</div>
    <div class="msg-bubble">${formatSimpleMarkdown(text)}</div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

async function sendChatMessage(message) {
  appendChatMessage("user", message, "You");
  const chip = document.getElementById("activeModelChip");
  chip.textContent = "Routing...";

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  const data = await res.json();
  const turn = data.turn || {};
  const routing = turn.routing || {};
  const modelLabel = routing.selected_model
    ? `${routing.target_agent} (${routing.selected_model})`
    : "TabFlow Coordinator";

  chip.textContent = routing.selected_model || "Ready";
  appendChatMessage("assistant", turn.reply || "Done!", modelLabel);
  if (data.board) {
    applyBoardSnapshot(data.board);
  }
}

async function approveMergePlan(planId) {
  const nameInput = document.getElementById(`planName-${planId}`);
  const noteInput = document.getElementById(`planNote-${planId}`);
  const finalName = nameInput ? nameInput.value.trim() : "Merged Work Session";
  const finalNote = noteInput ? noteInput.value.trim() : "";

  const res = await fetch("/api/execute-merge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      plan_id: planId,
      final_session_name: finalName,
      where_i_left_off_note: finalNote,
      user_confirmed: true,
    }),
  });
  const data = await res.json();
  if (data.result) {
    appendChatMessage(
      "assistant",
      `✅ **Human-in-the-Loop Confirmed:** ${data.result.message}`,
      "HITL Gate → Coordinator"
    );
    if (document.documentElement.getAttribute("data-tabflow-extension") === "installed") {
      window.postMessage(
        {
          type: "TABFLOW_APPLY_CHROME_MERGE",
          payload: data.result.data || {},
        },
        "*"
      );
    }
  }
  if (data.board) {
    applyBoardSnapshot(data.board);
  }
}

function toggleSelectGroup(groupId, checked) {
  if (checked) {
    state.selectedGroupIds.add(groupId);
  } else {
    state.selectedGroupIds.delete(groupId);
  }
  renderTabGroups();
  updateSelectionButton();
}

function updateSelectionButton() {
  const btn = document.getElementById("btnMergeSelected");
  const countSpan = document.getElementById("selectedCount");
  const count = state.selectedGroupIds.size;
  countSpan.textContent = count;
  btn.style.display = count >= 2 ? "inline-flex" : "none";
}

function openEditModal(groupId) {
  const grp = state.tabGroups.find((g) => g.group_id === groupId);
  if (!grp) return;
  document.getElementById("editGroupId").value = grp.group_id;
  document.getElementById("editSessionName").value = grp.session_name;
  document.getElementById("editSessionNote").value = grp.where_i_left_off_note || "";
  document.getElementById("editModal").style.display = "flex";
}

function openDeleteModal(groupId, sessionName) {
  document.getElementById("deleteGroupId").value = groupId;
  document.getElementById(
    "deleteModalText"
  ).textContent = `Are you sure you want to permanently delete the saved Tab Group "${sessionName}"? This high-stakes action is protected by the Human-in-the-Loop confirmation gate.`;
  document.getElementById("deleteModal").style.display = "flex";
}

window.addEventListener("message", async (event) => {
  if (event.source !== window || !event.data) return;

  if (event.data.type === "TABFLOW_CHROME_SNAPSHOT_RESULT") {
    const snapshot = event.data.snapshot || { groups: [] };
    const res = await fetch("/api/sync-chrome", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(snapshot),
    });
    const data = await res.json();
    appendChatMessage(
      "assistant",
      `🔄 **Synced ${data.synced_count || 0} live Tab Group(s) & Window Tab Pool(s) from your Chrome browser!** Click **✨ Auto-Analyze & Propose Merges** to organize them.`,
      "Chrome Extension Bridge"
    );
    if (data.board) applyBoardSnapshot(data.board);
  }
});

window.addEventListener("DOMContentLoaded", () => {
  fetchBoardState();

  document.getElementById("btnSyncChrome").addEventListener("click", () => {
    const isInstalled =
      document.documentElement.getAttribute("data-tabflow-extension") === "installed";
    if (isInstalled) {
      window.postMessage({ type: "TABFLOW_REQUEST_CHROME_SNAPSHOT" }, "*");
    } else {
      document.getElementById("extensionModal").style.display = "flex";
    }
  });

  document.getElementById("btnCloseExtensionModal").addEventListener("click", () => {
    document.getElementById("extensionModal").style.display = "none";
  });

  document.getElementById("btnImportClosedGroups").addEventListener("click", () => {
    document.getElementById("closedGroupsModal").style.display = "flex";
  });

  document.getElementById("btnCancelClosedModal").addEventListener("click", () => {
    document.getElementById("closedGroupsModal").style.display = "none";
  });

  document.getElementById("btnChooseLeveldbFolder").addEventListener("click", () => {
    document.getElementById("closedGroupsModal").style.display = "none";
    document.getElementById("leveldbFileInput").click();
  });

  document.getElementById("leveldbFileInput").addEventListener("change", async (e) => {
    const fileList = Array.from(e.target.files || []).filter(
      (f) => f.name.endsWith(".ldb") || f.name.endsWith(".log") || f.name.endsWith(".sst")
    );
    if (!fileList.length) {
      appendChatMessage(
        "assistant",
        "No `.ldb` or `.log` files found in the selected folder. Please select `~/.config/google-chrome/Default/Sync Data/LevelDB`.",
        "Chrome Profile Reader"
      );
      return;
    }

    const encodedFiles = await Promise.all(
      fileList.map(
        (file) =>
          new Promise((resolve) => {
            const reader = new FileReader();
            reader.onload = () => {
              const base64 = String(reader.result || "").split(",")[1] || "";
              resolve({ name: file.name, content_b64: base64 });
            };
            reader.onerror = () => resolve(null);
            reader.readAsDataURL(file);
          })
      )
    );

    const res = await fetch("/api/import-leveldb", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files: encodedFiles.filter(Boolean) }),
    });
    const data = await res.json();
    appendChatMessage(
      "assistant",
      `📂 **Imported ${data.imported_count || 0} Saved Tab Group(s) (including closed groups) from Chrome's Sync Data LevelDB!**`,
      "Chrome LevelDB Reader"
    );
    if (data.board) applyBoardSnapshot(data.board);
    e.target.value = "";
  });

  document.getElementById("btnAutoAnalyze").addEventListener("click", async () => {
    const chip = document.getElementById("activeModelChip");
    chip.textContent = "gemini-2.5-pro";
    const res = await fetch("/api/propose-auto-merges", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (data.turn && data.turn.reply) {
      appendChatMessage(
        "assistant",
        data.turn.reply,
        "session_synthesizer_pro_agent (gemini-2.5-pro)"
      );
    }
    if (data.board) applyBoardSnapshot(data.board);
  });

  document.getElementById("btnMergeSelected").addEventListener("click", async () => {
    const ids = Array.from(state.selectedGroupIds);
    if (ids.length < 2) return;
    const res = await fetch("/api/propose-custom-merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_group_ids: ids }),
    });
    const data = await res.json();
    state.selectedGroupIds.clear();
    if (data.result) {
      appendChatMessage("assistant", data.result.message, "session_synthesizer_pro_agent");
    }
    if (data.board) applyBoardSnapshot(data.board);
  });

  document.getElementById("btnResetDemo").addEventListener("click", async () => {
    const res = await fetch("/api/reset-demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    state.selectedGroupIds.clear();
    appendChatMessage(
      "assistant",
      "↺ Reset database back to the initial 5 demo Tab Groups (with duplicate URLs and stray tabs ready to test!).",
      "TabFlow System"
    );
    if (data.board) applyBoardSnapshot(data.board);
  });

  document.getElementById("searchInput").addEventListener("input", (e) => {
    state.searchQuery = e.target.value.trim();
    fetchBoardState(state.searchQuery);
  });

  document.getElementById("chatForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("chatInput");
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    await sendChatMessage(msg);
  });

  document.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const prompt = btn.getAttribute("data-prompt");
      if (prompt) sendChatMessage(prompt);
    });
  });

  document.querySelectorAll(".tel-tab").forEach((tabBtn) => {
    tabBtn.addEventListener("click", () => {
      document.querySelectorAll(".tel-tab").forEach((b) => b.classList.remove("active"));
      tabBtn.classList.add("active");
      state.activeTelemetryTab = tabBtn.getAttribute("data-tab");
      renderTelemetry();
    });
  });

  document.getElementById("btnCancelEdit").addEventListener("click", () => {
    document.getElementById("editModal").style.display = "none";
  });

  document.getElementById("btnSaveEdit").addEventListener("click", async () => {
    const groupId = document.getElementById("editGroupId").value;
    const newName = document.getElementById("editSessionName").value.trim();
    const note = document.getElementById("editSessionNote").value.trim();
    if (!newName) return;
    const res = await fetch("/api/rename-group", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group_id: groupId,
        new_session_name: newName,
        where_i_left_off_note: note,
      }),
    });
    const data = await res.json();
    document.getElementById("editModal").style.display = "none";
    if (data.result) {
      appendChatMessage("assistant", data.result.message, "tab_triage_flash_agent");
      if (document.documentElement.getAttribute("data-tabflow-extension") === "installed") {
        const updatedSession = (data.result.data || {}).work_session || {};
        window.postMessage(
          {
            type: "TABFLOW_APPLY_CHROME_RENAME",
            payload: {
              group_id: groupId,
              new_session_name: newName,
              tabs: updatedSession.tabs || [],
            },
          },
          "*"
        );
      }
    }
    if (data.board) applyBoardSnapshot(data.board);
  });

  document.getElementById("btnCancelDelete").addEventListener("click", () => {
    document.getElementById("deleteModal").style.display = "none";
  });

  document.getElementById("btnConfirmDelete").addEventListener("click", async () => {
    const groupId = document.getElementById("deleteGroupId").value;
    const res = await fetch("/api/delete-group", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group_id: groupId,
        reason: "User confirmed deletion in modal",
        user_confirmed: true,
      }),
    });
    const data = await res.json();
    document.getElementById("deleteModal").style.display = "none";
    if (data.result) {
      appendChatMessage("assistant", `🗑️ ${data.result.message}`, "HITL Gate → Coordinator");
    }
    if (data.board) applyBoardSnapshot(data.board);
  });
});

