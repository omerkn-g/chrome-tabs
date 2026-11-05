// TabFlow Chrome Extension Service Worker (background.js)
// Captures Open Tab Groups, Closed Tab Groups (via storage cache + chrome.sessions),
// and Ungrouped Tabs across Windows, and applies merges back to Chrome.

const CHROME_TO_UI_COLOR = {
  grey: "slate",
  blue: "blue",
  red: "rose",
  yellow: "amber",
  green: "emerald",
  pink: "rose",
  purple: "violet",
  cyan: "indigo",
  orange: "amber",
};

const UI_TO_CHROME_COLOR = {
  slate: "grey",
  blue: "blue",
  rose: "red",
  amber: "yellow",
  emerald: "green",
  violet: "purple",
  indigo: "cyan",
};

function isInternalOrTabFlowUrl(url = "") {
  return (
    url.startsWith("chrome://") ||
    url.startsWith("chrome-extension://") ||
    url.startsWith("http://localhost:8080") ||
    url.startsWith("http://127.0.0.1:8080")
  );
}

async function getStoredSavedGroupsCache() {
  try {
    const data = await chrome.storage.local.get("tabflow_saved_groups_cache");
    return data.tabflow_saved_groups_cache || {};
  } catch (_) {
    return {};
  }
}

async function setStoredSavedGroupsCache(cacheObj) {
  try {
    await chrome.storage.local.set({ tabflow_saved_groups_cache: cacheObj });
  } catch (_) {}
}

async function captureLiveChromeState() {
  const [tabGroups, windows, cachedGroups] = await Promise.all([
    chrome.tabGroups.query({}),
    chrome.windows.getAll({ populate: true, windowTypes: ["normal"] }),
    getStoredSavedGroupsCache(),
  ]);

  let recentlyClosed = [];
  if (chrome.sessions && chrome.sessions.getRecentlyClosed) {
    try {
      recentlyClosed = await chrome.sessions.getRecentlyClosed({ maxResults: 25 });
    } catch (_) {}
  }

  const groupMap = new Map();
  for (const tg of tabGroups) {
    groupMap.set(tg.id, {
      group_id: `chrome-grp-${tg.id}`,
      chrome_group_id: tg.id,
      window_id: tg.windowId,
      session_name: tg.title && tg.title.trim() ? tg.title.trim() : `Unnamed Group #${tg.id}`,
      color: CHROME_TO_UI_COLOR[tg.color] || "blue",
      collapsed: Boolean(tg.collapsed),
      is_ungrouped_pool: false,
      where_i_left_off_note: "Open Tab Group in Chrome.",
      tabs: [],
    });
  }

  const ungroupedByWindow = new Map();
  let winCounter = 1;

  for (const win of windows) {
    const winLabel = `Window #${winCounter++}`;
    const tabs = win.tabs || [];

    for (const tab of tabs) {
      const url = tab.url || tab.pendingUrl || "";
      if (!url || isInternalOrTabFlowUrl(url)) continue;

      const tabEntry = {
        tab_id: `chrome-tab-${tab.id}`,
        chrome_tab_id: tab.id,
        window_id: win.id,
        title: tab.title || url,
        url: url,
      };

      if (tab.groupId && tab.groupId !== -1 && groupMap.has(tab.groupId)) {
        groupMap.get(tab.groupId).tabs.push(tabEntry);
      } else {
        if (!ungroupedByWindow.has(win.id)) {
          ungroupedByWindow.set(win.id, {
            group_id: `chrome-ungrouped-win-${win.id}`,
            window_id: win.id,
            session_name: `Ungrouped Tabs (${winLabel})`,
            color: "slate",
            is_ungrouped_pool: true,
            where_i_left_off_note: `Loose ungrouped open tabs in ${winLabel}.`,
            tabs: [],
          });
        }
        ungroupedByWindow.get(win.id).tabs.push(tabEntry);
      }
    }
  }

  // Update persistent cache with currently open tab groups (keyed by normalized title + URL signature)
  const openGroupsList = Array.from(groupMap.values()).filter((g) => g.tabs.length > 0);
  const openTitles = new Set(openGroupsList.map((g) => g.session_name.toLowerCase()));

  for (const og of openGroupsList) {
    const cacheKey = og.session_name.toLowerCase();
    cachedGroups[cacheKey] = {
      ...og,
      group_id: `chrome-closed-${cacheKey.replace(/[^a-z0-9]+/g, "-")}`,
      where_i_left_off_note: "Saved Tab Group (currently closed in Chrome window).",
    };
  }
  await setStoredSavedGroupsCache(cachedGroups);

  // Include cached Tab Groups that are currently CLOSED in Chrome
  const closedSavedGroups = [];
  for (const [cacheKey, savedGrp] of Object.entries(cachedGroups)) {
    if (!openTitles.has(cacheKey) && Array.isArray(savedGrp.tabs) && savedGrp.tabs.length > 0) {
      closedSavedGroups.push(savedGrp);
    }
  }

  // Also check chrome.sessions.getRecentlyClosed for closed windows with multiple tabs
  let closedWinIdx = 1;
  for (const item of recentlyClosed) {
    if (item.window && Array.isArray(item.window.tabs)) {
      const validTabs = item.window.tabs.filter(
        (t) => t.url && !isInternalOrTabFlowUrl(t.url)
      );
      if (validTabs.length >= 2) {
        closedSavedGroups.push({
          group_id: `chrome-closed-session-${item.window.sessionId || closedWinIdx}`,
          session_name: `Recently Closed Window #${closedWinIdx++}`,
          color: "indigo",
          where_i_left_off_note: "Recovered from Chrome Recently Closed Sessions.",
          tabs: validTabs.map((t, idx) => ({
            tab_id: `closed-tab-${item.window.sessionId || closedWinIdx}-${idx}`,
            title: t.title || t.url,
            url: t.url,
          })),
        });
      }
    }
  }

  const exportedGroups = [
    ...openGroupsList,
    ...closedSavedGroups,
    ...Array.from(ungroupedByWindow.values()).filter((g) => g.tabs.length > 0),
  ];

  return {
    groups: exportedGroups,
    open_group_count: openGroupsList.length,
    closed_group_count: closedSavedGroups.length,
    window_count: windows.length,
    captured_at: new Date().toISOString(),
  };
}

// Automatically keep Saved Tab Groups cache updated whenever tabs or tabGroups change
if (chrome.tabGroups && chrome.tabGroups.onUpdated) {
  chrome.tabGroups.onUpdated.addListener(() => {
    captureLiveChromeState().catch(() => {});
  });
}
if (chrome.tabs && chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener(() => {
    captureLiveChromeState().catch(() => {});
  });
}

async function applyMergeInLiveChrome(payload) {
  try {
    const mergedSession = payload.merged_work_session;
    const duplicatesRemoved = payload.duplicates_removed || [];
    const routedStrays = payload.stray_routing || [];

    const dupChromeIds = duplicatesRemoved
      .map((t) => Number(String(t.tab_id || "").replace("chrome-tab-", "")))
      .filter((id) => Number.isInteger(id) && id > 0);
    if (dupChromeIds.length > 0) {
      try {
        await chrome.tabs.remove(dupChromeIds);
      } catch (_) {}
    }

    if (mergedSession && Array.isArray(mergedSession.tabs)) {
      const coreChromeTabIds = mergedSession.tabs
        .map((t) => Number(String(t.tab_id || "").replace("chrome-tab-", "")))
        .filter((id) => Number.isInteger(id) && id > 0);

      if (coreChromeTabIds.length > 0) {
        const newGroupId = await chrome.tabs.group({ tabIds: coreChromeTabIds });
        await chrome.tabGroups.update(newGroupId, {
          title: mergedSession.session_name,
          color: UI_TO_CHROME_COLOR[mergedSession.color] || "blue",
        });
      }
    }

    const straysByTarget = {};
    for (const s of routedStrays) {
      const tid = Number(String(s.tab_id || "").replace("chrome-tab-", ""));
      if (!Number.isInteger(tid) || tid <= 0) continue;
      const key = s.target_session_name || "Misc";
      straysByTarget[key] = straysByTarget[key] || [];
      straysByTarget[key].push(tid);
    }

    const existingGroups = await chrome.tabGroups.query({});
    for (const [targetName, tabIds] of Object.entries(straysByTarget)) {
      const match = existingGroups.find(
        (g) => (g.title || "").toLowerCase() === targetName.toLowerCase()
      );
      if (match) {
        await chrome.tabs.group({ groupId: match.id, tabIds });
      } else {
        const gid = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(gid, {
          title: targetName,
          color: targetName === "Misc" ? "grey" : "purple",
        });
      }
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

async function applyRenameInLiveChrome(payload) {
  try {
    const groupIdStr = String(payload.group_id || "");
    const newName = String(payload.new_session_name || "");
    if (groupIdStr.startsWith("chrome-grp-")) {
      const chromeGid = Number(groupIdStr.replace("chrome-grp-", ""));
      if (Number.isInteger(chromeGid) && chromeGid > 0) {
        await chrome.tabGroups.update(chromeGid, { title: newName });
      }
    } else if (groupIdStr.startsWith("chrome-ungrouped-win-") && Array.isArray(payload.tabs)) {
      const tabIds = payload.tabs
        .map((t) => Number(String(t.tab_id || "").replace("chrome-tab-", "")))
        .filter((id) => Number.isInteger(id) && id > 0);
      if (tabIds.length > 0) {
        const gid = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(gid, { title: newName, color: "blue" });
      }
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "CAPTURE_CHROME_TABS") {
    captureLiveChromeState().then(sendResponse);
    return true;
  }
  if (message?.type === "APPLY_CHROME_MERGE") {
    applyMergeInLiveChrome(message.payload || {}).then(sendResponse);
    return true;
  }
  if (message?.type === "APPLY_CHROME_RENAME") {
    applyRenameInLiveChrome(message.payload || {}).then(sendResponse);
    return true;
  }
});
